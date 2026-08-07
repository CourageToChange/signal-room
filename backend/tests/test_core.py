from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from signal_room.config import AppSettings
from signal_room.core import (
    MAX_RPC_BYTES,
    PRODUCTION_SOCKET_GROUPS,
    CoreClient,
    CoreRequestError,
    CoreService,
    CoreUnavailableError,
    InProcessCore,
)
from signal_room.db import Database
from signal_room.migrate import migrate_database
from signal_room.models import (
    HealthState,
    IncidentState,
    Observation,
    ProviderKind,
    TopologyConfig,
)


def ingest_params(asset_id: str, run_id: str, health: HealthState) -> dict[str, Any]:
    sequence = int(run_id.rsplit("-", 1)[-1])
    observation = Observation(
        asset_id=asset_id,
        observed_at=datetime.now(UTC).replace(microsecond=0)
        - timedelta(minutes=1)
        + timedelta(seconds=sequence),
        health=health,
        message=f"{asset_id} {health}",
    )
    return {
        "provider": "fixture",
        "run_id": run_id,
        "attempted_at": observation.observed_at.isoformat(),
        "completed_at": observation.observed_at.isoformat(),
        "success": True,
        "error_code": None,
        "message": "Fixture completed",
        "observations": [observation.model_dump(mode="json")],
    }


def test_production_socket_permissions_are_role_scoped(
    settings: AppSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    production = settings.model_copy(update={"environment": "production"})
    service = CoreService(production)
    ownership: list[tuple[Path, str]] = []
    permissions: list[tuple[Path, int]] = []
    monkeypatch.setattr(
        "signal_room.core.shutil.chown",
        lambda path, *, group: ownership.append((Path(path), group)),
    )
    monkeypatch.setattr(
        "signal_room.core.os.chmod",
        lambda path, mode: permissions.append((Path(path), mode)),
    )

    for role, group in PRODUCTION_SOCKET_GROUPS.items():
        path = tmp_path / f"{role}.sock"
        path.touch(mode=0o600)
        service._secure_socket(path, role)
        assert ownership[-1] == (path, group)
        assert permissions[-1] == (path, 0o660)

    development_path = tmp_path / "development.sock"
    development_path.touch(mode=0o600)
    CoreService(settings)._secure_socket(development_path, "query")
    assert len(ownership) == len(PRODUCTION_SOCKET_GROUPS)
    assert permissions[-1] == (development_path, 0o660)


async def test_core_dispatch_covers_scoped_query_ingest_and_responder_workflows(
    settings: AppSettings,
    database: Database,
    topology: TopologyConfig,
    tmp_path: Path,
) -> None:
    service = CoreService(settings, database)
    query = InProcessCore(service, "query")
    ingest = InProcessCore(service, "ingest")
    notifier = InProcessCore(service, "notifier")
    maintenance = InProcessCore(service, "maintenance")

    with pytest.raises(CoreRequestError, match="not permitted"):
        await query.call("ingest_batch", {})
    with pytest.raises(CoreRequestError) as missing:
        await query.call("asset_detail", {"asset_id": "missing"})
    assert missing.value.code == "not_found"

    bootstrap = await query.call("bootstrap")
    assert bootstrap["stale"] is True
    assert len(bootstrap["assets"]) == len(topology.assets)
    detail = await query.call("asset_detail", {"asset_id": "atlas-node"})
    assert detail["asset"]["check_ids"] == ["fixture-health"]

    empty_metrics = await query.call(
        "metrics", {"asset_id": "atlas-node", "range": "1h", "resolution": "raw"}
    )
    assert empty_metrics["buckets"] == []
    with pytest.raises(CoreRequestError, match="range"):
        await query.call(
            "metrics", {"asset_id": "atlas-node", "range": "century", "resolution": "auto"}
        )
    with pytest.raises(CoreRequestError, match="resolution"):
        await query.call("metrics", {"asset_id": "atlas-node", "range": "1h", "resolution": "2m"})

    for index, health in enumerate(
        [HealthState.DOWN, HealthState.DOWN, HealthState.DOWN, HealthState.HEALTHY]
    ):
        assert (
            await ingest.call("ingest_batch", ingest_params("atlas-node", f"run-{index}", health))
        )["ok"]
    await ingest.call("collector_heartbeat", {"at": datetime.now(UTC).isoformat()})
    metrics = await query.call(
        "metrics", {"asset_id": "atlas-node", "range": "24h", "resolution": "auto"}
    )
    assert metrics["resolution"] == "5m"
    assert metrics["buckets"]
    raw = await query.call(
        "metrics", {"asset_id": "atlas-node", "range": "1h", "resolution": "raw"}
    )
    assert raw["buckets"]
    await database.conn.executemany(
        """
        INSERT OR REPLACE INTO hourly_rollups(
          asset_id, bucket_at, sample_count, healthy_count,
          cpu_ratio_avg, memory_ratio_avg, disk_ratio_avg, latency_ms_avg
        ) VALUES (?, ?, 2, ?, .4, .5, .3, 20)
        """,
        [
            (
                "atlas-node",
                (datetime.now(UTC) - timedelta(days=2))
                .replace(minute=0, second=0, microsecond=0)
                .isoformat(),
                2,
            ),
            (
                "atlas-node",
                (datetime.now(UTC) - timedelta(days=1))
                .replace(minute=0, second=0, microsecond=0)
                .isoformat(),
                1,
            ),
        ],
    )
    await database.conn.commit()
    long_metrics = await query.call(
        "metrics", {"asset_id": "atlas-node", "range": "30d", "resolution": "1d"}
    )
    assert {item["health"] for item in long_metrics["buckets"]} >= {"healthy", "degraded"}

    page = await query.call("incident_page", {"states": ["open", "recovering"], "limit": 500})
    assert len(page["items"]) == 1
    incident_id = page["items"][0]["id"]
    incident = await query.call("incident", {"incident_id": incident_id})
    assert incident["runbook"] is not None
    assert (await query.call("timeline", {"incident_id": incident_id, "limit": 500}))["items"]
    for method in ("incident", "timeline"):
        with pytest.raises(CoreRequestError) as error:
            await query.call(method, {"incident_id": "missing"})
        assert error.value.code == "not_found"

    action = {
        "incident_id": incident_id,
        "actor_subject": "subject-1",
        "actor_email": "owner@example.test",
        "version": incident["version"],
        "idempotency_key": "acknowledge-key",
    }
    acknowledged = await query.call("acknowledge", action)
    noted = await query.call(
        "note",
        {
            **action,
            "version": acknowledged["version"],
            "idempotency_key": "note-key-123",
            "body": "Evidence checked",
        },
    )
    await database.set_incident_state(
        incident_id, IncidentState.RESOLVED, at=datetime.now(UTC), message="Recovered"
    )
    resolved = await database.get_incident(incident_id)
    assert resolved
    closed = await query.call(
        "close",
        {
            **action,
            "version": resolved.version,
            "idempotency_key": "close-key-123",
        },
    )
    assert noted["notes"] and closed["state"] == "closed"
    with pytest.raises(CoreRequestError) as conflict:
        await query.call("close", {**action, "version": 1, "idempotency_key": "other-key"})
    assert conflict.value.code == "conflict"

    starts = datetime.now(UTC) + timedelta(minutes=5)
    created = await query.call(
        "maintenance_create",
        {
            "maintenance": {
                "asset_ids": ["atlas-node"],
                "starts_at": starts.isoformat(),
                "ends_at": (starts + timedelta(hours=1)).isoformat(),
                "reason": "Upgrade",
            },
            "actor_subject": "subject-1",
            "actor_email": "owner@example.test",
            "idempotency_key": "maintenance-key",
        },
    )
    assert await query.call("maintenance_list", {"include_expired": True})
    cancelled = await query.call(
        "maintenance_cancel",
        {
            "maintenance_id": created["id"],
            "actor_subject": "subject-1",
            "actor_email": "owner@example.test",
            "version": created["version"],
            "idempotency_key": "cancel-key-123",
        },
    )
    assert cancelled["cancelled_at"]
    with pytest.raises(CoreRequestError) as missing_window:
        await query.call(
            "maintenance_cancel",
            {
                "maintenance_id": "missing",
                "actor_subject": "subject-1",
                "actor_email": "owner@example.test",
                "version": 1,
                "idempotency_key": "missing-key",
            },
        )
    assert missing_window.value.code == "not_found"

    await notifier.call("notification_heartbeat", {"enabled": True, "success": True})
    assert isinstance(await notifier.call("notifications_due", {"limit": 999}), list)
    await notifier.call(
        "notification_mark",
        {"event_uuid": "missing", "delivered": False, "diagnostic": "test"},
    )
    diagnostics = await query.call("diagnostics", {"request_id": "request-1"})
    assert diagnostics["request_id"] == "request-1"
    assert await query.call("stream_events", {"after": 0, "limit": 999})
    readiness = await query.call("readiness")
    assert readiness["database"] == "ready"
    backup = await maintenance.call("backup", {"destination": str(tmp_path / "core-backup")})
    assert Path(backup["path"]).is_file()

    for provider in ProviderKind:
        assert service._provider_interval(provider) > 0


class MemoryWriter:
    def __init__(self) -> None:
        self.data = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def reader_for(payload: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader(limit=MAX_RPC_BYTES * 2)
    reader.feed_data(payload)
    reader.feed_eof()
    return reader


async def test_core_socket_boundary_redacts_and_classifies_failures(
    settings: AppSettings, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = CoreService(settings, database)
    cases = [
        (b"not json\n", "invalid_json"),
        (b"[]\n", "invalid_request"),
        (b'{"id":"","method":"bootstrap","params":{}}\n', "invalid_request"),
        (b'{"id":"1","method":"backup","params":{}}\n', "forbidden_method"),
        (b'{"id":"1","method":"asset_detail","params":{"asset_id":"missing"}}\n', "not_found"),
        (b"x" * (MAX_RPC_BYTES + 1) + b"\n", "request_too_large"),
    ]
    for payload, code in cases:
        writer = MemoryWriter()
        await service._handle("query", reader_for(payload), writer)  # type: ignore[arg-type]
        response = json.loads(writer.data)
        assert response["error"]["code"] == code
        assert writer.closed

    writer = MemoryWriter()
    await service._handle(
        "query",
        reader_for(b'{"id":"ok","method":"bootstrap","params":{}}\n'),
        writer,  # type: ignore[arg-type]
    )
    assert json.loads(writer.data)["result"]["assets"]

    async def explode(role: str, method: str, params: dict[str, Any]) -> None:
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(service, "dispatch", explode)
    writer = MemoryWriter()
    await service._handle(
        "query",
        reader_for(b'{"id":"1","method":"bootstrap","params":{}}\n'),
        writer,  # type: ignore[arg-type]
    )
    assert json.loads(writer.data)["error"] == {
        "code": "internal_error",
        "message": "core request failed",
    }


class ClientReader:
    def __init__(self, writer: ClientWriter, mode: str) -> None:
        self.writer = writer
        self.mode = mode

    async def readline(self) -> bytes:
        if self.mode == "empty":
            return b""
        if self.mode == "invalid":
            return b"not-json\n"
        request = json.loads(self.writer.data)
        identifier = "wrong" if self.mode == "mismatch" else request["id"]
        if self.mode == "error":
            return (
                json.dumps(
                    {"id": identifier, "error": {"code": "conflict", "message": "changed"}}
                ).encode()
                + b"\n"
            )
        return json.dumps({"id": identifier, "result": {"ok": True}}).encode() + b"\n"


class ClientWriter(MemoryWriter):
    pass


async def test_core_client_validates_transport_responses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = CoreClient(tmp_path / "query.sock")

    async def connection(mode: str) -> tuple[ClientReader, ClientWriter]:
        writer = ClientWriter()
        return ClientReader(writer, mode), writer

    for mode, error_type in [
        ("empty", CoreUnavailableError),
        ("invalid", CoreUnavailableError),
        ("mismatch", CoreUnavailableError),
        ("error", CoreRequestError),
    ]:

        async def open_connection(
            path: str, selected: str = mode, **_: object
        ) -> tuple[ClientReader, ClientWriter]:
            return await connection(selected)

        monkeypatch.setattr(asyncio, "open_unix_connection", open_connection, raising=False)
        with pytest.raises(error_type):
            await client.call("bootstrap")

    async def open_ok(path: str, **_: object) -> tuple[ClientReader, ClientWriter]:
        return await connection("ok")

    monkeypatch.setattr(asyncio, "open_unix_connection", open_ok, raising=False)
    assert await client.call("bootstrap") == {"ok": True}
    with pytest.raises(CoreRequestError, match="size limit"):
        await client.call("bootstrap", {"body": "x" * MAX_RPC_BYTES})

    async def unavailable(path: str, **_: object) -> tuple[ClientReader, ClientWriter]:
        raise OSError("missing socket")

    monkeypatch.setattr(asyncio, "open_unix_connection", unavailable, raising=False)
    with pytest.raises(CoreUnavailableError):
        await client.call("bootstrap")


async def test_core_client_reads_response_larger_than_default_stream_limit(
    tmp_path: Path,
) -> None:
    """Regression: a response line larger than asyncio's default 64 KiB StreamReader
    limit must round-trip. The client opened the socket without limit=MAX_RPC_BYTES, so
    readline() raised ValueError on >64 KiB payloads (e.g. a 24h metrics window ~79 KiB),
    surfacing as an unhandled 500 in the web console's metric explorer. Uses a real Unix
    socket on purpose: the mocked-transport test above cannot observe the reader limit."""
    asyncio_unix: Any = asyncio
    socket_path = tmp_path / "query.sock"
    blob = "x" * 200_000  # > 64 KiB default limit, < MAX_RPC_BYTES

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        raw = await reader.readline()
        request = json.loads(raw)
        writer.write(
            json.dumps({"id": request["id"], "result": {"blob": blob}}).encode() + b"\n"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio_unix.start_unix_server(
        handle, path=str(socket_path), limit=MAX_RPC_BYTES
    )
    try:
        result = await CoreClient(socket_path).call("metrics")
    finally:
        server.close()
        await server.wait_closed()
    assert result == {"blob": blob}


async def test_core_client_rejects_response_exceeding_max_rpc_bytes(
    tmp_path: Path,
) -> None:
    """A response beyond MAX_RPC_BYTES must fail closed as CoreUnavailableError, never a
    raw ValueError bubbling up as a 500."""
    asyncio_unix: Any = asyncio
    socket_path = tmp_path / "query.sock"
    huge = "x" * (MAX_RPC_BYTES + 50_000)

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        raw = await reader.readline()
        request = json.loads(raw)
        writer.write(
            json.dumps({"id": request["id"], "result": {"blob": huge}}).encode() + b"\n"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio_unix.start_unix_server(
        handle, path=str(socket_path), limit=MAX_RPC_BYTES + 100_000
    )
    try:
        with pytest.raises(CoreUnavailableError):
            await CoreClient(socket_path).call("metrics")
    finally:
        server.close()
        await server.wait_closed()


async def test_core_explicit_lifespan_opens_and_closes_database(
    settings: AppSettings, tmp_path: Path
) -> None:
    path = tmp_path / "lifespan.sqlite3"
    await migrate_database(path)
    local_settings = settings.model_copy(update={"db_path": path})
    service = CoreService(local_settings)
    async with service.lifespan():
        assert (await service.dispatch("query", "bootstrap", {}))["assets"]
    assert service.database.connection is None
