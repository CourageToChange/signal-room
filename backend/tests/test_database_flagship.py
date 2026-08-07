from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from signal_room.db import ConflictError, Database, IdempotencyConflictError
from signal_room.models import (
    CheckDefinition,
    CheckKind,
    HealthState,
    IncidentState,
    IncidentType,
    Observation,
    ProviderKind,
    Severity,
    TopologyConfig,
)

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


class FaultConnection:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.committed = False
        self.rolled_back = False

    async def execute(self, statement: str) -> None:
        if self.error:
            raise self.error

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


async def test_transaction_classifies_full_busy_and_body_failures(tmp_path) -> None:
    database = Database(tmp_path / "fault.sqlite3")
    full = FaultConnection(sqlite3.OperationalError("database or disk is full"))
    database.connection = full  # type: ignore[assignment]
    from signal_room.db import StorageFullError

    with pytest.raises(StorageFullError, match="storage is full"):
        async with database._transaction():
            pass
    assert full.rolled_back

    busy = FaultConnection(sqlite3.OperationalError("database is locked"))
    database.connection = busy  # type: ignore[assignment]
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        async with database._transaction():
            pass
    assert busy.rolled_back

    body = FaultConnection()
    database.connection = body  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="transaction body"):
        async with database._transaction():
            raise RuntimeError("transaction body failed")
    assert body.rolled_back and not body.committed


def sample(
    asset_id: str,
    health: HealthState,
    *,
    at: datetime = NOW,
    check_id: str = "default",
    condition: IncidentType | None = None,
) -> Observation:
    return Observation(
        asset_id=asset_id,
        check_id=check_id,
        observed_at=at,
        health=health,
        condition=condition,
        message=f"{asset_id} is {health}",
        cpu_ratio=0.5,
        memory_ratio=0.6,
        disk_ratio=0.4,
        details={"test": True},
    )


async def open_incident(
    database: Database,
    incident_id: str,
    *,
    root: str = "atlas-node",
    at: datetime = NOW,
    severity: Severity = Severity.WARNING,
) -> object:
    return await database.create_incident(
        incident_id=incident_id,
        root_asset_id=root,
        severity=severity,
        title=f"Incident {incident_id}",
        summary="Confirmed test failure",
        opened_at=at,
        incident_type=IncidentType.ASSET_DOWN,
    )


async def test_provider_batches_are_atomic_ordered_and_idempotent(
    database: Database, topology: TopologyConfig
) -> None:
    with pytest.raises(ValueError, match="duplicate"):
        await database.record_provider_batch(
            provider=ProviderKind.FIXTURE,
            run_id="duplicate-observations",
            attempted_at=NOW,
            observations=[sample("atlas-node", HealthState.HEALTHY)] * 2,
            success=True,
        )

    with pytest.raises(KeyError, match="unknown asset/check"):
        await database.record_provider_batch(
            provider=ProviderKind.FIXTURE,
            run_id="unknown-check",
            attempted_at=NOW,
            observations=[sample("atlas-node", HealthState.HEALTHY, check_id="missing")],
            success=True,
        )

    result = await database.record_provider_batch(
        provider=ProviderKind.FIXTURE,
        run_id="healthy-run",
        attempted_at=NOW,
        completed_at=NOW,
        observations=[sample("atlas-node", HealthState.HEALTHY)],
        success=True,
        message="",
    )
    assert result[0].health == HealthState.HEALTHY
    assert (
        await database.record_provider_batch(
            provider=ProviderKind.FIXTURE,
            run_id="healthy-run",
            attempted_at=NOW,
            completed_at=NOW,
            observations=[sample("atlas-node", HealthState.DOWN)],
            success=True,
        )
        == []
    )

    # A late observation is retained as evidence but cannot rewind current state.
    await database.record_provider_batch(
        provider=ProviderKind.FIXTURE,
        run_id="late-run",
        attempted_at=NOW - timedelta(minutes=1),
        completed_at=NOW - timedelta(minutes=1),
        observations=[sample("atlas-node", HealthState.DOWN, at=NOW - timedelta(minutes=1))],
        success=True,
    )
    assert (await database.get_state("atlas-node")).health == HealthState.HEALTHY

    await database.record_provider_batch(
        provider=ProviderKind.HTTPS,
        run_id="failed-provider",
        attempted_at=NOW,
        observations=[],
        success=False,
        error_code="timeout",
    )
    providers = {item.provider: item for item in await database.list_provider_states()}
    assert providers[ProviderKind.FIXTURE].status == "healthy"
    assert providers[ProviderKind.HTTPS].status == "failed"
    assert len(await database.list_samples("atlas-node", NOW - timedelta(hours=1))) == 2

    with pytest.raises(KeyError):
        await database.get_state("missing")
    assert await database.get_asset("missing") is None
    assert database.conn is not None

    # Configuration changes retire assets and remove obsolete check state without history loss.
    node = topology.assets[0].model_copy(
        update={"checks": [CheckDefinition(id="replacement-check", type=CheckKind.FIXTURE)]}
    )
    await database.sync_assets([node], "replacement", NOW + timedelta(minutes=1))
    assert len(await database.list_assets()) == 1
    assert len(await database.list_assets(include_retired=True)) == len(topology.assets)
    await database.sync_assets(topology.assets, "restored", NOW + timedelta(minutes=2))
    assert len(await database.list_assets()) == len(topology.assets)


async def test_incident_lifecycle_recurrence_pagination_and_immutable_history(
    database: Database,
) -> None:
    incident = await open_incident(database, "incident-a")
    assert await database.find_active_incident("atlas-node") is not None
    assert await database.find_active_root_incident("atlas-node") is not None
    assert await database.previous_incident("atlas-node", IncidentType.ASSET_DOWN) is None
    assert await database.find_active_incident("orchid-guest") is None

    await database.attach_asset("incident-a", "orchid-guest", "Correlated guest", NOW)
    await database.attach_asset("incident-a", "orchid-guest", "Duplicate", NOW)
    assert not await database.escalate_incident("incident-a", Severity.INFO, NOW, "No downgrade")
    assert await database.escalate_incident(
        "incident-a", Severity.CRITICAL, NOW, "Impact increased"
    )
    assert not await database.escalate_incident(
        "incident-a", Severity.CRITICAL, NOW, "Already critical"
    )
    assert not await database.escalate_incident("missing", Severity.CRITICAL, NOW, "Missing")
    await database.add_event("incident-a", "evidence", "Evidence retained", created_at=NOW)
    assert not await database.set_incident_state(
        "missing", IncidentState.RECOVERING, at=NOW, message="Missing"
    )
    assert not await database.set_incident_state(
        "incident-a", IncidentState.OPEN, at=NOW, message="No-op"
    )
    assert await database.set_incident_state(
        "incident-a", IncidentState.RECOVERING, at=NOW, message="Signals improving"
    )
    assert await database.set_incident_state(
        "incident-a", IncidentState.OPEN, at=NOW, message="Fault returned"
    )
    assert await database.set_incident_state(
        "incident-a", IncidentState.RESOLVED, at=NOW, message="Recovered"
    )
    assert not await database.set_incident_state(
        "incident-a", IncidentState.OPEN, at=NOW, message="Cannot reopen"
    )
    previous = await database.previous_incident("atlas-node", IncidentType.ASSET_DOWN)
    assert previous and previous.id == "incident-a"

    recurrence = await open_incident(database, "incident-b", at=NOW + timedelta(hours=1))
    assert recurrence.previous_incident_id == "incident-a"
    assert recurrence.id != incident.id
    assert (await database.active_incidents_containing("atlas-node"))[0].id == "incident-b"

    await open_incident(database, "incident-c", root="orchid-guest", at=NOW + timedelta(hours=2))
    first = await database.list_incident_page(limit=1)
    assert len(first.items) == 1 and first.next_cursor
    second = await database.list_incident_page(cursor=first.next_cursor, limit=1)
    assert len(second.items) == 1
    filtered = await database.list_incident_page(states={IncidentState.RESOLVED}, limit=10)
    assert [item.id for item in filtered.items] == ["incident-a"]
    with pytest.raises(ValueError, match="invalid cursor"):
        await database.list_incident_page(cursor="not-a-cursor")

    timeline = await database.incident_timeline("incident-a", limit=1)
    assert timeline.items and timeline.next_cursor
    next_timeline = await database.incident_timeline(
        "incident-a", cursor=int(timeline.next_cursor), limit=100
    )
    assert next_timeline.items
    assert await database.get_incident("missing") is None
    assert await database.get_incident_summary("missing") is None
    assert await database.events_after(0)
    assert await database.stream_events_after(0)


async def test_versioned_incident_mutations_and_idempotency(database: Database) -> None:
    incident = await open_incident(database, "mutations")
    assert await database.acknowledge("missing", "owner@example.test", NOW) is None
    with pytest.raises(ConflictError, match="version"):
        await database.acknowledge(
            incident.id,
            "owner@example.test",
            NOW,
            expected_version=99,
        )
    acknowledged = await database.acknowledge(
        incident.id,
        "owner@example.test",
        NOW,
        actor_subject="subject-1",
        expected_version=incident.version,
        idempotency_key="ack-key",
    )
    assert acknowledged and acknowledged.acknowledged_by == "owner@example.test"
    replay = await database.acknowledge(
        incident.id,
        "owner@example.test",
        NOW,
        actor_subject="subject-1",
        expected_version=incident.version,
        idempotency_key="ack-key",
    )
    assert replay and replay.version == acknowledged.version
    with pytest.raises(IdempotencyConflictError):
        await database.acknowledge(
            incident.id,
            "owner@example.test",
            NOW,
            actor_subject="subject-1",
            expected_version=acknowledged.version,
            idempotency_key="ack-key",
        )
    second_ack = await database.acknowledge(incident.id, "other@example.test", NOW)
    assert second_ack and second_ack.acknowledged_by == "owner@example.test"

    assert await database.add_note("missing", "owner@example.test", "note", NOW) is None
    with pytest.raises(ConflictError, match="version"):
        await database.add_note(
            incident.id,
            "owner@example.test",
            "note",
            NOW,
            expected_version=99,
        )
    current = await database.get_incident(incident.id)
    assert current
    noted = await database.add_note(
        incident.id,
        "owner@example.test",
        "  Evidence checked  ",
        NOW,
        actor_subject="subject-1",
        expected_version=current.version,
        idempotency_key="note-key",
    )
    assert noted and noted.notes[0].body == "Evidence checked"
    replay_note = await database.add_note(
        incident.id,
        "owner@example.test",
        "Evidence checked",
        NOW,
        actor_subject="subject-1",
        expected_version=current.version,
        idempotency_key="note-key",
    )
    assert replay_note and replay_note.version == noted.version
    with pytest.raises(IdempotencyConflictError):
        await database.add_note(
            incident.id,
            "owner@example.test",
            "Different",
            NOW,
            actor_subject="subject-1",
            expected_version=current.version,
            idempotency_key="note-key",
        )

    with pytest.raises(ConflictError, match="recover"):
        await database.close_incident(incident.id, "owner@example.test", NOW)
    assert await database.close_incident("missing", "owner@example.test", NOW) is None
    assert await database.set_incident_state(
        incident.id, IncidentState.RESOLVED, at=NOW, message="Recovered"
    )
    resolved = await database.get_incident(incident.id)
    assert resolved
    with pytest.raises(ConflictError, match="version"):
        await database.close_incident(incident.id, "owner@example.test", NOW, expected_version=1)
    with pytest.raises(ConflictError, match="active"):
        await database.acknowledge(incident.id, "owner@example.test", NOW)
    with pytest.raises(ConflictError, match="immutable"):
        await database.add_note(incident.id, "owner@example.test", "late", NOW)
    closed = await database.close_incident(
        incident.id,
        "owner@example.test",
        NOW,
        actor_subject="subject-1",
        expected_version=resolved.version,
        idempotency_key="close-key",
    )
    assert closed and closed.state == IncidentState.CLOSED
    replay_close = await database.close_incident(
        incident.id,
        "owner@example.test",
        NOW,
        actor_subject="subject-1",
        expected_version=resolved.version,
        idempotency_key="close-key",
    )
    assert replay_close and replay_close.state == IncidentState.CLOSED


async def test_maintenance_scoping_cancellation_and_idempotency(database: Database) -> None:
    with pytest.raises(KeyError, match="unknown"):
        await database.create_maintenance(
            maintenance_id="unknown-window",
            asset_ids=["missing"],
            starts_at=NOW,
            ends_at=NOW + timedelta(hours=1),
            reason="Unknown",
            actor_subject="subject",
            actor_email="owner@example.test",
            at=NOW,
        )
    window = await database.create_maintenance(
        maintenance_id="window-1",
        asset_ids=["atlas-node", "orchid-guest"],
        starts_at=NOW,
        ends_at=NOW + timedelta(hours=1),
        reason="Upgrade",
        actor_subject="subject",
        actor_email="owner@example.test",
        at=NOW,
        idempotency_key="window-key",
    )
    replay = await database.create_maintenance(
        maintenance_id="different-id",
        asset_ids=["orchid-guest", "atlas-node"],
        starts_at=NOW,
        ends_at=NOW + timedelta(hours=1),
        reason="Upgrade",
        actor_subject="subject",
        actor_email="owner@example.test",
        at=NOW,
        idempotency_key="window-key",
    )
    assert replay.id == window.id
    with pytest.raises(IdempotencyConflictError):
        await database.create_maintenance(
            maintenance_id="other",
            asset_ids=["atlas-node"],
            starts_at=NOW,
            ends_at=NOW + timedelta(hours=1),
            reason="Changed",
            actor_subject="subject",
            actor_email="owner@example.test",
            at=NOW,
            idempotency_key="window-key",
        )
    assert await database.is_maintenance_active("atlas-node", NOW + timedelta(minutes=1))
    assert not await database.is_maintenance_active("atlas-node", NOW + timedelta(hours=2))
    assert await database.get_maintenance("missing") is None
    assert await database.list_maintenance(include_expired=True)
    assert (
        await database.cancel_maintenance(
            "missing",
            actor_subject="subject",
            actor_email="owner@example.test",
            expected_version=1,
            at=NOW,
        )
        is None
    )
    with pytest.raises(ConflictError, match="version"):
        await database.cancel_maintenance(
            window.id,
            actor_subject="subject",
            actor_email="owner@example.test",
            expected_version=99,
            at=NOW,
        )
    cancelled = await database.cancel_maintenance(
        window.id,
        actor_subject="subject",
        actor_email="owner@example.test",
        expected_version=window.version,
        at=NOW,
        idempotency_key="cancel-key",
    )
    assert cancelled and cancelled.cancelled_at
    replay_cancel = await database.cancel_maintenance(
        window.id,
        actor_subject="subject",
        actor_email="owner@example.test",
        expected_version=window.version,
        at=NOW,
        idempotency_key="cancel-key",
    )
    assert replay_cancel and replay_cancel.version == cancelled.version


async def test_notification_outbox_retention_and_backup_rotation(
    database: Database, tmp_path
) -> None:
    await open_incident(database, "notify-a")
    await open_incident(database, "notify-b", root="orchid-guest")
    due = await database.due_notifications(NOW)
    assert len(due) == 2
    await database.mark_notification("missing", delivered=True, at=NOW)
    await database.mark_notification(
        due[0]["event_uuid"], delivered=False, at=NOW, diagnostic="timeout"
    )
    assert len(await database.due_notifications(NOW)) == 1
    await database.mark_notification(
        due[0]["event_uuid"],
        delivered=False,
        at=NOW + timedelta(minutes=2),
        diagnostic="failed",
        max_attempts=2,
    )
    await database.mark_notification(due[1]["event_uuid"], delivered=True, at=NOW)
    status = await database.notification_status(enabled=True)
    assert status["delivered"] == 1
    assert status["dead_letter"] == 1
    assert status["suppressed"] == 0
    assert status["last_success_at"] is not None

    old = NOW - timedelta(days=10)
    await database.record_observation(sample("atlas-node", HealthState.HEALTHY, at=old))
    await database.cleanup(7, 365, NOW, 180)
    assert await database.list_samples("atlas-node", old - timedelta(days=1)) == []
    assert await database.list_hourly_rollups("atlas-node", old - timedelta(days=1))

    backup_dir = tmp_path / "daily"
    first = await database.backup_to(backup_dir, retention=1)
    assert first.is_file() and first.with_suffix(first.suffix + ".sha256").is_file()
    extra = backup_dir / "signal-room-2000-01-01.sqlite3"
    extra.write_bytes(first.read_bytes())
    extra.with_suffix(extra.suffix + ".sha256").write_text("old", encoding="ascii")
    os.utime(extra, (1, 1))
    await database.backup_to(backup_dir, retention=1)
    assert not extra.exists()
    assert await database.readiness() == (True, "ready")


async def test_disabled_notifications_are_suppressed_and_future_events_do_not_queue(
    database: Database,
) -> None:
    await open_incident(database, "disabled-a")
    await open_incident(database, "disabled-b", root="orchid-guest")

    assert len(await database.due_notifications(NOW)) == 2
    assert await database.record_notification_heartbeat(enabled=False, at=NOW, success=False) == 2
    assert await database.due_notifications(NOW) == []
    status = await database.notification_status(enabled=False)
    assert status == {
        "enabled": False,
        "pending": 0,
        "delivered": 0,
        "dead_letter": 0,
        "suppressed": 2,
        "last_success_at": None,
    }

    assert await database.set_incident_state(
        "disabled-a",
        IncidentState.RESOLVED,
        at=NOW + timedelta(minutes=1),
        message="Recovered while notifications are disabled",
    )
    count = await (
        await database.conn.execute("SELECT COUNT(*) FROM notification_outbox")
    ).fetchone()
    assert count[0] == 2
    audit = await (
        await database.conn.execute(
            "SELECT metadata_json FROM audit_events WHERE kind='notifications_suppressed'"
        )
    ).fetchone()
    assert audit is not None and audit[0] == '{"count":2}'


async def test_outbox_retention_cannot_block_incident_cleanup(database: Database) -> None:
    old = NOW - timedelta(days=366)
    await open_incident(database, "terminal-old", at=old)
    await open_incident(database, "pending-old", root="orchid-guest", at=old)
    terminal_event = await (
        await database.conn.execute(
            "SELECT event_uuid FROM notification_outbox WHERE incident_id='terminal-old'"
        )
    ).fetchone()
    assert terminal_event is not None
    await database.mark_notification(terminal_event[0], delivered=True, at=old)
    await database.conn.execute(
        """
        UPDATE incidents SET state='resolved', recovered_at=?
        WHERE id IN ('terminal-old', 'pending-old')
        """,
        (old.isoformat(),),
    )
    await database.conn.commit()

    await database.cleanup(7, 365, NOW, 180)

    incidents = await (
        await database.conn.execute(
            "SELECT COUNT(*) FROM incidents WHERE id IN ('terminal-old', 'pending-old')"
        )
    ).fetchone()
    assert incidents[0] == 0
    terminal = await (
        await database.conn.execute(
            "SELECT COUNT(*) FROM notification_outbox WHERE event_uuid=?",
            (terminal_event[0],),
        )
    ).fetchone()
    assert terminal[0] == 0
    pending = await (
        await database.conn.execute(
            "SELECT incident_id FROM notification_outbox WHERE event_kind='opened'"
        )
    ).fetchall()
    assert [row[0] for row in pending] == [None]
