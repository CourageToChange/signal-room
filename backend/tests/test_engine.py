from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import permutations
from pathlib import Path

import pytest
from signal_room.db import Database
from signal_room.engine import IncidentEngine
from signal_room.migrate import migrate_database
from signal_room.models import (
    HealthState,
    IncidentState,
    IncidentType,
    Observation,
    ProviderKind,
    Severity,
    TopologyConfig,
)


def observation(
    asset_id: str,
    at: datetime,
    health: HealthState,
    message: str = "test observation",
) -> Observation:
    return Observation(asset_id=asset_id, observed_at=at, health=health, message=message)


async def test_cascading_failures_create_one_parent_incident(
    database: Database, topology: TopologyConfig
) -> None:
    engine = IncidentEngine(database, topology.assets, topology.thresholds)
    start = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)

    for offset in range(3):
        await engine.process(
            observation("orchid-guest", start + timedelta(seconds=offset * 5), HealthState.DEGRADED)
        )
    for asset_id in ("gallery-service", "notes-service", "portfolio-service"):
        for offset in range(3):
            await engine.process(
                observation(
                    asset_id,
                    start + timedelta(seconds=15 + offset * 5),
                    HealthState.DOWN,
                )
            )

    incidents = await database.list_incidents()
    assert len(incidents) == 1
    assert incidents[0].root_asset_id == "orchid-guest"
    assert set(incidents[0].affected_asset_ids) == {
        "orchid-guest",
        "gallery-service",
        "notes-service",
        "portfolio-service",
    }
    assert sum(event.kind == "correlated" for event in incidents[0].events) == 3


async def test_resource_warning_must_persist_for_configured_window(
    database: Database, topology: TopologyConfig
) -> None:
    engine = IncidentEngine(database, topology.assets, topology.thresholds)
    start = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)

    for seconds in (0, 150, 299):
        await engine.process(
            Observation(
                asset_id="atlas-node",
                observed_at=start + timedelta(seconds=seconds),
                health=HealthState.DEGRADED,
                message="CPU 92%",
                details={"condition": "resource_pressure", "severity": "warning"},
            )
        )
    assert await database.list_incidents() == []

    await engine.process(
        Observation(
            asset_id="atlas-node",
            observed_at=start + timedelta(seconds=topology.thresholds.resource_warning_seconds),
            health=HealthState.DEGRADED,
            message="CPU 93%",
            details={"condition": "resource_pressure", "severity": "warning"},
        )
    )
    assert len(await database.list_incidents()) == 1


async def test_recovery_requires_every_affected_asset_twice(
    database: Database, topology: TopologyConfig
) -> None:
    engine = IncidentEngine(database, topology.assets, topology.thresholds)
    start = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    affected = ["orchid-guest", "gallery-service"]
    for offset in range(3):
        await engine.process(
            observation("orchid-guest", start + timedelta(seconds=offset), HealthState.DOWN)
        )
        await engine.process(
            observation("gallery-service", start + timedelta(seconds=offset), HealthState.DOWN)
        )
    incident = (await database.list_incidents())[0]
    assert incident.state == IncidentState.OPEN

    for asset_id in affected:
        await engine.process(
            observation(asset_id, start + timedelta(minutes=1), HealthState.HEALTHY)
        )
    incident = await database.get_incident(incident.id)
    assert incident and incident.state == IncidentState.RECOVERING

    for asset_id in affected:
        await engine.process(
            observation(asset_id, start + timedelta(minutes=1, seconds=5), HealthState.HEALTHY)
        )
    incident = await database.get_incident(incident.id)
    assert incident and incident.state == IncidentState.RESOLVED
    assert incident.events[-1].kind == "resolved"


async def test_resolved_incident_can_be_acknowledged_only_before_recovery(
    database: Database, topology: TopologyConfig
) -> None:
    engine = IncidentEngine(database, topology.assets, topology.thresholds)
    start = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    for offset in range(3):
        await engine.process(
            observation("atlas-node", start + timedelta(seconds=offset), HealthState.DOWN)
        )
    incident = (await database.list_incidents())[0]
    acknowledged = await database.acknowledge(incident.id, "owner@example.invalid", start)
    assert acknowledged and acknowledged.acknowledged_by == "owner@example.invalid"
    second = await database.acknowledge(incident.id, "other@example.invalid", start)
    assert second and second.acknowledged_by == "owner@example.invalid"


async def test_database_online_backup_is_consistent(
    database: Database, topology: TopologyConfig, tmp_path: Path
) -> None:
    destination = tmp_path / "backup" / "copy.sqlite3"
    await database.backup_to(destination)
    copy = Database(destination)
    await copy.connect()
    try:
        assert len(await copy.list_assets()) == len(topology.assets)
    finally:
        await copy.close()


@pytest.mark.parametrize(
    ("health", "details", "condition", "severity"),
    [
        (HealthState.UNKNOWN, {}, IncidentType.MONITORING_UNAVAILABLE, Severity.WARNING),
        (HealthState.DEGRADED, {"source": "https"}, IncidentType.HTTP_FAILED, Severity.WARNING),
        (
            HealthState.DEGRADED,
            {"source": "tls", "severity": "critical"},
            IncidentType.CERTIFICATE_EXPIRING,
            Severity.CRITICAL,
        ),
        (
            HealthState.DEGRADED,
            {"source": "proxmox-backup"},
            IncidentType.BACKUP_STALE,
            Severity.WARNING,
        ),
        (
            HealthState.DEGRADED,
            {"condition": "resource_pressure"},
            IncidentType.RESOURCE_PRESSURE,
            Severity.WARNING,
        ),
        (HealthState.DOWN, {}, IncidentType.ASSET_DOWN, Severity.CRITICAL),
    ],
)
def test_incident_classification_is_typed_and_ranked(
    topology: TopologyConfig,
    health: HealthState,
    details: dict[str, str],
    condition: IncidentType,
    severity: Severity,
) -> None:
    engine = IncidentEngine(None, topology.assets, topology.thresholds)  # type: ignore[arg-type]
    item = Observation(asset_id="atlas-node", health=health, message="Signal", details=details)
    assert engine._condition(item) == condition
    assert engine._severity(item) == severity
    assert engine._observation_rank(item)[0] >= 0
    explicit = item.model_copy(update={"condition": IncidentType.BACKUP_FAILED})
    assert engine._condition(explicit) == IncidentType.BACKUP_FAILED
    assert [asset.id for asset in engine._ancestors("gallery-service")] == [
        "atlas-node",
        "orchid-guest",
    ]


async def test_provider_batch_rejects_unknown_assets(
    database: Database, topology: TopologyConfig
) -> None:
    engine = IncidentEngine(database, topology.assets, topology.thresholds)
    with pytest.raises(KeyError, match="unknown assets"):
        await engine.process_batch(
            provider=ProviderKind.FIXTURE,
            run_id="unknown-batch",
            attempted_at=datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
            observations=[
                Observation(asset_id="not-configured", health=HealthState.DOWN, message="Down")
            ],
        )


async def test_observation_order_cannot_change_incident_correlation(
    tmp_path: Path, topology: TopologyConfig
) -> None:
    start = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    asset_ids = ["orchid-guest", "gallery-service", "notes-service", "portfolio-service"]
    expected: tuple[object, ...] | None = None
    for case, order in enumerate(permutations(asset_ids)):
        db = Database(tmp_path / f"order-{case}.sqlite3")
        await migrate_database(db.path)
        await db.connect()
        try:
            await db.sync_assets(topology.assets)
            engine = IncidentEngine(
                db,
                topology.assets,
                topology.thresholds,
                incident_id_factory=lambda root, at: f"incident-{root}",
            )
            for run in range(3):
                observations = [
                    Observation(
                        asset_id=asset_id,
                        observed_at=start + timedelta(seconds=run),
                        health=HealthState.DOWN,
                        message=f"{asset_id} down",
                    )
                    for asset_id in order
                ]
                await engine.process_batch(
                    provider=ProviderKind.FIXTURE,
                    run_id=f"case-{case}-run-{run}",
                    attempted_at=start + timedelta(seconds=run),
                    completed_at=start + timedelta(seconds=run),
                    observations=observations,
                )
            incident = (await db.list_incidents())[0]
            actual = (
                incident.id,
                incident.root_asset_id,
                incident.incident_type,
                incident.severity,
                tuple(incident.affected_asset_ids),
                tuple(event.kind for event in incident.events),
            )
            expected = actual if expected is None else expected
            assert actual == expected
        finally:
            await db.close()


async def test_maintenance_mutes_until_one_fresh_post_window_observation(
    database: Database, topology: TopologyConfig
) -> None:
    engine = IncidentEngine(database, topology.assets, topology.thresholds)
    start = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    await database.create_maintenance(
        maintenance_id="maintenance-window",
        asset_ids=["atlas-node"],
        starts_at=start - timedelta(minutes=1),
        ends_at=start + timedelta(minutes=1),
        reason="Planned work",
        actor_subject="subject",
        actor_email="owner@example.test",
        idempotency_key="maintenance-engine-test",
        at=start - timedelta(minutes=2),
    )
    for offset in range(3):
        await engine.process(
            observation("atlas-node", start + timedelta(seconds=offset), HealthState.DOWN)
        )
    assert await database.list_incidents() == []
    await engine.process(observation("atlas-node", start + timedelta(minutes=2), HealthState.DOWN))
    assert len(await database.list_incidents()) == 1


async def test_unhealthy_signals_interrupt_recovery(
    database: Database, topology: TopologyConfig
) -> None:
    engine = IncidentEngine(database, topology.assets, topology.thresholds)
    start = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    for offset in range(3):
        await engine.process(
            observation("atlas-node", start + timedelta(seconds=offset), HealthState.DOWN)
        )
    incident = (await database.list_incidents())[0]
    await engine.process(
        observation("atlas-node", start + timedelta(minutes=1), HealthState.HEALTHY)
    )
    assert (await database.get_incident(incident.id)).state == IncidentState.RECOVERING  # type: ignore[union-attr]
    for offset in range(3):
        await engine.process(
            observation(
                "atlas-node",
                start + timedelta(minutes=2, seconds=offset),
                HealthState.DOWN,
            )
        )
    interrupted = await database.get_incident(incident.id)
    assert interrupted and interrupted.state == IncidentState.OPEN
    assert any(
        event.message == "An unhealthy observation interrupted recovery"
        for event in interrupted.events
    )
