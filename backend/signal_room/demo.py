from __future__ import annotations

import json
import re
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from .db import Database
from .engine import IncidentEngine
from .migrate import migrate_database
from .models import (
    BootstrapResponse,
    Capabilities,
    HealthState,
    IncidentType,
    Observation,
    ProviderKind,
    RunbookConfig,
    TopologyConfig,
)

PRIVATE_PATTERN = re.compile(
    r"(?:192\.168\.|10\.\d+\.|172\.(?:1[6-9]|2\d|3[01])\.)|"
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|"
    r"(?:token|secret|password)\s*[=:]\s*[^\s,]+",
    re.IGNORECASE,
)


def _observation(
    asset_id: str,
    at: datetime,
    health: HealthState,
    message: str,
    **metrics: float,
) -> Observation:
    return Observation(
        asset_id=asset_id,
        check_id="fixture-health",
        provider=ProviderKind.FIXTURE,
        observed_at=at,
        health=health,
        condition=(
            IncidentType.RESOURCE_PRESSURE
            if asset_id == "orchid-guest" and health == HealthState.DEGRADED
            else IncidentType.HTTP_FAILED
            if "service" in asset_id and health != HealthState.HEALTHY
            else IncidentType.ASSET_DOWN
            if health == HealthState.DOWN
            else None
        ),
        message=message,
        cpu_ratio=metrics.get("cpu_ratio"),
        memory_ratio=metrics.get("memory_ratio"),
        disk_ratio=metrics.get("disk_ratio"),
        latency_ms=metrics.get("latency_ms"),
        details={
            "source": "fictional-drill",
            "severity": "critical" if health == HealthState.DOWN else None,
        },
    )


async def export_pressure_drop(
    topology: TopologyConfig,
    runbooks: RunbookConfig,
    output: Path,
) -> None:
    start = datetime(2026, 7, 15, 18, 0, tzinfo=UTC)
    with tempfile.TemporaryDirectory(prefix="signal-room-demo-") as temp_dir:
        database_path = Path(temp_dir) / "demo.sqlite3"
        await migrate_database(database_path)
        database = Database(database_path)
        await database.connect()
        await database.sync_assets(topology.assets, topology.revision)
        drill_thresholds = topology.thresholds.model_copy(update={"resource_warning_seconds": 10})
        engine = IncidentEngine(
            database,
            topology.assets,
            drill_thresholds,
            incident_id_factory=lambda asset_id, opened_at: str(
                uuid5(NAMESPACE_URL, f"signal-room:{asset_id}:{opened_at.isoformat()}")
            ),
        )
        frames: list[dict[str, object]] = []

        for seconds in range(0, 81, 5):
            at = start + timedelta(seconds=seconds)
            if seconds < 15:
                guest_health = HealthState.HEALTHY
                guest_memory = 0.58 + seconds / 500
                guest_message = "Guest running normally"
            elif seconds < 30:
                guest_health = HealthState.DEGRADED
                guest_memory = {15: 0.91, 20: 0.95, 25: 0.98}[seconds]
                guest_message = f"Memory pressure reached {guest_memory:.0%}"
            elif seconds < 60:
                guest_health = HealthState.DOWN if seconds >= 40 else HealthState.DEGRADED
                guest_memory = 0.99
                guest_message = "Shared guest is no longer serving dependants"
            else:
                guest_health = HealthState.HEALTHY
                guest_memory = max(0.62, 0.88 - (seconds - 60) / 100)
                guest_message = "Guest capacity has recovered"

            service_health = (
                HealthState.HEALTHY
                if seconds < 25 or seconds >= 65
                else HealthState.DEGRADED
                if seconds < 35
                else HealthState.DOWN
            )
            service_message = (
                "HTTPS check passed"
                if service_health == HealthState.HEALTHY
                else "Latency rising behind shared dependency"
                if service_health == HealthState.DEGRADED
                else "Origin check failed"
            )
            latency = (
                32
                if service_health == HealthState.HEALTHY
                else 780
                if service_health == HealthState.DEGRADED
                else 5000
            )

            observations = [
                _observation(
                    "atlas-node",
                    at,
                    HealthState.HEALTHY,
                    "Host remains healthy",
                    cpu_ratio=0.37,
                    memory_ratio=0.55,
                    disk_ratio=0.44,
                ),
                _observation(
                    "orchid-guest",
                    at,
                    guest_health,
                    guest_message,
                    cpu_ratio=0.82 if seconds >= 15 and seconds < 60 else 0.24,
                    memory_ratio=guest_memory,
                    disk_ratio=0.39,
                ),
                *[
                    _observation(
                        asset_id,
                        at,
                        service_health,
                        service_message,
                        latency_ms=latency + index * 40,
                    )
                    for index, asset_id in enumerate(
                        ["gallery-service", "notes-service", "portfolio-service"]
                    )
                ],
                _observation(
                    "vault-storage",
                    at,
                    HealthState.HEALTHY,
                    "Storage capacity healthy",
                    disk_ratio=0.58,
                ),
                _observation(
                    "backup-chain",
                    at,
                    HealthState.HEALTHY,
                    "Latest backup completed 11 hours ago",
                ),
            ]
            await engine.process_batch(
                provider=ProviderKind.FIXTURE,
                run_id=f"pressure-drop-{seconds}",
                attempted_at=at,
                completed_at=at,
                observations=observations,
            )
            incidents = await database.list_incidents()
            for incident in incidents:
                asset = next(item for item in topology.assets if item.id == incident.root_asset_id)
                if asset.runbook_id:
                    incident.runbook = runbooks.runbooks.get(asset.runbook_id)
            frame = BootstrapResponse(
                build_sha="fictional-demo",
                generated_at=at,
                collector_last_seen_at=at,
                stale=False,
                assets=await database.list_assets(),
                states=await database.list_states(),
                incidents=[item for item in incidents],
                capabilities=Capabilities(can_mutate=False, data_source="fixture"),
                last_event_id=await database.latest_event_id(),
            )
            snapshot = frame.model_dump(mode="json")
            snapshot["incidents"] = [item.model_dump(mode="json") for item in incidents]
            for incident in snapshot["incidents"]:
                for event in incident["events"]:
                    event["event_uuid"] = str(
                        uuid5(
                            NAMESPACE_URL,
                            f"signal-room:event:{incident['id']}:{event['id']}:{event['kind']}",
                        )
                    )
            frames.append({"at_seconds": seconds, "snapshot": snapshot})
        await database.close()

    payload = {
        "version": 1,
        "slug": "pressure-drop",
        "title": "Pressure Drop",
        "summary": "A shared guest saturates while its host remains healthy.",
        "duration_seconds": 80,
        "frames": frames,
        "questions": [
            {
                "id": "root-cause",
                "prompt": "Which asset is the shared point of failure?",
                "options": ["Atlas Node", "Orchid Guest", "Vault Storage"],
                "answer": "Orchid Guest",
                "explanation": (
                    "All three failed services depend on Orchid while Atlas and Vault stay healthy."
                ),
            },
            {
                "id": "first-action",
                "prompt": "What should be checked before restarting services?",
                "options": [
                    "Delete old data immediately",
                    "Inspect the guest's memory trend and recent changes",
                    "Restart every child service together",
                ],
                "answer": "Inspect the guest's memory trend and recent changes",
                "explanation": (
                    "Preserving evidence and checking the shared dependency avoids "
                    "treating symptoms first."
                ),
            },
        ],
    }
    serialized = json.dumps(payload, indent=2, sort_keys=False)
    if PRIVATE_PATTERN.search(serialized):
        raise ValueError("demo export contains a private-data pattern")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(serialized + "\n", encoding="utf-8", newline="\n")
