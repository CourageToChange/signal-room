from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
from collections.abc import Callable
from pathlib import Path

import uvicorn

from .api import create_app
from .collector import Collector
from .config import AppSettings, export_config_schema, load_runbooks, load_topology
from .core import CoreClient, CoreService
from .demo import export_pressure_drop
from .migrate import migrate_database
from .notifier import Notifier


def _install_stop_handlers(stop: Callable[[], None]) -> None:
    loop = asyncio.get_running_loop()
    for name in ("SIGTERM", "SIGINT"):
        value = getattr(signal, name, None)
        if value is None:
            continue
        try:
            loop.add_signal_handler(value, stop)
        except (NotImplementedError, RuntimeError):
            pass


async def _core(settings: AppSettings) -> None:
    service = CoreService(settings)
    await service.serve_forever()


async def _collect(settings: AppSettings, once: bool) -> None:
    collector = Collector(
        settings,
        load_topology(settings.config_path),
        CoreClient(settings.ingest_socket),
    )
    _install_stop_handlers(collector.stop)
    if once:
        await collector.collect_once()
    else:
        await collector.run_forever()


async def _notify(settings: AppSettings, once: bool) -> None:
    notifier = Notifier(settings, CoreClient(settings.notifier_socket))
    _install_stop_handlers(notifier.stop)
    if once:
        await notifier.run_once()
    else:
        await notifier.run_forever()


async def _backup(settings: AppSettings, destination: Path) -> None:
    result = await CoreClient(settings.maintenance_socket).call(
        "backup", {"destination": str(destination.resolve())}
    )
    print(result["path"])


async def _export_demo(settings: AppSettings, output: Path) -> None:
    await export_pressure_drop(
        load_topology(settings.config_path), load_runbooks(settings.runbooks_path), output
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="signal-room")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("core", help="run the sole database owner and incident engine")
    serve = subcommands.add_parser("serve", help="serve the private API and built frontend")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    collect = subcommands.add_parser("collect", help="run read-only telemetry providers")
    collect.add_argument("--once", action="store_true")
    notify = subcommands.add_parser("notify", help="deliver the signed notification outbox")
    notify.add_argument("--once", action="store_true")
    migrate = subcommands.add_parser("migrate", help="run ordered one-shot database migrations")
    migrate.add_argument("--backup-directory", type=Path)
    backup = subcommands.add_parser("backup", help="request a verified online SQLite backup")
    backup.add_argument("destination", type=Path)
    validate = subcommands.add_parser("validate-config", help="strictly validate v2 configuration")
    validate.add_argument("--schema-output", type=Path)
    export = subcommands.add_parser("export-demo", help="generate the fictional static drill")
    export.add_argument(
        "--output",
        type=Path,
        default=Path("frontend/src/demo/generated/pressure-drop.json"),
    )
    return parser


def main() -> None:
    parser = build_parser()
    arguments = parser.parse_args()
    settings = AppSettings()
    settings.assert_command_role(arguments.command)
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if arguments.command == "core":
        asyncio.run(_core(settings))
    elif arguments.command == "serve":
        uvicorn.run(
            create_app(settings),
            host=arguments.host,
            port=arguments.port,
            log_level=settings.log_level.lower(),
            server_header=False,
            proxy_headers=False,
        )
    elif arguments.command == "collect":
        asyncio.run(_collect(settings, arguments.once))
    elif arguments.command == "notify":
        asyncio.run(_notify(settings, arguments.once))
    elif arguments.command == "migrate":
        version = asyncio.run(migrate_database(settings.db_path, arguments.backup_directory))
        print(f"schema {version}")
    elif arguments.command == "backup":
        asyncio.run(_backup(settings, arguments.destination))
    elif arguments.command == "validate-config":
        topology = load_topology(settings.config_path)
        load_runbooks(settings.runbooks_path)
        if arguments.schema_output:
            export_config_schema(arguments.schema_output)
        print(json.dumps({"version": topology.version, "revision": topology.revision}))
    elif arguments.command == "export-demo":
        asyncio.run(_export_demo(settings, arguments.output))


if __name__ == "__main__":
    main()
