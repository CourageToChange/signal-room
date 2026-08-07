#!/usr/bin/env python3
"""Run an isolated, seeded fixture backend for browser release tests."""

from __future__ import annotations

import asyncio
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import uvicorn
from signal_room.api import create_app
from signal_room.config import AppSettings, load_topology
from signal_room.core import CoreService
from signal_room.db import Database
from signal_room.engine import IncidentEngine
from signal_room.migrate import migrate_database
from signal_room.models import HealthState, Observation

ROOT = Path(__file__).resolve().parents[1]


async def seed(settings: AppSettings) -> None:
    await migrate_database(settings.db_path)
    topology = load_topology(settings.config_path)
    database = Database(settings.db_path)
    await database.connect()
    try:
        await database.sync_assets(topology.assets, topology.revision)
        engine = IncidentEngine(database, topology.assets, topology.thresholds)
        start = datetime.now(UTC) - timedelta(seconds=2)
        for offset in range(topology.thresholds.failure_observations):
            await engine.process(
                Observation(
                    asset_id="atlas-node",
                    observed_at=start + timedelta(seconds=offset),
                    health=HealthState.DOWN,
                    message="Seeded read-only fixture failure",
                )
            )
        await database.set_runtime_value("collector_last_seen_at", datetime.now(UTC).isoformat())
    finally:
        await database.close()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="signal-room-e2e-") as temporary:
        root = Path(temporary)
        settings = AppSettings(
            environment="test",
            runtime_role="all",
            mode="fixture",
            auth_mode="development",
            config_path=ROOT / "config/config.example.yaml",
            runbooks_path=ROOT / "config/runbooks.yaml",
            static_dir=ROOT / "frontend/dist-private",
            db_path=root / "fixture.sqlite3",
            query_socket=root / "query.sock",
            ingest_socket=root / "ingest.sock",
            notifier_socket=root / "notifier.sock",
            maintenance_socket=root / "maintenance.sock",
            public_origin="http://127.0.0.1:8081",
            trusted_hosts="127.0.0.1,localhost",
            build_sha="e2e-fixture",
        )
        asyncio.run(seed(settings))
        app = create_app(settings, core_service=CoreService(settings))
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=8081,
            log_level="warning",
            server_header=False,
            proxy_headers=False,
        )


if __name__ == "__main__":
    main()
