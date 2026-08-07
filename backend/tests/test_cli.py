from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from signal_room import cli
from signal_room.config import AppSettings


class Runnable:
    def __init__(self) -> None:
        self.once = 0
        self.forever = 0
        self.stopped = 0

    def stop(self) -> None:
        self.stopped += 1

    async def collect_once(self) -> None:
        self.once += 1

    async def run_once(self) -> None:
        self.once += 1

    async def run_forever(self) -> None:
        self.forever += 1


async def test_async_role_helpers_use_scoped_clients_and_modes(
    settings: AppSettings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    core_service = SimpleNamespace(served=False)

    async def serve_forever() -> None:
        core_service.served = True

    core_service.serve_forever = serve_forever
    monkeypatch.setattr(cli, "CoreService", lambda value: core_service)
    await cli._core(settings)
    assert core_service.served

    collector = Runnable()
    notifier = Runnable()
    clients: list[Path] = []
    installed: list[Any] = []

    class FakeClient:
        def __init__(self, path: Path) -> None:
            clients.append(path)

        async def call(self, method: str, params: dict[str, Any]) -> dict[str, str]:
            assert method == "backup"
            assert Path(params["destination"]).is_absolute()
            return {"path": "/backup/verified.sqlite3"}

    monkeypatch.setattr(cli, "CoreClient", FakeClient)
    monkeypatch.setattr(cli, "Collector", lambda *args: collector)
    monkeypatch.setattr(cli, "Notifier", lambda *args: notifier)
    monkeypatch.setattr(cli, "load_topology", lambda path: object())
    monkeypatch.setattr(cli, "_install_stop_handlers", installed.append)

    await cli._collect(settings, True)
    await cli._collect(settings, False)
    await cli._notify(settings, True)
    await cli._notify(settings, False)
    await cli._backup(settings, tmp_path / "backups")
    assert collector.once == collector.forever == 1
    assert notifier.once == notifier.forever == 1
    assert installed == [collector.stop, collector.stop, notifier.stop, notifier.stop]
    assert clients == [
        settings.ingest_socket,
        settings.ingest_socket,
        settings.notifier_socket,
        settings.notifier_socket,
        settings.maintenance_socket,
    ]
    assert "/backup/verified.sqlite3" in capsys.readouterr().out

    exported: dict[str, Any] = {}

    async def export(topology: object, runbooks: object, output: Path) -> None:
        exported.update(topology=topology, runbooks=runbooks, output=output)

    monkeypatch.setattr(cli, "export_pressure_drop", export)
    monkeypatch.setattr(cli, "load_topology", lambda path: "topology")
    monkeypatch.setattr(cli, "load_runbooks", lambda path: "runbooks")
    await cli._export_demo(settings, tmp_path / "demo.json")
    assert exported == {
        "topology": "topology",
        "runbooks": "runbooks",
        "output": tmp_path / "demo.json",
    }


async def test_stop_handler_installation_tolerates_unsupported_loops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Loop:
        def __init__(self) -> None:
            self.calls = 0

        def add_signal_handler(self, value: object, stop: object) -> None:
            self.calls += 1
            if self.calls == 1:
                raise NotImplementedError
            raise RuntimeError

    loop = Loop()
    monkeypatch.setattr(cli.asyncio, "get_running_loop", lambda: loop)
    cli._install_stop_handlers(lambda: None)
    assert loop.calls == 2


def test_parser_exposes_every_role_command() -> None:
    parser = cli.build_parser()
    assert parser.parse_args(["core"]).command == "core"
    assert parser.parse_args(["serve", "--port", "9000"]).port == 9000
    assert parser.parse_args(["collect", "--once"]).once
    assert parser.parse_args(["notify", "--once"]).once
    assert parser.parse_args(["migrate", "--backup-directory", "safe"]).backup_directory == Path(
        "safe"
    )
    assert parser.parse_args(["backup", "safe"]).destination == Path("safe")
    assert parser.parse_args(["validate-config"]).schema_output is None
    assert parser.parse_args(["export-demo"]).output.name == "pressure-drop.json"


def test_main_dispatches_all_commands(
    settings: AppSettings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[str, Any]] = []

    async def core(value: AppSettings) -> None:
        calls.append(("core", value))

    async def collect(value: AppSettings, once: bool) -> None:
        calls.append(("collect", once))

    async def notify(value: AppSettings, once: bool) -> None:
        calls.append(("notify", once))

    async def backup(value: AppSettings, destination: Path) -> None:
        calls.append(("backup", destination))

    async def export(value: AppSettings, output: Path) -> None:
        calls.append(("export", output))

    async def migrate(path: Path, backup_directory: Path | None) -> int:
        calls.append(("migrate", backup_directory))
        return 4

    monkeypatch.setattr(cli, "AppSettings", lambda: settings)
    monkeypatch.setattr(cli, "_core", core)
    monkeypatch.setattr(cli, "_collect", collect)
    monkeypatch.setattr(cli, "_notify", notify)
    monkeypatch.setattr(cli, "_backup", backup)
    monkeypatch.setattr(cli, "_export_demo", export)
    monkeypatch.setattr(cli, "migrate_database", migrate)

    app = object()
    monkeypatch.setattr(cli, "create_app", lambda value: app)
    monkeypatch.setattr(
        cli.uvicorn,
        "run",
        lambda application, **kwargs: calls.append(("serve", (application, kwargs))),
    )
    monkeypatch.setattr(
        cli, "load_topology", lambda path: SimpleNamespace(version=2, revision="r2")
    )
    monkeypatch.setattr(cli, "load_runbooks", lambda path: object())
    monkeypatch.setattr(cli, "export_config_schema", lambda path: calls.append(("schema", path)))

    commands = [
        ["core"],
        ["serve", "--host", "127.0.0.2", "--port", "9090"],
        ["collect", "--once"],
        ["notify", "--once"],
        ["migrate", "--backup-directory", str(tmp_path / "pre")],
        ["backup", str(tmp_path / "daily")],
        ["validate-config", "--schema-output", str(tmp_path / "schema.json")],
        ["export-demo", "--output", str(tmp_path / "demo.json")],
    ]
    for arguments in commands:
        monkeypatch.setattr(sys, "argv", ["signal-room", *arguments])
        cli.main()

    names = [name for name, _ in calls]
    assert names == [
        "core",
        "serve",
        "collect",
        "notify",
        "migrate",
        "backup",
        "schema",
        "export",
    ]
    serve = calls[1][1]
    assert serve[0] is app
    assert serve[1]["port"] == 9090
    assert serve[1]["server_header"] is False
    output = capsys.readouterr().out
    assert "schema 4" in output
    assert '"revision": "r2"' in output
