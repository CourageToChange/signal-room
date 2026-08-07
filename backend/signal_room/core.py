from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from uuid import uuid4

from .config import AppSettings, load_runbooks, load_topology
from .db import ConflictError, Database, IdempotencyConflictError
from .engine import IncidentEngine
from .migrate import CURRENT_SCHEMA_VERSION
from .models import (
    AssetDetailResponse,
    BootstrapResponse,
    Capabilities,
    DiagnosticsResponse,
    HealthState,
    IncidentPage,
    IncidentState,
    IncidentView,
    MaintenanceCreateRequest,
    MetricBucket,
    MetricsResponse,
    MetricThresholds,
    NotificationStatus,
    Observation,
    ProviderKind,
    ProviderStateView,
    StreamEventView,
    TimelinePage,
    utc_now,
)

CoreRole = Literal["query", "ingest", "notifier", "maintenance"]
MAX_RPC_BYTES = 1_048_576
PRODUCTION_SOCKET_GROUPS: dict[CoreRole, str] = {
    "query": "signal-room-query",
    "ingest": "signal-room-ingest",
    "notifier": "signal-room-notify",
    "maintenance": "signal-room-core",
}


class CoreUnavailableError(RuntimeError):
    pass


class CoreRequestError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CoreTransport(Protocol):
    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any: ...


class CoreClient:
    def __init__(self, socket_path: Path, timeout_seconds: float = 10) -> None:
        self.socket_path = socket_path
        self.timeout_seconds = timeout_seconds

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        request_id = str(uuid4())
        payload = (
            json.dumps(
                {"id": request_id, "method": method, "params": params or {}},
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )
        if len(payload) > MAX_RPC_BYTES:
            raise CoreRequestError("request_too_large", "core request exceeds the size limit")
        try:
            asyncio_unix = cast(Any, asyncio)
            open_unix_connection = asyncio_unix.open_unix_connection
            reader, writer = await asyncio.wait_for(
                open_unix_connection(str(self.socket_path), limit=MAX_RPC_BYTES),
                timeout=self.timeout_seconds,
            )
            try:
                writer.write(payload)
                await writer.drain()
                raw = await asyncio.wait_for(reader.readline(), timeout=self.timeout_seconds)
            finally:
                writer.close()
                await writer.wait_closed()
        except (OSError, TimeoutError) as error:
            raise CoreUnavailableError("Signal Room core is unavailable") from error
        except ValueError as error:
            # readline() raises ValueError when a response line exceeds the reader's
            # limit. With limit=MAX_RPC_BYTES the client now matches the server, so any
            # such overrun means the response is genuinely too large; fail closed as a
            # clean unavailability instead of letting it surface as an unhandled 500.
            raise CoreUnavailableError(
                "Signal Room core returned an oversized response"
            ) from error
        if not raw or len(raw) > MAX_RPC_BYTES:
            raise CoreUnavailableError("Signal Room core returned an invalid response")
        try:
            response = json.loads(raw)
        except json.JSONDecodeError as error:
            raise CoreUnavailableError("Signal Room core returned invalid JSON") from error
        if response.get("id") != request_id:
            raise CoreUnavailableError("Signal Room core response did not match the request")
        if "error" in response:
            error_payload = response["error"]
            raise CoreRequestError(
                str(error_payload.get("code", "core_error")),
                str(error_payload.get("message")),
            )
        return response.get("result")


class InProcessCore:
    """Explicit test/fixture adapter; production roles always use Unix sockets."""

    def __init__(self, service: CoreService, role: CoreRole = "query") -> None:
        self.service = service
        self.role = role

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        try:
            return await self.service.dispatch(self.role, method, params or {})
        except CoreRequestError:
            raise
        except KeyError as error:
            raise CoreRequestError("not_found", "requested subject was not found") from error
        except IdempotencyConflictError as error:
            raise CoreRequestError("idempotency_conflict", str(error)) from error
        except ConflictError as error:
            raise CoreRequestError("conflict", str(error)) from error


class CoreService:
    QUERY_METHODS = {
        "bootstrap",
        "asset_detail",
        "metrics",
        "incident_page",
        "incident",
        "timeline",
        "acknowledge",
        "note",
        "close",
        "maintenance_list",
        "maintenance_create",
        "maintenance_cancel",
        "diagnostics",
        "stream_events",
        "readiness",
    }
    INGEST_METHODS = {"ingest_batch", "collector_heartbeat"}
    NOTIFIER_METHODS = {
        "notifications_due",
        "notification_mark",
        "notification_heartbeat",
    }
    MAINTENANCE_METHODS = {"backup"}

    def __init__(self, settings: AppSettings, database: Database | None = None) -> None:
        self.settings = settings
        self.topology = load_topology(settings.config_path)
        self.runbooks = load_runbooks(settings.runbooks_path)
        self.database = database or Database(settings.db_path)
        self.engine = IncidentEngine(self.database, self.topology.assets, self.topology.thresholds)
        self._servers: list[asyncio.AbstractServer] = []
        self._cleanup_task: asyncio.Task[None] | None = None

    async def open(self) -> None:
        await self.database.connect()
        await self.database.sync_assets(self.topology.assets, self.topology.revision)

    async def close(self) -> None:
        if self._cleanup_task:
            self._cleanup_task.cancel()
            await asyncio.gather(self._cleanup_task, return_exceptions=True)
            self._cleanup_task = None
        for server in self._servers:
            server.close()
        await asyncio.gather(*(server.wait_closed() for server in self._servers))
        self._servers.clear()
        for path in (
            self.settings.query_socket,
            self.settings.ingest_socket,
            self.settings.notifier_socket,
            self.settings.maintenance_socket,
        ):
            path.unlink(missing_ok=True)
        await self.database.close()

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[CoreService]:
        await self.open()
        try:
            yield self
        finally:
            await self.close()

    async def serve_forever(self) -> None:
        await self.open()
        sockets: tuple[tuple[Path, CoreRole], ...] = (
            (self.settings.query_socket, "query"),
            (self.settings.ingest_socket, "ingest"),
            (self.settings.notifier_socket, "notifier"),
            (self.settings.maintenance_socket, "maintenance"),
        )
        for path, role in sockets:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.unlink(missing_ok=True)
            asyncio_unix = cast(Any, asyncio)
            start_unix_server = asyncio_unix.start_unix_server
            server = await start_unix_server(
                lambda reader, writer, selected=role: self._handle(selected, reader, writer),
                path=str(path),
                limit=MAX_RPC_BYTES,
            )
            self._secure_socket(path, role)
            self._servers.append(server)
        self._cleanup_task = asyncio.create_task(self._maintenance_loop())
        try:
            await asyncio.gather(*(server.serve_forever() for server in self._servers))
        finally:
            await self.close()

    def _secure_socket(self, path: Path, role: CoreRole) -> None:
        if self.settings.environment == "production":
            shutil.chown(path, group=PRODUCTION_SOCKET_GROUPS[role])
        # The core-created Unix socket is intentionally shared with exactly one
        # role-specific group; its parent directory is root-owned and mode 0750.
        os.chmod(path, 0o660)  # nosec B103

    async def _maintenance_loop(self) -> None:
        while True:
            await self.database.cleanup(
                self.settings.retention_sample_days,
                self.settings.retention_incident_days,
                utc_now(),
                self.settings.retention_rollup_days,
            )
            await asyncio.sleep(3600)

    async def _handle(
        self, role: CoreRole, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        response: dict[str, Any]
        request_id: str | None = None
        try:
            raw = await reader.readline()
            if not raw or len(raw) > MAX_RPC_BYTES:
                raise CoreRequestError("request_too_large", "core request is too large")
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise CoreRequestError("invalid_request", "core request must be an object")
            request_id = str(request.get("id", ""))
            method = str(request.get("method", ""))
            params = request.get("params", {})
            if not request_id or not isinstance(params, dict):
                raise CoreRequestError("invalid_request", "core request fields are invalid")
            result = await self.dispatch(role, method, params)
            response = {"id": request_id, "result": result}
        except CoreRequestError as error:
            response = {"id": request_id, "error": {"code": error.code, "message": str(error)}}
        except json.JSONDecodeError:
            response = {
                "id": request_id,
                "error": {"code": "invalid_json", "message": "core request is invalid JSON"},
            }
        except KeyError:
            response = {
                "id": request_id,
                "error": {"code": "not_found", "message": "requested subject was not found"},
            }
        except IdempotencyConflictError as error:
            response = {
                "id": request_id,
                "error": {"code": "idempotency_conflict", "message": str(error)},
            }
        except ConflictError as error:
            response = {"id": request_id, "error": {"code": "conflict", "message": str(error)}}
        except Exception:  # noqa: BLE001 -- the boundary deliberately redacts internals
            response = {
                "id": request_id,
                "error": {"code": "internal_error", "message": "core request failed"},
            }
        encoded = json.dumps(response, separators=(",", ":"), default=str).encode() + b"\n"
        if len(encoded) > MAX_RPC_BYTES:
            encoded = (
                json.dumps(
                    {
                        "id": request_id,
                        "error": {
                            "code": "response_too_large",
                            "message": "core response is too large",
                        },
                    },
                    separators=(",", ":"),
                ).encode()
                + b"\n"
            )
        writer.write(encoded)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def dispatch(self, role: CoreRole, method: str, params: dict[str, Any]) -> Any:
        allowed = {
            "query": self.QUERY_METHODS,
            "ingest": self.INGEST_METHODS,
            "notifier": self.NOTIFIER_METHODS,
            "maintenance": self.MAINTENANCE_METHODS,
        }[role]
        if method not in allowed:
            raise CoreRequestError("forbidden_method", "method is not permitted on this socket")
        handler = cast(Any, getattr(self, f"_rpc_{method}"))
        result = await handler(params)
        if hasattr(result, "model_dump"):
            return result.model_dump(mode="json")
        if isinstance(result, list):
            return [
                item.model_dump(mode="json") if hasattr(item, "model_dump") else item
                for item in result
            ]
        return result

    def _provider_interval(self, provider: ProviderKind) -> int:
        return {
            ProviderKind.FIXTURE: self.topology.polling.fixture_seconds,
            ProviderKind.PROXMOX: self.topology.polling.proxmox_seconds,
            ProviderKind.BACKUP: self.topology.polling.backup_seconds,
            ProviderKind.HTTPS: self.topology.polling.https_seconds,
            ProviderKind.TLS: self.topology.polling.tls_seconds,
        }[provider]

    async def _provider_views(self, now: datetime) -> list[ProviderStateView]:
        values = await self.database.list_provider_states()
        configured = {check.provider for asset in self.topology.assets for check in asset.checks}
        by_provider = {value.provider: value for value in values}
        views: list[ProviderStateView] = []
        for provider in sorted(configured, key=str):
            value = by_provider.get(provider, ProviderStateView(provider=provider))
            stale = (
                value.last_success_at is None
                or (now - value.last_success_at).total_seconds()
                > self._provider_interval(provider) * 3
            )
            if stale and value.status != "failed":
                value = value.model_copy(update={"status": "stale"})
            views.append(value)
        return views

    async def _enrich_incident(self, incident: IncidentView) -> IncidentView:
        asset = next(
            (item for item in self.topology.assets if item.id == incident.root_asset_id), None
        )
        runbook = (
            self.runbooks.runbooks.get(asset.runbook_id) if asset and asset.runbook_id else None
        )
        return incident.model_copy(update={"runbook": runbook})

    async def _rpc_bootstrap(self, _: dict[str, Any]) -> BootstrapResponse:
        now = utc_now()
        heartbeat_raw = await self.database.get_runtime_value("collector_last_seen_at")
        heartbeat = datetime.fromisoformat(heartbeat_raw) if heartbeat_raw else None
        providers = await self._provider_views(now)
        stale = (
            heartbeat is None
            or (now - heartbeat).total_seconds() > 60
            or any(item.status in {"never", "failed", "stale"} for item in providers)
        )
        incident_page = await self.database.list_incident_page(
            states={IncidentState.OPEN, IncidentState.RECOVERING}, limit=20
        )
        return BootstrapResponse(
            build_sha=self.settings.build_sha,
            generated_at=now,
            collector_last_seen_at=heartbeat,
            stale=stale,
            assets=await self.database.list_assets(),
            states=await self.database.list_states(),
            providers=providers,
            incidents=incident_page.items,
            capabilities=Capabilities(
                can_mutate=True,
                drill_available=True,
                data_source=self.settings.mode,
            ),
            last_event_id=await self.database.latest_event_id(),
        )

    async def _rpc_asset_detail(self, params: dict[str, Any]) -> AssetDetailResponse:
        asset_id = str(params["asset_id"])
        asset = await self.database.get_asset(asset_id)
        if asset is None:
            raise KeyError(asset_id)
        active = await self.database.active_incidents_containing(asset_id)
        return AssetDetailResponse(
            asset=asset,
            state=await self.database.get_state(asset_id),
            active_incidents=[item for item in active],
        )

    async def _rpc_metrics(self, params: dict[str, Any]) -> MetricsResponse:
        asset_id = str(params["asset_id"])
        if await self.database.get_asset(asset_id) is None:
            raise KeyError(asset_id)
        range_name = str(params.get("range", "1h"))
        resolution = str(params.get("resolution", "auto"))
        ranges = {
            "1h": timedelta(hours=1),
            "24h": timedelta(hours=24),
            "7d": timedelta(days=7),
            "30d": timedelta(days=30),
            "180d": timedelta(days=180),
        }
        defaults = {"1h": "raw", "24h": "5m", "7d": "1h", "30d": "1h", "180d": "1d"}
        if range_name not in ranges:
            raise CoreRequestError("invalid_parameter", "unsupported metrics range")
        if resolution == "auto":
            resolution = defaults[range_name]
        seconds = {"raw": 0, "5m": 300, "1h": 3600, "1d": 86_400}
        if resolution not in seconds:
            raise CoreRequestError("invalid_parameter", "unsupported metrics resolution")
        now = utc_now()
        since = now - ranges[range_name]
        samples = await self.database.list_samples(asset_id, since, limit=100_000)
        grouped: dict[datetime, list[Observation]] = defaultdict(list)
        interval = seconds[resolution]
        for sample in samples:
            if interval == 0:
                bucket_at = sample.observed_at
            else:
                timestamp = int(sample.observed_at.timestamp())
                bucket_at = datetime.fromtimestamp(timestamp - timestamp % interval, tz=UTC)
            grouped[bucket_at].append(sample)
        if range_name in {"30d", "180d"}:
            for row in await self.database.list_hourly_rollups(asset_id, since):
                bucket_at = datetime.fromisoformat(row["bucket_at"])
                grouped.setdefault(bucket_at, []).append(
                    Observation(
                        asset_id=asset_id,
                        observed_at=bucket_at,
                        health=(
                            HealthState.HEALTHY
                            if row["healthy_count"] == row["sample_count"]
                            else HealthState.DEGRADED
                        ),
                        message="Hourly rollup",
                        cpu_ratio=row["cpu_ratio_avg"],
                        memory_ratio=row["memory_ratio_avg"],
                        disk_ratio=row["disk_ratio_avg"],
                        latency_ms=row["latency_ms_avg"],
                    )
                )
        check_count = len((await self.database.get_asset(asset_id)).check_ids)  # type: ignore[union-attr]
        poll_seconds = min(
            self._provider_interval(check.provider)
            for asset in self.topology.assets
            if asset.id == asset_id
            for check in asset.checks
        )
        bucket_seconds = interval or poll_seconds
        expected_per_bucket = max(1, round(bucket_seconds / poll_seconds) * check_count)

        def average(values: list[float | None]) -> float | None:
            present = [value for value in values if value is not None]
            return sum(present) / len(present) if present else None

        buckets: list[MetricBucket] = []
        for started_at, values in sorted(grouped.items()):
            worst = max(
                values,
                key=lambda item: {
                    HealthState.HEALTHY: 0,
                    HealthState.DEGRADED: 1,
                    HealthState.UNKNOWN: 2,
                    HealthState.DOWN: 3,
                }[item.health],
            )
            buckets.append(
                MetricBucket(
                    started_at=started_at,
                    ended_at=started_at + timedelta(seconds=bucket_seconds),
                    sample_count=len(values),
                    expected_samples=expected_per_bucket,
                    completeness=min(1, len(values) / expected_per_bucket),
                    health=worst.health,
                    cpu_ratio=average([item.cpu_ratio for item in values]),
                    memory_ratio=average([item.memory_ratio for item in values]),
                    disk_ratio=average([item.disk_ratio for item in values]),
                    latency_ms=average([item.latency_ms for item in values]),
                )
            )
        expected_total = max(
            1, int(ranges[range_name].total_seconds() / poll_seconds) * check_count
        )
        thresholds = self.topology.thresholds
        return MetricsResponse(
            asset_id=asset_id,
            range=cast(Any, range_name),
            resolution=cast(Any, resolution),
            generated_at=now,
            completeness=min(1, len(samples) / expected_total),
            thresholds=MetricThresholds(
                cpu_warning_ratio=thresholds.cpu_warning_ratio,
                memory_warning_ratio=thresholds.memory_warning_ratio,
                memory_critical_ratio=thresholds.memory_critical_ratio,
                disk_warning_ratio=thresholds.disk_warning_ratio,
                disk_critical_ratio=thresholds.disk_critical_ratio,
            ),
            buckets=buckets,
        )

    async def _rpc_incident_page(self, params: dict[str, Any]) -> IncidentPage:
        states = params.get("states")
        parsed_states = {IncidentState(item) for item in states} if states else None
        return await self.database.list_incident_page(
            states=parsed_states,
            cursor=params.get("cursor"),
            limit=min(100, max(1, int(params.get("limit", 50)))),
        )

    async def _rpc_incident(self, params: dict[str, Any]) -> IncidentView:
        value = await self.database.get_incident(str(params["incident_id"]))
        if value is None:
            raise KeyError(params["incident_id"])
        return await self._enrich_incident(value)

    async def _rpc_timeline(self, params: dict[str, Any]) -> TimelinePage:
        if await self.database.get_incident_summary(str(params["incident_id"])) is None:
            raise KeyError(params["incident_id"])
        return await self.database.incident_timeline(
            str(params["incident_id"]),
            cursor=int(params.get("cursor") or 0),
            limit=min(200, max(1, int(params.get("limit", 100)))),
        )

    async def _rpc_acknowledge(self, params: dict[str, Any]) -> IncidentView:
        value = await self.database.acknowledge(
            str(params["incident_id"]),
            str(params["actor_email"]),
            utc_now(),
            actor_subject=str(params["actor_subject"]),
            expected_version=int(params["version"]),
            idempotency_key=str(params["idempotency_key"]),
        )
        if value is None:
            raise KeyError(params["incident_id"])
        return await self._enrich_incident(value)

    async def _rpc_note(self, params: dict[str, Any]) -> IncidentView:
        value = await self.database.add_note(
            str(params["incident_id"]),
            str(params["actor_email"]),
            str(params["body"]),
            utc_now(),
            actor_subject=str(params["actor_subject"]),
            expected_version=int(params["version"]),
            idempotency_key=str(params["idempotency_key"]),
        )
        if value is None:
            raise KeyError(params["incident_id"])
        return await self._enrich_incident(value)

    async def _rpc_close(self, params: dict[str, Any]) -> IncidentView:
        value = await self.database.close_incident(
            str(params["incident_id"]),
            str(params["actor_email"]),
            utc_now(),
            actor_subject=str(params["actor_subject"]),
            expected_version=int(params["version"]),
            idempotency_key=str(params["idempotency_key"]),
        )
        if value is None:
            raise KeyError(params["incident_id"])
        return await self._enrich_incident(value)

    async def _rpc_maintenance_list(self, params: dict[str, Any]) -> list[Any]:
        return await self.database.list_maintenance(
            include_expired=bool(params.get("include_expired", False))
        )

    async def _rpc_maintenance_create(self, params: dict[str, Any]) -> Any:
        request = MaintenanceCreateRequest.model_validate(params["maintenance"])
        return await self.database.create_maintenance(
            maintenance_id=str(uuid4()),
            asset_ids=request.asset_ids,
            starts_at=request.starts_at,
            ends_at=request.ends_at,
            reason=request.reason,
            actor_subject=str(params["actor_subject"]),
            actor_email=str(params["actor_email"]),
            at=utc_now(),
            idempotency_key=str(params["idempotency_key"]),
        )

    async def _rpc_maintenance_cancel(self, params: dict[str, Any]) -> Any:
        value = await self.database.cancel_maintenance(
            str(params["maintenance_id"]),
            actor_subject=str(params["actor_subject"]),
            actor_email=str(params["actor_email"]),
            expected_version=int(params["version"]),
            at=utc_now(),
            idempotency_key=str(params["idempotency_key"]),
        )
        if value is None:
            raise KeyError(params["maintenance_id"])
        return value

    async def _rpc_diagnostics(self, params: dict[str, Any]) -> DiagnosticsResponse:
        now = utc_now()
        ready, _ = await self.database.readiness()
        providers = await self._provider_views(now)
        heartbeat = await self.database.get_runtime_value("collector_last_seen_at")
        collector_fresh = bool(
            heartbeat and (now - datetime.fromisoformat(heartbeat)).total_seconds() <= 60
        )
        notification_enabled = (
            await self.database.get_runtime_value("notification_enabled") == "true"
        )
        status = await self.database.notification_status(enabled=notification_enabled)
        return DiagnosticsResponse(
            request_id=str(params.get("request_id", "")),
            build_version="1.0.0",
            build_sha=self.settings.build_sha,
            schema_version=CURRENT_SCHEMA_VERSION,
            configuration_revision=self.topology.revision,
            database_ok=ready,
            collector_fresh=collector_fresh,
            providers=providers,
            notifications=NotificationStatus.model_validate(status),
        )

    async def _rpc_stream_events(self, params: dict[str, Any]) -> list[StreamEventView]:
        return await self.database.stream_events_after(
            int(params.get("after", 0)), min(200, max(1, int(params.get("limit", 100))))
        )

    async def _rpc_readiness(self, _: dict[str, Any]) -> dict[str, Any]:
        database_ok, database_message = await self.database.readiness()
        now = utc_now()
        heartbeat = await self.database.get_runtime_value("collector_last_seen_at")
        heartbeat_ok = bool(
            heartbeat and (now - datetime.fromisoformat(heartbeat)).total_seconds() <= 60
        )
        providers = await self._provider_views(now)
        provider_ok = bool(providers) and all(item.status == "healthy" for item in providers)
        ready = database_ok and heartbeat_ok and provider_ok
        return {
            "ok": ready,
            "database": database_message,
            "collector_fresh": heartbeat_ok,
            "providers_fresh": provider_ok,
        }

    async def _rpc_ingest_batch(self, params: dict[str, Any]) -> dict[str, bool]:
        observations = [Observation.model_validate(item) for item in params.get("observations", [])]
        await self.engine.process_batch(
            provider=ProviderKind(params["provider"]),
            run_id=str(params["run_id"]),
            attempted_at=datetime.fromisoformat(str(params["attempted_at"])),
            completed_at=datetime.fromisoformat(str(params["completed_at"])),
            observations=observations,
            success=bool(params["success"]),
            error_code=params.get("error_code"),
            message=str(params.get("message", "")),
        )
        return {"ok": True}

    async def _rpc_collector_heartbeat(self, params: dict[str, Any]) -> dict[str, bool]:
        await self.database.set_runtime_value(
            "collector_last_seen_at", str(params.get("at") or utc_now().isoformat())
        )
        return {"ok": True}

    async def _rpc_notifications_due(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        return await self.database.due_notifications(
            datetime.fromisoformat(str(params.get("at") or utc_now().isoformat())),
            min(100, max(1, int(params.get("limit", 20)))),
        )

    async def _rpc_notification_mark(self, params: dict[str, Any]) -> dict[str, bool]:
        await self.database.mark_notification(
            str(params["event_uuid"]),
            delivered=bool(params["delivered"]),
            at=datetime.fromisoformat(str(params.get("at") or utc_now().isoformat())),
            diagnostic=str(params.get("diagnostic", "")),
        )
        return {"ok": True}

    async def _rpc_notification_heartbeat(self, params: dict[str, Any]) -> dict[str, bool]:
        at = datetime.fromisoformat(str(params.get("at") or utc_now().isoformat()))
        await self.database.record_notification_heartbeat(
            enabled=bool(params.get("enabled")),
            at=at,
            success=bool(params.get("success")),
        )
        return {"ok": True}

    async def _rpc_backup(self, params: dict[str, Any]) -> dict[str, str]:
        destination = Path(str(params["destination"]))
        result = await self.database.backup_to(
            destination, retention=self.settings.backup_retention_days
        )
        return {"path": str(result)}
