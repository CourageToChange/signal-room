from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio
from signal_room.config import AppSettings, load_runbooks, load_topology
from signal_room.db import Database
from signal_room.migrate import migrate_database
from signal_room.models import RunbookConfig, TopologyConfig

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def topology() -> TopologyConfig:
    return load_topology(ROOT / "config" / "config.example.yaml")


@pytest.fixture
def runbooks() -> RunbookConfig:
    return load_runbooks(ROOT / "config" / "runbooks.yaml")


@pytest.fixture
def settings(tmp_path: Path) -> AppSettings:
    static_dir = tmp_path / "static-private"
    assets_dir = static_dir / "assets"
    assets_dir.mkdir(parents=True)
    (static_dir / "index.html").write_text(
        "<!doctype html><html><body>Signal Room test shell</body></html>",
        encoding="utf-8",
    )
    (assets_dir / "app-12345678.js").write_text("export {};", encoding="utf-8")
    result = AppSettings(
        environment="test",
        mode="fixture",
        auth_mode="development",
        config_path=ROOT / "config" / "config.example.yaml",
        runbooks_path=ROOT / "config" / "runbooks.yaml",
        static_dir=static_dir,
        db_path=tmp_path / "signal-room.sqlite3",
        public_origin="http://testserver",
        trusted_hosts="testserver",
    )
    asyncio.run(migrate_database(result.db_path))
    return result


@pytest_asyncio.fixture
async def database(tmp_path: Path, topology: TopologyConfig):
    db = Database(tmp_path / "engine.sqlite3")
    await migrate_database(db.path)
    await db.connect()
    await db.sync_assets(topology.assets)
    yield db
    await db.close()
