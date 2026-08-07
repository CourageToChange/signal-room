from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from .migrate import CURRENT_SCHEMA_VERSION, verified_backup, verify_schema
from .models import (
    AssetDefinition,
    AssetStateView,
    AssetView,
    HealthState,
    IncidentEventView,
    IncidentPage,
    IncidentState,
    IncidentSummary,
    IncidentType,
    IncidentView,
    MaintenanceWindowView,
    Observation,
    ProviderKind,
    ProviderStateView,
    Severity,
    StreamEventView,
    TimelinePage,
)


class DatabaseError(RuntimeError):
    pass


class StorageFullError(DatabaseError):
    pass


class ConflictError(DatabaseError):
    pass


class IdempotencyConflictError(ConflictError):
    pass


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _severity_rank(value: Severity | str) -> int:
    return {Severity.INFO: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}[Severity(value)]


def _health_rank(value: HealthState | str) -> int:
    return {
        HealthState.HEALTHY: 0,
        HealthState.DEGRADED: 1,
        HealthState.UNKNOWN: 2,
        HealthState.DOWN: 3,
    }[HealthState(value)]


def _encode_cursor(opened_at: str, incident_id: str) -> str:
    return base64.urlsafe_b64encode(f"{opened_at}\n{incident_id}".encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        opened_at, incident_id = base64.urlsafe_b64decode(padded).decode().split("\n", 1)
        datetime.fromisoformat(opened_at)
        if not incident_id:
            raise ValueError
        return opened_at, incident_id
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError("invalid cursor") from error


class Database:
    """The core process's sole SQLite owner.

    Runtime connection never edits schema.  The one-shot migration command must
    have brought the database to the packaged schema before the core can start.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self.connection is None:
            raise RuntimeError("database is not connected")
        return self.connection

    async def connect(self) -> None:
        await verify_schema(self.path)
        self.connection = await aiosqlite.connect(self.path)
        self.connection.row_factory = aiosqlite.Row
        await self.connection.execute("PRAGMA foreign_keys=ON")
        await self.connection.execute("PRAGMA journal_mode=WAL")
        await self.connection.execute("PRAGMA synchronous=FULL")
        await self.connection.execute("PRAGMA busy_timeout=5000")
        await self.connection.execute("PRAGMA wal_autocheckpoint=1000")

    async def close(self) -> None:
        if self.connection is not None:
            await self.connection.close()
            self.connection = None

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._write_lock:
            try:
                await self.conn.execute("BEGIN IMMEDIATE")
                yield self.conn
                await self.conn.commit()
            except (sqlite3.DatabaseError, aiosqlite.Error) as error:
                await self.conn.rollback()
                if "full" in str(error).lower():
                    raise StorageFullError("SQLite storage is full") from error
                raise
            except Exception:
                await self.conn.rollback()
                raise

    async def _append_stream_unlocked(
        self,
        connection: aiosqlite.Connection,
        *,
        topic: str,
        kind: str,
        subject_id: str | None,
        payload: dict[str, Any],
        at: datetime,
        event_uuid: str | None = None,
    ) -> tuple[int, str]:
        event_id = event_uuid or str(uuid4())
        cursor = await connection.execute(
            """
            INSERT INTO stream_events(event_uuid, created_at, topic, kind, subject_id, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (event_id, _iso(at), topic, kind, subject_id, _json(payload)),
        )
        return int(cursor.lastrowid or 0), event_id

    async def _append_audit_unlocked(
        self,
        connection: aiosqlite.Connection,
        *,
        kind: str,
        subject_type: str,
        subject_id: str | None,
        message: str,
        at: datetime,
        actor_subject: str | None = None,
        actor_email: str | None = None,
        metadata: dict[str, Any] | None = None,
        event_uuid: str | None = None,
    ) -> str:
        identifier = event_uuid or str(uuid4())
        await connection.execute(
            """
            INSERT INTO audit_events(
              event_uuid, created_at, kind, subject_type, subject_id,
              actor_subject, actor_email, message, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                identifier,
                _iso(at),
                kind,
                subject_type,
                subject_id,
                actor_subject,
                actor_email,
                message[:500],
                _json(metadata or {}),
            ),
        )
        return identifier

    async def sync_assets(
        self,
        assets: list[AssetDefinition],
        revision: str = "development",
        at: datetime | None = None,
    ) -> None:
        changed_at = at or datetime.now(UTC)
        active_ids = {asset.id for asset in assets}
        async with self._transaction() as connection:
            rows = await (await connection.execute("SELECT id FROM assets")).fetchall()
            for row in rows:
                if row["id"] not in active_ids:
                    await connection.execute(
                        "UPDATE assets SET retired_at=COALESCE(retired_at, ?) WHERE id=?",
                        (_iso(changed_at), row["id"]),
                    )

            for asset in assets:
                await connection.execute(
                    """
                    INSERT INTO assets(
                      id, label, kind, parent_id, runbook_id, sort_order,
                      retired_at, configuration_revision
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
                    ON CONFLICT(id) DO UPDATE SET
                      label=excluded.label,
                      kind=excluded.kind,
                      parent_id=excluded.parent_id,
                      runbook_id=excluded.runbook_id,
                      sort_order=excluded.sort_order,
                      retired_at=NULL,
                      configuration_revision=excluded.configuration_revision
                    """,
                    (
                        asset.id,
                        asset.label,
                        asset.kind,
                        asset.parent_id,
                        asset.runbook_id,
                        asset.sort_order,
                        revision,
                    ),
                )
                await connection.execute(
                    "INSERT OR IGNORE INTO asset_state(asset_id) VALUES (?)", (asset.id,)
                )

            for asset in assets:
                await connection.execute(
                    "DELETE FROM asset_dependencies WHERE asset_id=?", (asset.id,)
                )
                await connection.executemany(
                    "INSERT INTO asset_dependencies(asset_id, depends_on_id) VALUES (?, ?)",
                    [(asset.id, dependency) for dependency in asset.depends_on],
                )
                existing_checks = {
                    str(row["check_id"])
                    for row in await (
                        await connection.execute(
                            "SELECT check_id FROM asset_checks WHERE asset_id=?", (asset.id,)
                        )
                    ).fetchall()
                }
                current_checks = {check.id for check in asset.checks}
                for check_id in existing_checks - current_checks:
                    await connection.execute(
                        "DELETE FROM check_state WHERE asset_id=? AND check_id=?",
                        (asset.id, check_id),
                    )
                    await connection.execute(
                        "DELETE FROM asset_checks WHERE asset_id=? AND check_id=?",
                        (asset.id, check_id),
                    )
                for check in asset.checks:
                    await connection.execute(
                        """
                        INSERT INTO asset_checks(asset_id, check_id, check_type, definition_json)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(asset_id, check_id) DO UPDATE SET
                          check_type=excluded.check_type,
                          definition_json=excluded.definition_json
                        """,
                        (
                            asset.id,
                            check.id,
                            check.type,
                            check.model_dump_json(exclude_none=True),
                        ),
                    )
            await connection.execute(
                """
                INSERT INTO runtime_state(key, value) VALUES ('configuration_revision', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (revision,),
            )
            await self._append_audit_unlocked(
                connection,
                kind="configuration_applied",
                subject_type="configuration",
                subject_id=revision,
                message="Topology configuration synchronized",
                at=changed_at,
                metadata={"active_asset_count": len(assets)},
            )
            await self._append_stream_unlocked(
                connection,
                topic="snapshot",
                kind="configuration_applied",
                subject_id=revision,
                payload={"revision": revision},
                at=changed_at,
            )

    async def list_assets(self, *, include_retired: bool = False) -> list[AssetView]:
        query = """
            SELECT a.id, a.label, a.kind, a.parent_id, a.runbook_id,
                   a.sort_order, a.retired_at,
                   COALESCE((
                     SELECT json_group_array(depends_on_id)
                     FROM (SELECT depends_on_id FROM asset_dependencies
                           WHERE asset_id=a.id ORDER BY depends_on_id)
                   ), '[]') AS dependencies_json,
                   COALESCE((
                     SELECT json_group_array(check_id)
                     FROM (SELECT check_id FROM asset_checks
                           WHERE asset_id=a.id ORDER BY check_id)
                   ), '[]') AS checks_json
            FROM assets a
            WHERE (? OR a.retired_at IS NULL)
            ORDER BY a.sort_order, a.label
            """
        rows = await (await self.conn.execute(query, (include_retired,))).fetchall()
        return [
            AssetView(
                id=row["id"],
                label=row["label"],
                kind=row["kind"],
                depends_on=json.loads(row["dependencies_json"]),
                parent_id=row["parent_id"],
                check_ids=json.loads(row["checks_json"]),
                runbook_id=row["runbook_id"],
                sort_order=row["sort_order"],
                retired_at=_dt(row["retired_at"]),
            )
            for row in rows
        ]

    async def get_asset(self, asset_id: str) -> AssetView | None:
        return next((item for item in await self.list_assets() if item.id == asset_id), None)

    async def list_states(self) -> list[AssetStateView]:
        rows = await (
            await self.conn.execute(
                """
                SELECT s.asset_id, s.health, s.last_observed_at, s.unhealthy_since_at,
                       s.consecutive_failures, s.consecutive_successes, s.message,
                       s.latency_ms, s.cpu_ratio, s.memory_ratio, s.disk_ratio
                FROM asset_state s JOIN assets a ON a.id=s.asset_id
                WHERE a.retired_at IS NULL
                ORDER BY a.sort_order, a.id
                """
            )
        ).fetchall()
        return [self._state_from_row(row) for row in rows]

    @staticmethod
    def _state_from_row(row: aiosqlite.Row) -> AssetStateView:
        data = dict(row)
        data["last_observed_at"] = _dt(data["last_observed_at"])
        data["unhealthy_since_at"] = _dt(data["unhealthy_since_at"])
        return AssetStateView.model_validate(data)

    async def get_state(self, asset_id: str) -> AssetStateView:
        row = await (
            await self.conn.execute(
                """
                SELECT asset_id, health, last_observed_at, unhealthy_since_at,
                       consecutive_failures, consecutive_successes, message,
                       latency_ms, cpu_ratio, memory_ratio, disk_ratio
                FROM asset_state WHERE asset_id=?
                """,
                (asset_id,),
            )
        ).fetchone()
        if row is None:
            raise KeyError(asset_id)
        return self._state_from_row(row)

    async def record_provider_batch(
        self,
        *,
        provider: ProviderKind,
        run_id: str,
        attempted_at: datetime,
        observations: list[Observation],
        success: bool,
        completed_at: datetime | None = None,
        error_code: str | None = None,
        message: str = "",
    ) -> list[AssetStateView]:
        completed = completed_at or datetime.now(UTC)
        ordered = sorted(observations, key=lambda item: (item.asset_id, item.check_id))
        keys = [(item.asset_id, item.check_id) for item in ordered]
        if len(set(keys)) != len(keys):
            raise ValueError("a provider batch cannot contain duplicate asset/check observations")

        affected: set[str] = set()
        async with self._transaction() as connection:
            existing_run = await (
                await connection.execute("SELECT 1 FROM provider_runs WHERE id=?", (run_id,))
            ).fetchone()
            if existing_run is not None:
                return []
            await connection.execute(
                """
                INSERT INTO provider_runs(
                  id, provider, attempted_at, completed_at, success,
                  observation_count, error_code, message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    provider,
                    _iso(attempted_at),
                    _iso(completed),
                    int(success),
                    len(ordered),
                    error_code,
                    message[:240],
                ),
            )
            await connection.execute(
                """
                INSERT INTO provider_state(
                  provider, last_attempt_at, last_success_at, consecutive_failures, message
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                  last_attempt_at=excluded.last_attempt_at,
                  last_success_at=CASE WHEN ? THEN excluded.last_success_at
                                       ELSE provider_state.last_success_at END,
                  consecutive_failures=CASE WHEN ? THEN 0
                    ELSE provider_state.consecutive_failures + 1 END,
                  message=excluded.message
                """,
                (
                    provider,
                    _iso(attempted_at),
                    _iso(completed) if success else None,
                    0 if success else 1,
                    (
                        message
                        or ("Provider batch completed" if success else "Provider batch failed")
                    )[:240],
                    int(success),
                    int(success),
                ),
            )

            for original in ordered:
                check_id = original.check_id
                definition = await (
                    await connection.execute(
                        "SELECT check_id FROM asset_checks WHERE asset_id=? AND check_id=?",
                        (original.asset_id, check_id),
                    )
                ).fetchone()
                if definition is None and check_id == "default":
                    definition = await (
                        await connection.execute(
                            """
                            SELECT check_id FROM asset_checks
                            WHERE asset_id=? ORDER BY check_id LIMIT 1
                            """,
                            (original.asset_id,),
                        )
                    ).fetchone()
                    if definition is not None:
                        check_id = str(definition["check_id"])
                if definition is None:
                    raise KeyError(f"unknown asset/check {original.asset_id}/{check_id}")
                observation = original.model_copy(
                    update={"check_id": check_id, "provider": provider, "provider_run_id": run_id}
                )
                affected.add(observation.asset_id)
                await connection.execute(
                    """
                    INSERT INTO samples(
                      asset_id, observed_at, health, message, latency_ms, cpu_ratio,
                      memory_ratio, disk_ratio, details_json, check_id, provider,
                      provider_run_id, condition
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        observation.asset_id,
                        _iso(observation.observed_at),
                        observation.health,
                        observation.message,
                        observation.latency_ms,
                        observation.cpu_ratio,
                        observation.memory_ratio,
                        observation.disk_ratio,
                        _json(observation.details),
                        check_id,
                        provider,
                        run_id,
                        observation.condition,
                    ),
                )
                previous = await (
                    await connection.execute(
                        """
                        SELECT last_observed_at, unhealthy_since_at,
                               consecutive_failures, consecutive_successes
                        FROM check_state WHERE asset_id=? AND check_id=?
                        """,
                        (observation.asset_id, check_id),
                    )
                ).fetchone()
                if previous and previous["last_observed_at"]:
                    if (
                        datetime.fromisoformat(previous["last_observed_at"])
                        > observation.observed_at
                    ):
                        continue
                unhealthy = observation.health != HealthState.HEALTHY
                failures = (
                    (int(previous["consecutive_failures"]) + 1)
                    if previous and unhealthy
                    else (1 if unhealthy else 0)
                )
                successes = (
                    int(previous["consecutive_successes"]) + 1
                    if previous and not unhealthy
                    else (1 if not unhealthy else 0)
                )
                unhealthy_since = None
                if unhealthy:
                    unhealthy_since = (
                        previous["unhealthy_since_at"]
                        if previous and previous["unhealthy_since_at"]
                        else _iso(observation.observed_at)
                    )
                await connection.execute(
                    """
                    INSERT INTO check_state(
                      asset_id, check_id, provider, health, last_observed_at,
                      unhealthy_since_at, consecutive_failures, consecutive_successes,
                      message, latency_ms, cpu_ratio, memory_ratio, disk_ratio, condition
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(asset_id, check_id) DO UPDATE SET
                      provider=excluded.provider,
                      health=excluded.health,
                      last_observed_at=excluded.last_observed_at,
                      unhealthy_since_at=excluded.unhealthy_since_at,
                      consecutive_failures=excluded.consecutive_failures,
                      consecutive_successes=excluded.consecutive_successes,
                      message=excluded.message,
                      latency_ms=excluded.latency_ms,
                      cpu_ratio=excluded.cpu_ratio,
                      memory_ratio=excluded.memory_ratio,
                      disk_ratio=excluded.disk_ratio,
                      condition=excluded.condition
                    """,
                    (
                        observation.asset_id,
                        check_id,
                        provider,
                        observation.health,
                        _iso(observation.observed_at),
                        unhealthy_since,
                        failures,
                        successes,
                        observation.message,
                        observation.latency_ms,
                        observation.cpu_ratio,
                        observation.memory_ratio,
                        observation.disk_ratio,
                        observation.condition,
                    ),
                )

            for asset_id in sorted(affected):
                check_rows = await (
                    await connection.execute(
                        """
                        SELECT * FROM check_state WHERE asset_id=?
                        ORDER BY last_observed_at DESC, check_id
                        """,
                        (asset_id,),
                    )
                ).fetchall()
                if not check_rows:
                    continue
                worst = max(
                    check_rows,
                    key=lambda row: (_health_rank(row["health"]), row["last_observed_at"] or ""),
                )
                previous_asset = await (
                    await connection.execute(
                        """
                        SELECT consecutive_failures, consecutive_successes, unhealthy_since_at
                        FROM asset_state WHERE asset_id=?
                        """,
                        (asset_id,),
                    )
                ).fetchone()
                if previous_asset is None:
                    raise KeyError(asset_id)
                unhealthy = worst["health"] != HealthState.HEALTHY
                failures = int(previous_asset["consecutive_failures"] or 0) + 1 if unhealthy else 0
                successes = (
                    int(previous_asset["consecutive_successes"] or 0) + 1 if not unhealthy else 0
                )
                unhealthy_since = None
                if unhealthy:
                    unhealthy_since = (
                        previous_asset["unhealthy_since_at"] or worst["last_observed_at"]
                    )
                await connection.execute(
                    """
                    UPDATE asset_state SET
                      health=?, last_observed_at=?, unhealthy_since_at=?,
                      consecutive_failures=?, consecutive_successes=?, message=?,
                      latency_ms=?, cpu_ratio=?, memory_ratio=?, disk_ratio=?, last_check_id=?
                    WHERE asset_id=?
                    """,
                    (
                        worst["health"],
                        worst["last_observed_at"],
                        unhealthy_since,
                        failures,
                        successes,
                        worst["message"],
                        worst["latency_ms"],
                        worst["cpu_ratio"],
                        worst["memory_ratio"],
                        worst["disk_ratio"],
                        worst["check_id"],
                        asset_id,
                    ),
                )

            await self._append_stream_unlocked(
                connection,
                topic="provider",
                kind="completed" if success else "failed",
                subject_id=str(provider),
                payload={"provider": provider, "success": success},
                at=completed,
            )
        return [await self.get_state(asset_id) for asset_id in sorted(affected)]

    async def record_observation(self, observation: Observation) -> AssetStateView:
        run_id = observation.provider_run_id or str(uuid4())
        states = await self.record_provider_batch(
            provider=observation.provider,
            run_id=run_id,
            attempted_at=observation.observed_at,
            completed_at=observation.observed_at,
            observations=[observation],
            success=True,
        )
        return states[0]

    async def list_provider_states(self) -> list[ProviderStateView]:
        rows = await (
            await self.conn.execute(
                """
                SELECT provider, last_attempt_at, last_success_at,
                       consecutive_failures, message
                FROM provider_state ORDER BY provider
                """
            )
        ).fetchall()
        values: list[ProviderStateView] = []
        for row in rows:
            attempt = _dt(row["last_attempt_at"])
            success = _dt(row["last_success_at"])
            status = "healthy"
            if attempt is None:
                status = "never"
            elif success is None or attempt > success:
                status = "failed"
            values.append(
                ProviderStateView(
                    provider=row["provider"],
                    last_attempt_at=attempt,
                    last_success_at=success,
                    consecutive_failures=row["consecutive_failures"],
                    status=status,
                    message=row["message"],
                )
            )
        return values

    async def find_active_incident(
        self,
        root_asset_id: str,
        incident_type: IncidentType = IncidentType.ASSET_DOWN,
    ) -> IncidentView | None:
        fingerprint = f"{incident_type}:{root_asset_id}"
        row = await (
            await self.conn.execute(
                """
                SELECT id FROM incidents
                WHERE fingerprint=? AND state IN ('open', 'recovering')
                ORDER BY opened_at DESC LIMIT 1
                """,
                (fingerprint,),
            )
        ).fetchone()
        return await self.get_incident(row["id"]) if row else None

    async def find_active_root_incident(self, root_asset_id: str) -> IncidentView | None:
        row = await (
            await self.conn.execute(
                """
                SELECT id FROM incidents
                WHERE root_asset_id=? AND state IN ('open', 'recovering')
                ORDER BY opened_at ASC, id ASC LIMIT 1
                """,
                (root_asset_id,),
            )
        ).fetchone()
        return await self.get_incident(row["id"]) if row else None

    async def previous_incident(
        self, root_asset_id: str, incident_type: IncidentType
    ) -> IncidentSummary | None:
        row = await (
            await self.conn.execute(
                """
                SELECT id FROM incidents
                WHERE root_asset_id=? AND incident_type=?
                  AND state IN ('resolved', 'closed')
                ORDER BY opened_at DESC LIMIT 1
                """,
                (root_asset_id, incident_type),
            )
        ).fetchone()
        return await self.get_incident_summary(row["id"]) if row else None

    async def _append_incident_event_unlocked(
        self,
        connection: aiosqlite.Connection,
        incident_id: str,
        kind: str,
        message: str,
        *,
        created_at: datetime,
        metadata: dict[str, Any] | None = None,
        actor_subject: str | None = None,
        actor_email: str | None = None,
        notify: bool = False,
    ) -> int:
        event_uuid = str(uuid4())
        cursor = await connection.execute(
            """
            INSERT INTO incident_events(
              incident_id, created_at, kind, message, metadata_json,
              event_uuid, actor_subject, actor_email
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                incident_id,
                _iso(created_at),
                kind,
                message[:500],
                _json(metadata or {}),
                event_uuid,
                actor_subject,
                actor_email,
            ),
        )
        incident_row = await (
            await connection.execute(
                """
                SELECT root_asset_id, incident_type, severity, state
                FROM incidents WHERE id=?
                """,
                (incident_id,),
            )
        ).fetchone()
        if incident_row is None:
            raise KeyError(incident_id)
        affected_rows = await (
            await connection.execute(
                "SELECT asset_id FROM incident_assets WHERE incident_id=? ORDER BY asset_id",
                (incident_id,),
            )
        ).fetchall()
        public_payload = {
            "event_uuid": event_uuid,
            "incident_id": incident_id,
            "event_kind": kind,
            "occurred_at": _iso(created_at),
            "root_asset_id": incident_row["root_asset_id"],
            "incident_type": incident_row["incident_type"],
            "severity": incident_row["severity"],
            "state": incident_row["state"],
            "affected_asset_ids": [row["asset_id"] for row in affected_rows],
        }
        await self._append_stream_unlocked(
            connection,
            topic="incident",
            kind=kind,
            subject_id=incident_id,
            payload=public_payload,
            at=created_at,
            event_uuid=event_uuid,
        )
        if notify:
            await connection.execute(
                """
                INSERT INTO notification_outbox(
                  event_uuid, incident_id, event_kind, payload_json,
                  created_at, next_attempt_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event_uuid,
                    incident_id,
                    kind,
                    _json(public_payload),
                    _iso(created_at),
                    _iso(created_at),
                ),
            )
        return int(cursor.lastrowid or 0)

    async def create_incident(
        self,
        *,
        incident_id: str,
        root_asset_id: str,
        severity: Severity,
        title: str,
        summary: str,
        opened_at: datetime,
        incident_type: IncidentType = IncidentType.ASSET_DOWN,
        affected_asset_id: str | None = None,
    ) -> IncidentView:
        fingerprint = f"{incident_type}:{root_asset_id}"
        previous = await self.previous_incident(root_asset_id, incident_type)
        async with self._transaction() as connection:
            await connection.execute(
                """
                INSERT INTO incidents(
                  id, previous_incident_id, fingerprint, root_asset_id,
                  incident_type, severity, state, version, title, summary, opened_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'open', 1, ?, ?, ?)
                """,
                (
                    incident_id,
                    previous.id if previous else None,
                    fingerprint,
                    root_asset_id,
                    incident_type,
                    severity,
                    title,
                    summary,
                    _iso(opened_at),
                ),
            )
            asset_ids = {root_asset_id, affected_asset_id or root_asset_id}
            await connection.executemany(
                "INSERT INTO incident_assets(incident_id, asset_id) VALUES (?, ?)",
                [(incident_id, asset_id) for asset_id in sorted(asset_ids)],
            )
            await self._append_incident_event_unlocked(
                connection,
                incident_id,
                "opened",
                summary,
                created_at=opened_at,
                metadata={
                    "root_asset_id": root_asset_id,
                    "incident_type": incident_type,
                    "severity": severity,
                    "previous_incident_id": previous.id if previous else None,
                },
                notify=(
                    not await self._is_maintenance_active_unlocked(
                        connection, root_asset_id, opened_at
                    )
                    and await self._notifications_may_queue_unlocked(connection)
                ),
            )
        incident = await self.get_incident(incident_id)
        if incident is None:
            raise RuntimeError("created incident could not be reloaded")
        return incident

    async def attach_asset(
        self, incident_id: str, asset_id: str, message: str, at: datetime
    ) -> None:
        async with self._transaction() as connection:
            cursor = await connection.execute(
                "INSERT OR IGNORE INTO incident_assets(incident_id, asset_id) VALUES (?, ?)",
                (incident_id, asset_id),
            )
            if cursor.rowcount:
                await connection.execute(
                    "UPDATE incidents SET version=version+1 WHERE id=?", (incident_id,)
                )
                await self._append_incident_event_unlocked(
                    connection,
                    incident_id,
                    "correlated",
                    message,
                    created_at=at,
                    metadata={"asset_id": asset_id},
                )

    async def escalate_incident(
        self, incident_id: str, severity: Severity, at: datetime, message: str
    ) -> bool:
        async with self._transaction() as connection:
            row = await (
                await connection.execute(
                    "SELECT severity, state, root_asset_id FROM incidents WHERE id=?",
                    (incident_id,),
                )
            ).fetchone()
            if row is None or row["state"] not in {"open", "recovering"}:
                return False
            if _severity_rank(row["severity"]) >= _severity_rank(severity):
                return False
            await connection.execute(
                "UPDATE incidents SET severity=?, version=version+1 WHERE id=?",
                (severity, incident_id),
            )
            await self._append_incident_event_unlocked(
                connection,
                incident_id,
                "escalated",
                message,
                created_at=at,
                metadata={"severity": severity},
                notify=(
                    not await self._is_maintenance_active_unlocked(
                        connection, row["root_asset_id"], at
                    )
                    and await self._notifications_may_queue_unlocked(connection)
                ),
            )
        return True

    async def add_event(
        self,
        incident_id: str,
        kind: str,
        message: str,
        *,
        created_at: datetime,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        async with self._transaction() as connection:
            return await self._append_incident_event_unlocked(
                connection,
                incident_id,
                kind,
                message,
                created_at=created_at,
                metadata=metadata,
            )

    async def set_incident_state(
        self,
        incident_id: str,
        state: IncidentState,
        *,
        at: datetime,
        message: str,
    ) -> bool:
        async with self._transaction() as connection:
            row = await (
                await connection.execute(
                    "SELECT state, root_asset_id FROM incidents WHERE id=?", (incident_id,)
                )
            ).fetchone()
            if row is None:
                return False
            current = IncidentState(row["state"])
            if current in {IncidentState.RESOLVED, IncidentState.CLOSED}:
                return False
            allowed = {
                IncidentState.OPEN: {IncidentState.RECOVERING, IncidentState.RESOLVED},
                IncidentState.RECOVERING: {IncidentState.OPEN, IncidentState.RESOLVED},
            }
            if state not in allowed[current]:
                return False
            recovered_at = _iso(at) if state == IncidentState.RESOLVED else None
            await connection.execute(
                """
                UPDATE incidents SET state=?, recovered_at=COALESCE(?, recovered_at),
                  version=version+1 WHERE id=?
                """,
                (state, recovered_at, incident_id),
            )
            await self._append_incident_event_unlocked(
                connection,
                incident_id,
                str(state),
                message,
                created_at=at,
                notify=(
                    state == IncidentState.RESOLVED
                    and not await self._is_maintenance_active_unlocked(
                        connection, row["root_asset_id"], at
                    )
                    and await self._notifications_may_queue_unlocked(connection)
                ),
            )
        return True

    async def active_incidents_containing(self, asset_id: str) -> list[IncidentView]:
        rows = list(
            await (
                await self.conn.execute(
                    """
                SELECT i.id FROM incidents i
                JOIN incident_assets ia ON ia.incident_id=i.id
                WHERE ia.asset_id=? AND i.state IN ('open', 'recovering')
                ORDER BY i.opened_at, i.id
                """,
                    (asset_id,),
                )
            ).fetchall()
        )
        return [item for row in rows if (item := await self.get_incident(row["id"]))]

    async def incident_assets_healthy(self, incident_id: str, required_successes: int) -> bool:
        row = await (
            await self.conn.execute(
                """
                SELECT COUNT(*) AS unhealthy
                FROM incident_assets ia JOIN asset_state s ON s.asset_id=ia.asset_id
                WHERE ia.incident_id=?
                  AND (s.health != 'healthy' OR s.consecutive_successes < ?)
                """,
                (incident_id, required_successes),
            )
        ).fetchone()
        return bool(row and row["unhealthy"] == 0)

    async def _incident_base(self, incident_id: str) -> dict[str, Any] | None:
        row = await (
            await self.conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,))
        ).fetchone()
        if row is None:
            return None
        asset_rows = await (
            await self.conn.execute(
                "SELECT asset_id FROM incident_assets WHERE incident_id=? ORDER BY asset_id",
                (incident_id,),
            )
        ).fetchall()
        data = dict(row)
        for field in ("opened_at", "acknowledged_at", "recovered_at", "closed_at"):
            data[field] = _dt(data[field])
        data["affected_asset_ids"] = [item["asset_id"] for item in asset_rows]
        return data

    async def get_incident_summary(self, incident_id: str) -> IncidentSummary | None:
        data = await self._incident_base(incident_id)
        return IncidentSummary.model_validate(data) if data else None

    async def get_incident(self, incident_id: str) -> IncidentView | None:
        data = await self._incident_base(incident_id)
        if data is None:
            return None
        event_rows = await (
            await self.conn.execute(
                "SELECT * FROM incident_events WHERE incident_id=? ORDER BY id", (incident_id,)
            )
        ).fetchall()
        note_rows = await (
            await self.conn.execute(
                "SELECT * FROM incident_notes WHERE incident_id=? ORDER BY id", (incident_id,)
            )
        ).fetchall()
        data["events"] = [self._incident_event_from_row(row) for row in event_rows]
        data["notes"] = [
            {
                "id": row["id"],
                "incident_id": row["incident_id"],
                "created_at": datetime.fromisoformat(row["created_at"]),
                "author": row["author"],
                "body": row["body"],
            }
            for row in note_rows
        ]
        return IncidentView.model_validate(data)

    @staticmethod
    def _incident_event_from_row(row: aiosqlite.Row) -> IncidentEventView:
        return IncidentEventView(
            id=row["id"],
            event_uuid=row["event_uuid"],
            incident_id=row["incident_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            kind=row["kind"],
            message=row["message"],
            actor_subject=row["actor_subject"],
            actor_email=row["actor_email"],
            metadata=json.loads(row["metadata_json"]),
        )

    async def list_incident_page(
        self,
        *,
        states: set[IncidentState] | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> IncidentPage:
        state_filter = _json(sorted(str(state) for state in states)) if states else None
        if cursor:
            opened_at, incident_id = _decode_cursor(cursor)
        else:
            opened_at, incident_id = None, None
        parameters: list[Any] = [
            state_filter,
            state_filter,
            opened_at,
            opened_at,
            opened_at,
            incident_id,
            limit + 1,
        ]
        query = """
            SELECT id, opened_at FROM incidents
            WHERE (? IS NULL OR state IN (SELECT value FROM json_each(?)))
              AND (? IS NULL OR opened_at < ? OR (opened_at = ? AND id < ?))
            ORDER BY opened_at DESC, id DESC LIMIT ?
            """
        rows = list(await (await self.conn.execute(query, parameters)).fetchall())
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [item for row in rows if (item := await self.get_incident_summary(row["id"]))]
        next_cursor = None
        if has_more and rows:
            next_cursor = _encode_cursor(rows[-1]["opened_at"], rows[-1]["id"])
        return IncidentPage(items=items, next_cursor=next_cursor)

    async def list_incidents(self, states: set[IncidentState] | None = None) -> list[IncidentView]:
        page = await self.list_incident_page(states=states, limit=200)
        return [item for summary in page.items if (item := await self.get_incident(summary.id))]

    async def incident_timeline(
        self, incident_id: str, *, cursor: int = 0, limit: int = 100
    ) -> TimelinePage:
        rows = list(
            await (
                await self.conn.execute(
                    """
                    SELECT * FROM incident_events
                    WHERE incident_id=? AND id>? ORDER BY id LIMIT ?
                    """,
                    (incident_id, cursor, limit + 1),
                )
            ).fetchall()
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        return TimelinePage(
            items=[self._incident_event_from_row(row) for row in rows],
            next_cursor=str(rows[-1]["id"]) if has_more and rows else None,
        )

    async def _idempotency_unlocked(
        self,
        connection: aiosqlite.Connection,
        *,
        actor_subject: str,
        operation: str,
        key: str,
        request_hash: str,
    ) -> str | None:
        row = await (
            await connection.execute(
                """
                SELECT request_hash, response_json FROM idempotency_records
                WHERE actor_subject=? AND operation=? AND idempotency_key=?
                """,
                (actor_subject, operation, key),
            )
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise IdempotencyConflictError("idempotency key was reused with a different request")
        return str(json.loads(row["response_json"])["subject_id"])

    async def _store_idempotency_unlocked(
        self,
        connection: aiosqlite.Connection,
        *,
        actor_subject: str,
        operation: str,
        key: str,
        request_hash: str,
        subject_id: str,
        at: datetime,
    ) -> None:
        await connection.execute(
            """
            INSERT INTO idempotency_records(
              actor_subject, operation, idempotency_key, request_hash,
              response_json, status_code, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, 200, ?, ?)
            """,
            (
                actor_subject,
                operation,
                key,
                request_hash,
                _json({"subject_id": subject_id}),
                _iso(at),
                _iso(at + timedelta(days=1)),
            ),
        )

    async def acknowledge(
        self,
        incident_id: str,
        actor: str,
        at: datetime,
        *,
        actor_subject: str | None = None,
        expected_version: int | None = None,
        idempotency_key: str | None = None,
    ) -> IncidentView | None:
        subject = actor_subject or actor
        operation = f"incident:{incident_id}:acknowledge"
        request_hash = hashlib.sha256(f"{incident_id}:{expected_version}".encode()).hexdigest()
        async with self._transaction() as connection:
            if idempotency_key:
                replay = await self._idempotency_unlocked(
                    connection,
                    actor_subject=subject,
                    operation=operation,
                    key=idempotency_key,
                    request_hash=request_hash,
                )
                if replay:
                    return await self.get_incident(replay)
            row = await (
                await connection.execute(
                    "SELECT state, version, acknowledged_at FROM incidents WHERE id=?",
                    (incident_id,),
                )
            ).fetchone()
            if row is None:
                return None
            if expected_version is not None and row["version"] != expected_version:
                raise ConflictError("incident version changed")
            if row["state"] not in {"open", "recovering"}:
                raise ConflictError("only active incidents can be acknowledged")
            if row["acknowledged_at"] is None:
                await connection.execute(
                    """
                    UPDATE incidents SET acknowledged_at=?, acknowledged_by=?, version=version+1
                    WHERE id=?
                    """,
                    (_iso(at), actor, incident_id),
                )
                await self._append_incident_event_unlocked(
                    connection,
                    incident_id,
                    "acknowledged",
                    "Incident acknowledged",
                    created_at=at,
                    actor_subject=subject,
                    actor_email=actor,
                )
                await self._append_audit_unlocked(
                    connection,
                    kind="incident_acknowledged",
                    subject_type="incident",
                    subject_id=incident_id,
                    message="Incident acknowledged",
                    at=at,
                    actor_subject=subject,
                    actor_email=actor,
                )
            if idempotency_key:
                await self._store_idempotency_unlocked(
                    connection,
                    actor_subject=subject,
                    operation=operation,
                    key=idempotency_key,
                    request_hash=request_hash,
                    subject_id=incident_id,
                    at=at,
                )
        return await self.get_incident(incident_id)

    async def add_note(
        self,
        incident_id: str,
        actor: str,
        body: str,
        at: datetime,
        *,
        actor_subject: str | None = None,
        expected_version: int | None = None,
        idempotency_key: str | None = None,
    ) -> IncidentView | None:
        subject = actor_subject or actor
        clean_body = body.strip()
        operation = f"incident:{incident_id}:note"
        request_hash = hashlib.sha256(
            f"{incident_id}:{expected_version}:{clean_body}".encode()
        ).hexdigest()
        async with self._transaction() as connection:
            if idempotency_key:
                replay = await self._idempotency_unlocked(
                    connection,
                    actor_subject=subject,
                    operation=operation,
                    key=idempotency_key,
                    request_hash=request_hash,
                )
                if replay:
                    return await self.get_incident(replay)
            row = await (
                await connection.execute(
                    "SELECT state, version FROM incidents WHERE id=?", (incident_id,)
                )
            ).fetchone()
            if row is None:
                return None
            if expected_version is not None and row["version"] != expected_version:
                raise ConflictError("incident version changed")
            if row["state"] not in {"open", "recovering"}:
                raise ConflictError("resolved incidents are immutable")
            await connection.execute(
                """
                INSERT INTO incident_notes(incident_id, created_at, author, body)
                VALUES (?, ?, ?, ?)
                """,
                (incident_id, _iso(at), actor, clean_body),
            )
            await connection.execute(
                "UPDATE incidents SET version=version+1 WHERE id=?", (incident_id,)
            )
            await self._append_incident_event_unlocked(
                connection,
                incident_id,
                "note",
                "Responder note added",
                created_at=at,
                actor_subject=subject,
                actor_email=actor,
            )
            await self._append_audit_unlocked(
                connection,
                kind="incident_note_added",
                subject_type="incident",
                subject_id=incident_id,
                message="Responder note added",
                at=at,
                actor_subject=subject,
                actor_email=actor,
            )
            if idempotency_key:
                await self._store_idempotency_unlocked(
                    connection,
                    actor_subject=subject,
                    operation=operation,
                    key=idempotency_key,
                    request_hash=request_hash,
                    subject_id=incident_id,
                    at=at,
                )
        return await self.get_incident(incident_id)

    async def close_incident(
        self,
        incident_id: str,
        actor: str,
        at: datetime,
        *,
        actor_subject: str | None = None,
        expected_version: int | None = None,
        idempotency_key: str | None = None,
    ) -> IncidentView | None:
        subject = actor_subject or actor
        operation = f"incident:{incident_id}:close"
        request_hash = hashlib.sha256(f"{incident_id}:{expected_version}".encode()).hexdigest()
        async with self._transaction() as connection:
            if idempotency_key:
                replay = await self._idempotency_unlocked(
                    connection,
                    actor_subject=subject,
                    operation=operation,
                    key=idempotency_key,
                    request_hash=request_hash,
                )
                if replay:
                    return await self.get_incident(replay)
            row = await (
                await connection.execute(
                    "SELECT state, version FROM incidents WHERE id=?", (incident_id,)
                )
            ).fetchone()
            if row is None:
                return None
            if expected_version is not None and row["version"] != expected_version:
                raise ConflictError("incident version changed")
            if row["state"] != "resolved":
                raise ConflictError("incident must recover before closure")
            await connection.execute(
                """
                UPDATE incidents SET state='closed', closed_at=?, closed_by=?, version=version+1
                WHERE id=?
                """,
                (_iso(at), actor, incident_id),
            )
            await self._append_incident_event_unlocked(
                connection,
                incident_id,
                "closed",
                "Incident closed",
                created_at=at,
                actor_subject=subject,
                actor_email=actor,
            )
            await self._append_audit_unlocked(
                connection,
                kind="incident_closed",
                subject_type="incident",
                subject_id=incident_id,
                message="Incident closed",
                at=at,
                actor_subject=subject,
                actor_email=actor,
            )
            if idempotency_key:
                await self._store_idempotency_unlocked(
                    connection,
                    actor_subject=subject,
                    operation=operation,
                    key=idempotency_key,
                    request_hash=request_hash,
                    subject_id=incident_id,
                    at=at,
                )
        return await self.get_incident(incident_id)

    async def create_maintenance(
        self,
        *,
        maintenance_id: str,
        asset_ids: list[str],
        starts_at: datetime,
        ends_at: datetime,
        reason: str,
        actor_subject: str,
        actor_email: str,
        at: datetime,
        idempotency_key: str | None = None,
    ) -> MaintenanceWindowView:
        operation = "maintenance:create"
        request_hash = hashlib.sha256(
            _json(
                {
                    "asset_ids": sorted(asset_ids),
                    "starts_at": _iso(starts_at),
                    "ends_at": _iso(ends_at),
                    "reason": reason,
                }
            ).encode()
        ).hexdigest()
        async with self._transaction() as connection:
            if idempotency_key:
                replay = await self._idempotency_unlocked(
                    connection,
                    actor_subject=actor_subject,
                    operation=operation,
                    key=idempotency_key,
                    request_hash=request_hash,
                )
                if replay:
                    value = await self.get_maintenance(replay)
                    if value is None:
                        raise RuntimeError("idempotency record references missing maintenance")
                    return value
            known_rows = await (
                await connection.execute("SELECT id FROM assets WHERE retired_at IS NULL")
            ).fetchall()
            if not set(asset_ids) <= {row["id"] for row in known_rows}:
                raise KeyError("maintenance references an unknown or retired asset")
            await connection.execute(
                """
                INSERT INTO maintenance_windows(
                  id, starts_at, ends_at, reason, created_at, created_by
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    maintenance_id,
                    _iso(starts_at),
                    _iso(ends_at),
                    reason,
                    _iso(at),
                    actor_email,
                ),
            )
            await connection.executemany(
                "INSERT INTO maintenance_assets(maintenance_id, asset_id) VALUES (?, ?)",
                [(maintenance_id, asset_id) for asset_id in sorted(asset_ids)],
            )
            await self._append_audit_unlocked(
                connection,
                kind="maintenance_created",
                subject_type="maintenance",
                subject_id=maintenance_id,
                message="Maintenance window created",
                at=at,
                actor_subject=actor_subject,
                actor_email=actor_email,
                metadata={
                    "asset_ids": sorted(asset_ids),
                    "starts_at": _iso(starts_at),
                    "ends_at": _iso(ends_at),
                },
            )
            await self._append_stream_unlocked(
                connection,
                topic="maintenance",
                kind="created",
                subject_id=maintenance_id,
                payload={"asset_ids": sorted(asset_ids)},
                at=at,
            )
            if idempotency_key:
                await self._store_idempotency_unlocked(
                    connection,
                    actor_subject=actor_subject,
                    operation=operation,
                    key=idempotency_key,
                    request_hash=request_hash,
                    subject_id=maintenance_id,
                    at=at,
                )
        value = await self.get_maintenance(maintenance_id)
        if value is None:
            raise RuntimeError("created maintenance window could not be read back")
        return value

    async def cancel_maintenance(
        self,
        maintenance_id: str,
        *,
        actor_subject: str,
        actor_email: str,
        expected_version: int,
        at: datetime,
        idempotency_key: str | None = None,
    ) -> MaintenanceWindowView | None:
        operation = f"maintenance:{maintenance_id}:cancel"
        request_hash = hashlib.sha256(f"{maintenance_id}:{expected_version}".encode()).hexdigest()
        async with self._transaction() as connection:
            if idempotency_key:
                replay = await self._idempotency_unlocked(
                    connection,
                    actor_subject=actor_subject,
                    operation=operation,
                    key=idempotency_key,
                    request_hash=request_hash,
                )
                if replay:
                    return await self.get_maintenance(replay)
            row = await (
                await connection.execute(
                    "SELECT version, cancelled_at FROM maintenance_windows WHERE id=?",
                    (maintenance_id,),
                )
            ).fetchone()
            if row is None:
                return None
            if row["version"] != expected_version:
                raise ConflictError("maintenance version changed")
            if row["cancelled_at"] is None:
                await connection.execute(
                    """
                    UPDATE maintenance_windows SET cancelled_at=?, cancelled_by=?,
                      version=version+1 WHERE id=?
                    """,
                    (_iso(at), actor_email, maintenance_id),
                )
                await self._append_audit_unlocked(
                    connection,
                    kind="maintenance_cancelled",
                    subject_type="maintenance",
                    subject_id=maintenance_id,
                    message="Maintenance window cancelled",
                    at=at,
                    actor_subject=actor_subject,
                    actor_email=actor_email,
                )
                await self._append_stream_unlocked(
                    connection,
                    topic="maintenance",
                    kind="cancelled",
                    subject_id=maintenance_id,
                    payload={},
                    at=at,
                )
            if idempotency_key:
                await self._store_idempotency_unlocked(
                    connection,
                    actor_subject=actor_subject,
                    operation=operation,
                    key=idempotency_key,
                    request_hash=request_hash,
                    subject_id=maintenance_id,
                    at=at,
                )
        return await self.get_maintenance(maintenance_id)

    async def _is_maintenance_active_unlocked(
        self, connection: aiosqlite.Connection, asset_id: str, at: datetime
    ) -> bool:
        row = await (
            await connection.execute(
                """
                SELECT 1 FROM maintenance_windows w
                JOIN maintenance_assets a ON a.maintenance_id=w.id
                WHERE a.asset_id=? AND w.cancelled_at IS NULL
                  AND w.starts_at<=? AND w.ends_at>?
                LIMIT 1
                """,
                (asset_id, _iso(at), _iso(at)),
            )
        ).fetchone()
        return row is not None

    async def is_maintenance_active(self, asset_id: str, at: datetime) -> bool:
        return await self._is_maintenance_active_unlocked(self.conn, asset_id, at)

    async def get_maintenance(self, maintenance_id: str) -> MaintenanceWindowView | None:
        row = await (
            await self.conn.execute(
                "SELECT * FROM maintenance_windows WHERE id=?", (maintenance_id,)
            )
        ).fetchone()
        if row is None:
            return None
        assets = await (
            await self.conn.execute(
                "SELECT asset_id FROM maintenance_assets WHERE maintenance_id=? ORDER BY asset_id",
                (maintenance_id,),
            )
        ).fetchall()
        return MaintenanceWindowView(
            id=row["id"],
            asset_ids=[item["asset_id"] for item in assets],
            starts_at=datetime.fromisoformat(row["starts_at"]),
            ends_at=datetime.fromisoformat(row["ends_at"]),
            reason=row["reason"],
            created_at=datetime.fromisoformat(row["created_at"]),
            created_by=row["created_by"],
            cancelled_at=_dt(row["cancelled_at"]),
            cancelled_by=row["cancelled_by"],
            version=row["version"],
        )

    async def list_maintenance(
        self, *, include_expired: bool = False
    ) -> list[MaintenanceWindowView]:
        parameters = (include_expired, _iso(datetime.now(UTC)))
        rows = await (
            await self.conn.execute(
                """
                SELECT id FROM maintenance_windows
                WHERE (? OR ends_at>=?) ORDER BY starts_at DESC
                """,
                parameters,
            )
        ).fetchall()
        return [item for row in rows if (item := await self.get_maintenance(row["id"]))]

    async def list_samples(
        self, asset_id: str, since: datetime, limit: int = 500
    ) -> list[Observation]:
        rows = await (
            await self.conn.execute(
                """
                SELECT asset_id, check_id, provider, provider_run_id, observed_at,
                       health, condition, message, latency_ms, cpu_ratio,
                       memory_ratio, disk_ratio, details_json
                FROM samples WHERE asset_id=? AND observed_at>=?
                ORDER BY observed_at ASC LIMIT ?
                """,
                (asset_id, _iso(since), limit),
            )
        ).fetchall()
        return [
            Observation(
                asset_id=row["asset_id"],
                check_id=row["check_id"],
                provider=row["provider"],
                provider_run_id=row["provider_run_id"],
                observed_at=datetime.fromisoformat(row["observed_at"]),
                health=row["health"],
                condition=row["condition"],
                message=row["message"],
                latency_ms=row["latency_ms"],
                cpu_ratio=row["cpu_ratio"],
                memory_ratio=row["memory_ratio"],
                disk_ratio=row["disk_ratio"],
                details=json.loads(row["details_json"]),
            )
            for row in rows
        ]

    async def list_hourly_rollups(self, asset_id: str, since: datetime) -> list[dict[str, Any]]:
        rows = await (
            await self.conn.execute(
                """
                SELECT * FROM hourly_rollups
                WHERE asset_id=? AND bucket_at>=? ORDER BY bucket_at
                """,
                (asset_id, _iso(since)),
            )
        ).fetchall()
        return [dict(row) for row in rows]

    async def latest_event_id(self) -> int:
        row = await (
            await self.conn.execute("SELECT COALESCE(MAX(id), 0) AS id FROM stream_events")
        ).fetchone()
        return int(row["id"] if row else 0)

    async def events_after(self, event_id: int, limit: int = 100) -> list[IncidentEventView]:
        rows = await (
            await self.conn.execute(
                "SELECT * FROM incident_events WHERE id>? ORDER BY id LIMIT ?", (event_id, limit)
            )
        ).fetchall()
        return [self._incident_event_from_row(row) for row in rows]

    async def stream_events_after(self, event_id: int, limit: int = 100) -> list[StreamEventView]:
        rows = await (
            await self.conn.execute(
                "SELECT * FROM stream_events WHERE id>? ORDER BY id LIMIT ?", (event_id, limit)
            )
        ).fetchall()
        return [
            StreamEventView(
                id=row["id"],
                event_uuid=row["event_uuid"],
                created_at=datetime.fromisoformat(row["created_at"]),
                topic=row["topic"],
                kind=row["kind"],
                subject_id=row["subject_id"],
                payload=json.loads(row["payload_json"]),
            )
            for row in rows
        ]

    async def set_runtime_value(self, key: str, value: str) -> None:
        async with self._transaction() as connection:
            await connection.execute(
                """
                INSERT INTO runtime_state(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, value),
            )

    async def get_runtime_value(self, key: str) -> str | None:
        row = await (
            await self.conn.execute("SELECT value FROM runtime_state WHERE key=?", (key,))
        ).fetchone()
        return str(row["value"]) if row else None

    async def _notifications_may_queue_unlocked(self, connection: aiosqlite.Connection) -> bool:
        row = await (
            await connection.execute(
                "SELECT value FROM runtime_state WHERE key='notification_enabled'"
            )
        ).fetchone()
        return row is None or str(row["value"]).lower() != "false"

    async def record_notification_heartbeat(
        self,
        *,
        enabled: bool,
        at: datetime,
        success: bool = False,
    ) -> int:
        """Record notifier state and suppress disabled delivery atomically."""

        async with self._transaction() as connection:
            await connection.execute(
                """
                INSERT INTO runtime_state(key, value) VALUES ('notification_enabled', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                ("true" if enabled else "false",),
            )
            if success:
                await connection.execute(
                    """
                    INSERT INTO runtime_state(key, value)
                    VALUES ('notification_last_success_at', ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value
                    """,
                    (_iso(at),),
                )
            suppressed = 0
            if not enabled:
                cursor = await connection.execute(
                    """
                    UPDATE notification_outbox
                    SET suppressed_at=?, diagnostic='notifications_disabled'
                    WHERE delivered_at IS NULL
                      AND dead_letter_at IS NULL
                      AND suppressed_at IS NULL
                    """,
                    (_iso(at),),
                )
                suppressed = max(0, int(cursor.rowcount))
                if suppressed:
                    await self._append_audit_unlocked(
                        connection,
                        kind="notifications_suppressed",
                        subject_type="notification_outbox",
                        subject_id=None,
                        message="Undelivered notifications suppressed because delivery is disabled",
                        at=at,
                        metadata={"count": suppressed},
                    )
                    await self._append_stream_unlocked(
                        connection,
                        topic="notification",
                        kind="suppressed",
                        subject_id=None,
                        payload={"count": suppressed},
                        at=at,
                    )
            return suppressed

    async def notification_status(self, *, enabled: bool) -> dict[str, Any]:
        row = await (
            await self.conn.execute(
                """
                SELECT
                  SUM(CASE WHEN delivered_at IS NULL AND dead_letter_at IS NULL
                                AND suppressed_at IS NULL
                           THEN 1 ELSE 0 END) pending,
                  SUM(CASE WHEN delivered_at IS NOT NULL THEN 1 ELSE 0 END) delivered,
                  SUM(CASE WHEN dead_letter_at IS NOT NULL THEN 1 ELSE 0 END) dead_letter,
                  SUM(CASE WHEN suppressed_at IS NOT NULL THEN 1 ELSE 0 END) suppressed,
                  MAX(delivered_at) last_success_at
                FROM notification_outbox
                """
            )
        ).fetchone()
        if row is None:
            return {
                "enabled": enabled,
                "pending": 0,
                "delivered": 0,
                "dead_letter": 0,
                "suppressed": 0,
                "last_success_at": None,
            }
        return {
            "enabled": enabled,
            "pending": int(row["pending"] or 0),
            "delivered": int(row["delivered"] or 0),
            "dead_letter": int(row["dead_letter"] or 0),
            "suppressed": int(row["suppressed"] or 0),
            "last_success_at": _dt(row["last_success_at"]),
        }

    async def due_notifications(self, at: datetime, limit: int = 20) -> list[dict[str, Any]]:
        rows = await (
            await self.conn.execute(
                """
                SELECT event_uuid, incident_id, event_kind, payload_json, attempt_count
                FROM notification_outbox
                WHERE delivered_at IS NULL AND dead_letter_at IS NULL
                  AND suppressed_at IS NULL AND next_attempt_at<=?
                ORDER BY created_at LIMIT ?
                """,
                (_iso(at), limit),
            )
        ).fetchall()
        return [
            {
                "event_uuid": row["event_uuid"],
                "incident_id": row["incident_id"],
                "event_kind": row["event_kind"],
                "payload": json.loads(row["payload_json"]),
                "attempt_count": row["attempt_count"],
            }
            for row in rows
        ]

    async def mark_notification(
        self,
        event_uuid: str,
        *,
        delivered: bool,
        at: datetime,
        diagnostic: str = "",
        max_attempts: int = 8,
    ) -> None:
        async with self._transaction() as connection:
            row = await (
                await connection.execute(
                    """
                    SELECT attempt_count FROM notification_outbox
                    WHERE event_uuid=? AND delivered_at IS NULL
                      AND dead_letter_at IS NULL AND suppressed_at IS NULL
                    """,
                    (event_uuid,),
                )
            ).fetchone()
            if row is None:
                return
            attempts = int(row["attempt_count"]) + 1
            if delivered:
                await connection.execute(
                    """
                    UPDATE notification_outbox SET attempt_count=?, delivered_at=?, diagnostic=NULL
                    WHERE event_uuid=?
                    """,
                    (attempts, _iso(at), event_uuid),
                )
                kind = "delivered"
            elif attempts >= max_attempts:
                await connection.execute(
                    """
                    UPDATE notification_outbox SET attempt_count=?, dead_letter_at=?, diagnostic=?
                    WHERE event_uuid=?
                    """,
                    (attempts, _iso(at), diagnostic[:240], event_uuid),
                )
                kind = "dead_letter"
            else:
                delays = (60, 300, 900, 1800, 3600, 7200, 14400)
                delay = delays[min(attempts - 1, len(delays) - 1)]
                await connection.execute(
                    """
                    UPDATE notification_outbox SET attempt_count=?, next_attempt_at=?, diagnostic=?
                    WHERE event_uuid=?
                    """,
                    (attempts, _iso(at + timedelta(seconds=delay)), diagnostic[:240], event_uuid),
                )
                kind = "retry_scheduled"
            await self._append_stream_unlocked(
                connection,
                topic="notification",
                kind=kind,
                subject_id=event_uuid,
                payload={"attempt_count": attempts},
                at=at,
            )

    async def cleanup(
        self,
        sample_days: int,
        incident_days: int,
        now: datetime,
        rollup_days: int = 180,
    ) -> None:
        sample_cutoff = now - timedelta(days=sample_days)
        rollup_cutoff = now - timedelta(days=rollup_days)
        incident_cutoff = now - timedelta(days=incident_days)
        async with self._transaction() as connection:
            await connection.execute(
                """
                INSERT INTO hourly_rollups(
                  asset_id, bucket_at, sample_count, healthy_count,
                  cpu_ratio_avg, memory_ratio_avg, disk_ratio_avg, latency_ms_avg
                )
                SELECT asset_id,
                       strftime('%Y-%m-%dT%H:00:00+00:00', observed_at),
                       COUNT(*),
                       SUM(CASE WHEN health='healthy' THEN 1 ELSE 0 END),
                       AVG(cpu_ratio), AVG(memory_ratio), AVG(disk_ratio), AVG(latency_ms)
                FROM samples WHERE observed_at<?
                GROUP BY asset_id, strftime('%Y-%m-%dT%H:00:00+00:00', observed_at)
                ON CONFLICT(asset_id, bucket_at) DO UPDATE SET
                  sample_count=excluded.sample_count,
                  healthy_count=excluded.healthy_count,
                  cpu_ratio_avg=excluded.cpu_ratio_avg,
                  memory_ratio_avg=excluded.memory_ratio_avg,
                  disk_ratio_avg=excluded.disk_ratio_avg,
                  latency_ms_avg=excluded.latency_ms_avg
                """,
                (_iso(sample_cutoff),),
            )
            await connection.execute(
                "DELETE FROM samples WHERE observed_at<?", (_iso(sample_cutoff),)
            )
            await connection.execute(
                "DELETE FROM hourly_rollups WHERE bucket_at<?", (_iso(rollup_cutoff),)
            )
            await connection.execute(
                """
                DELETE FROM notification_outbox
                WHERE created_at<?
                  AND (delivered_at IS NOT NULL
                       OR dead_letter_at IS NOT NULL
                       OR suppressed_at IS NOT NULL)
                """,
                (_iso(incident_cutoff),),
            )
            await connection.execute(
                """
                DELETE FROM incidents
                WHERE (state='closed' AND closed_at<?)
                   OR (state='resolved' AND recovered_at<?)
                """,
                (_iso(incident_cutoff), _iso(incident_cutoff)),
            )
            await connection.execute(
                "DELETE FROM audit_events WHERE created_at<?", (_iso(incident_cutoff),)
            )
            await connection.execute(
                "DELETE FROM stream_events WHERE created_at<?", (_iso(incident_cutoff),)
            )
            await connection.execute(
                "DELETE FROM idempotency_records WHERE expires_at<?", (_iso(now),)
            )
        # Checkpoint must not run inside the write transaction.  Doing so reproduces
        # SQLITE_LOCKED under an active WAL reader.
        cursor = await self.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        await cursor.fetchall()

    async def backup_to(self, destination: Path, retention: int = 14) -> Path:
        if destination.suffix:
            target = destination
            directory = destination.parent
        else:
            directory = destination
            target = directory / f"signal-room-{datetime.now(UTC).date().isoformat()}.sqlite3"
        result = await verified_backup(self.conn, target, full_check=True)
        backups = sorted(
            directory.glob("signal-room-*.sqlite3"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for expired in backups[retention:]:
            expired.unlink(missing_ok=True)
            expired.with_suffix(expired.suffix + ".sha256").unlink(missing_ok=True)
        return result

    async def readiness(self) -> tuple[bool, str]:
        try:
            row = await (await self.conn.execute("PRAGMA quick_check")).fetchone()
            version = await (
                await self.conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations")
            ).fetchone()
            if not row or row[0] != "ok":
                return False, "database quick_check failed"
            if not version or int(version[0]) != CURRENT_SCHEMA_VERSION:
                return False, "database schema is not current"
            return True, "ready"
        except (sqlite3.DatabaseError, aiosqlite.Error):
            return False, "database is unavailable"
