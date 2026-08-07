#!/usr/bin/env python3
"""Fail-closed 24-hour staging monitor for one immutable Signal Room release."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import signal
import sqlite3
import stat

# This monitor invokes fixed absolute executables and never a shell.
import subprocess  # nosec B404
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

APPLICATION_UNITS = (
    "signal-room-core.service",
    "signal-room-collector.service",
    "signal-room-web.service",
    "signal-room-notifier.service",
)
TUNNEL_UNIT = "cloudflared.service"
BASE_ACTIVE_UNITS = ("signal-room.target", *APPLICATION_UNITS, "signal-room-backup.timer")
EXPECTED_GROUPS = {
    "signal-room-core.service": [910, 911, 915, 916, 917],
    "signal-room-collector.service": [910, 912, 916],
    "signal-room-web.service": [910, 913, 915],
    "signal-room-notifier.service": [910, 914, 917],
}
EXPECTED_SOCKETS = {
    "/run/signal-room/query.sock": (0o660, 911, 915),
    "/run/signal-room/ingest.sock": (0o660, 911, 916),
    "/run/signal-room/notifier.sock": (0o660, 911, 917),
    "/run/signal-room/maintenance.sock": (0o660, 911, 911),
}
PROHIBITED_PACKAGES = (
    "openssh-server",
    "openssh-sftp-server",
    "postfix",
    "docker.io",
    "docker-ce",
)
PROHIBITED_PROCESSES = {"sshd", "dockerd", "containerd", "master"}
EXPECTED_NFT_CHAINS = {"input": "drop", "output": "drop"}
EXPECTED_NFT_SETS = {"denied_v4", "cloudflare_tunnel_v4"}
EXPECTED_NFT_ACCEPT_COMMENTS = {
    "loopback input",
    "established input",
    "DHCP reply during staging",
    "loopback output",
    "established output",
    "DHCP request during staging",
    "DNS UDP",
    "DNS TCP",
    "read-only PVE API",
    "Tunnel QUIC",
    "Tunnel HTTP/2",
    "public HTTPS; app also pins public A/AAAA answers",
}
STOP_REASON: str | None = None


class GateFailure(RuntimeError):
    """Raised when a soak invariant fails."""


def utc_now() -> datetime:
    return datetime.now(UTC)


def command(*arguments: str, timeout: int = 30, check: bool = True) -> str:
    environment = {
        "HOME": "/",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    }
    # Every caller supplies an internal fixed argv list.
    completed = subprocess.run(  # noqa: S603  # nosec B603
        arguments,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=environment,
    )
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()[-500:]
        raise GateFailure(f"command failed ({completed.returncode}): {arguments[0]}: {detail}")
    return completed.stdout.strip()


def systemd_property(unit: str, name: str) -> str:
    return command("/usr/bin/systemctl", "show", unit, f"--property={name}", "--value")


def request_json(path: str) -> tuple[dict[str, Any], float]:
    started = time.perf_counter()
    connection = http.client.HTTPConnection("127.0.0.1", 8080, timeout=10)
    try:
        connection.request("GET", path, headers={"Accept": "application/json"})
        response = connection.getresponse()
        payload = response.read(1_048_577)
        if response.status != 200:
            raise GateFailure(f"{path} returned HTTP {response.status}")
        if len(payload) > 1_048_576:
            raise GateFailure(f"{path} exceeded the monitor response limit")
        decoded = json.loads(payload)
        if not isinstance(decoded, dict):
            raise GateFailure(f"{path} did not return an object")
        return decoded, (time.perf_counter() - started) * 1_000
    except (OSError, json.JSONDecodeError) as error:
        raise GateFailure(f"{path} failed: {type(error).__name__}: {error}") from error
    finally:
        connection.close()


def check_sse() -> float:
    started = time.perf_counter()
    connection = http.client.HTTPConnection("127.0.0.1", 8080, timeout=20)
    try:
        connection.request("GET", "/api/v1/stream", headers={"Accept": "text/event-stream"})
        response = connection.getresponse()
        if response.status != 200:
            raise GateFailure(f"SSE returned HTTP {response.status}")
        if not response.getheader("Content-Type", "").startswith("text/event-stream"):
            raise GateFailure("SSE returned the wrong content type")
        deadline = time.monotonic() + 19
        while time.monotonic() < deadline:
            line = response.readline(65_537)
            if len(line) > 65_536:
                raise GateFailure("SSE line exceeded the monitor limit")
            if line.startswith(b"data:"):
                return (time.perf_counter() - started) * 1_000
            if not line:
                break
        raise GateFailure("SSE did not produce an event or heartbeat")
    except (OSError, http.client.HTTPException) as error:
        raise GateFailure(f"SSE failed: {type(error).__name__}: {error}") from error
    finally:
        connection.close()


def parse_status(pid: int) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        process_status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except OSError as error:
        raise GateFailure(f"cannot read status for PID {pid}: {error}") from error
    for line in process_status.splitlines():
        name, separator, value = line.partition(":")
        if separator:
            values[name] = value.strip()
    return values


def parse_kib(status: dict[str, str], name: str) -> int:
    fields = status.get(name, "").split()
    if not fields or not fields[0].isdigit():
        raise GateFailure(f"malformed {name} in process status")
    return int(fields[0])


def _validate_process_identity(pid: int, user: str, group: str) -> None:
    status = parse_status(pid)
    raw_uid = command("/usr/bin/id", "-u", user)
    raw_gid = command("/usr/bin/id", "-g", user)
    if not raw_uid.isdigit() or not raw_gid.isdigit() or user != group:
        raise GateFailure(f"cannot resolve the expected {user} service identity")
    expected_uid = int(raw_uid)
    expected_gid = int(raw_gid)
    uids = {int(value) for value in status.get("Uid", "").split()}
    gids = {int(value) for value in status.get("Gid", "").split()}
    groups = [int(value) for value in status.get("Groups", "").split()]
    if uids != {expected_uid} or gids != {expected_gid} or groups != [expected_gid]:
        raise GateFailure(
            f"{user} process identity drifted: uids={sorted(uids)} "
            f"gids={sorted(gids)} groups={groups}"
        )


def check_units(require_tunnel: bool) -> tuple[int, int]:
    active_units = (*BASE_ACTIVE_UNITS, *((TUNNEL_UNIT,) if require_tunnel else ()))
    for unit in active_units:
        if command("/usr/bin/systemctl", "is-active", unit) != "active":
            raise GateFailure(f"{unit} is not active")
    if systemd_property("signal-room-migrate.service", "Result") != "success":
        raise GateFailure("migration unit no longer reports success")
    if systemd_property("signal-room-migrate.service", "ExecMainStatus") != "0":
        raise GateFailure("migration unit has a nonzero exit status")
    if command("/usr/bin/systemctl", "is-system-running") != "running":
        raise GateFailure("systemd is not in the running state")
    if command("/usr/bin/systemctl", "--failed", "--no-legend", "--plain"):
        raise GateFailure("systemd has failed units")

    total_rss = 0
    total_swap = 0
    for unit in APPLICATION_UNITS:
        if systemd_property(unit, "NRestarts") != "0":
            raise GateFailure(f"{unit} restarted during the soak")
        raw_pid = systemd_property(unit, "MainPID")
        if not raw_pid.isdigit() or int(raw_pid) <= 0:
            raise GateFailure(f"{unit} has no live main PID")
        process_status = parse_status(int(raw_pid))
        groups = [int(value) for value in process_status.get("Groups", "").split()]
        if groups != EXPECTED_GROUPS[unit]:
            raise GateFailure(f"{unit} group drift: {groups}")
        total_rss += parse_kib(process_status, "VmRSS")
        total_swap += parse_kib(process_status, "VmSwap")
    if require_tunnel:
        if systemd_property(TUNNEL_UNIT, "NRestarts") != "0":
            raise GateFailure(f"{TUNNEL_UNIT} restarted during the soak")
        raw_pid = systemd_property(TUNNEL_UNIT, "MainPID")
        if not raw_pid.isdigit() or int(raw_pid) <= 0:
            raise GateFailure(f"{TUNNEL_UNIT} has no live main PID")
        tunnel_pid = int(raw_pid)
        _validate_process_identity(tunnel_pid, "cloudflared", "cloudflared")
        try:
            executable = Path(f"/proc/{tunnel_pid}/exe").resolve(strict=True)
        except OSError as error:
            raise GateFailure(f"cannot resolve {TUNNEL_UNIT} executable: {error}") from error
        if executable != Path("/usr/bin/cloudflared"):
            raise GateFailure(f"{TUNNEL_UNIT} executable drifted: {executable}")
        process_status = parse_status(tunnel_pid)
        total_rss += parse_kib(process_status, "VmRSS")
        total_swap += parse_kib(process_status, "VmSwap")
    return total_rss, total_swap


def check_listener_and_isolation(require_tunnel: bool) -> None:
    listeners = []
    for line in command("/usr/bin/ss", "-H", "-ltn").splitlines():
        fields = line.split()
        if len(fields) >= 4:
            listeners.append(fields[3])
    expected_listeners = ["127.0.0.1:8080"]
    if require_tunnel:
        expected_listeners.append("127.0.0.1:20241")
    if sorted(listeners) != sorted(expected_listeners):
        raise GateFailure(f"unexpected TCP listeners: {listeners}")

    process_names = set(command("/usr/bin/ps", "-e", "-o", "comm=").split())
    prohibited = sorted(process_names & PROHIBITED_PROCESSES)
    if prohibited:
        raise GateFailure(f"prohibited processes present: {prohibited}")
    for package in PROHIBITED_PACKAGES:
        # Package names come only from the fixed PROHIBITED_PACKAGES tuple.
        result = subprocess.run(  # noqa: S603  # nosec B603
            ["/usr/bin/dpkg-query", "-W", "-f=${db:Status-Abbrev}", package],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.startswith("ii"):
            raise GateFailure(f"prohibited package installed: {package}")

    for interface in ("all", "eth0"):
        disabled = Path(f"/proc/sys/net/ipv6/conf/{interface}/disable_ipv6")
        if disabled.read_text().strip() != "1":
            raise GateFailure(f"IPv6 is enabled on {interface}")
    ipv6_interfaces = {
        line.split()[-1]
        for line in Path("/proc/net/if_inet6").read_text(encoding="ascii").splitlines()
    }
    if "eth0" in ipv6_interfaces:
        raise GateFailure("eth0 acquired an IPv6 address")

    runtime = os.stat("/run/signal-room")
    if (stat.S_IMODE(runtime.st_mode), runtime.st_uid, runtime.st_gid) != (0o711, 911, 911):
        raise GateFailure("runtime directory ownership or mode drifted")
    for path, expected in EXPECTED_SOCKETS.items():
        socket_status = os.stat(path)
        actual = (stat.S_IMODE(socket_status.st_mode), socket_status.st_uid, socket_status.st_gid)
        if actual != expected or not stat.S_ISSOCK(socket_status.st_mode):
            raise GateFailure(f"socket ownership or mode drifted: {path}: {actual}")


def _contains_accept(value: object) -> bool:
    if isinstance(value, dict):
        return "accept" in value or any(_contains_accept(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_accept(item) for item in value)
    return False


def validate_nftables_ruleset(payload: object) -> None:
    if not isinstance(payload, dict) or not isinstance(payload.get("nftables"), list):
        raise GateFailure("nftables JSON is malformed")
    chains: dict[str, str] = {}
    tables: set[tuple[str, str]] = set()
    sets: set[str] = set()
    accept_comments: list[str] = []
    for item in payload["nftables"]:
        if not isinstance(item, dict):
            raise GateFailure("nftables JSON contains a malformed object")
        table = item.get("table")
        if isinstance(table, dict):
            family = table.get("family")
            name = table.get("name")
            if not isinstance(family, str) or not isinstance(name, str):
                raise GateFailure("nftables table is malformed")
            tables.add((family, name))
        set_data = item.get("set")
        if isinstance(set_data, dict):
            if (
                set_data.get("family") != "inet"
                or set_data.get("table") != "signal_room_filter"
                or not isinstance(set_data.get("name"), str)
            ):
                raise GateFailure("an unexpected nftables set is active")
            sets.add(set_data["name"])
        chain = item.get("chain")
        if (
            isinstance(chain, dict)
            and chain.get("family") == "inet"
            and chain.get("table") == "signal_room_filter"
        ):
            name = chain.get("name")
            policy = chain.get("policy")
            if not isinstance(name, str) or not isinstance(policy, str):
                raise GateFailure("Signal Room nftables chain is malformed")
            chains[name] = policy
        elif isinstance(chain, dict):
            raise GateFailure("an unexpected nftables chain is active")
        rule = item.get("rule")
        if not isinstance(rule, dict):
            continue
        if rule.get("family") != "inet" or rule.get("table") != "signal_room_filter":
            raise GateFailure("an unexpected nftables rule is active")
        if _contains_accept(rule.get("expr")):
            comment = rule.get("comment")
            if not isinstance(comment, str):
                raise GateFailure("an nftables accept rule has no approved comment")
            accept_comments.append(comment)
    if tables != {("inet", "signal_room_filter")}:
        raise GateFailure(f"nftables table set drifted: {sorted(tables)}")
    if sets != EXPECTED_NFT_SETS:
        raise GateFailure(f"nftables named sets drifted: {sorted(sets)}")
    if chains != EXPECTED_NFT_CHAINS:
        raise GateFailure(f"nftables chain policy drifted: {chains}")
    if len(accept_comments) != len(set(accept_comments)):
        raise GateFailure("nftables contains duplicate accept rules")
    if set(accept_comments) != EXPECTED_NFT_ACCEPT_COMMENTS:
        raise GateFailure(f"nftables accept rules drifted: {sorted(accept_comments)}")


def check_nftables() -> None:
    if command("/usr/bin/systemctl", "is-active", "nftables.service") != "active":
        raise GateFailure("nftables.service is not active")
    if command("/usr/bin/systemctl", "is-enabled", "nftables.service") != "enabled":
        raise GateFailure("nftables.service is not enabled")
    config = Path("/etc/nftables.conf")
    try:
        metadata = config.stat()
        content = config.read_text(encoding="utf-8")
    except OSError as error:
        raise GateFailure(f"cannot inspect nftables configuration: {error}") from error
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o022:
        raise GateFailure("nftables configuration ownership or mode is unsafe")
    if "DNS_IP_" in content or "PVE_API_IP" in content:
        raise GateFailure("nftables configuration still contains address placeholders")
    command("/usr/sbin/nft", "--check", "--file", str(config))
    try:
        payload = json.loads(
            command(
                "/usr/sbin/nft",
                "--json",
                "list",
                "ruleset",
            )
        )
    except json.JSONDecodeError as error:
        raise GateFailure("nftables returned malformed JSON") from error
    validate_nftables_ruleset(payload)


def request_tunnel_endpoint(path: str) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", 20241, timeout=10)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        payload = response.read(1_048_577)
        if len(payload) > 1_048_576:
            raise GateFailure(f"cloudflared {path} exceeded the monitor response limit")
        return response.status, payload
    except (OSError, http.client.HTTPException) as error:
        raise GateFailure(f"cloudflared {path} failed: {type(error).__name__}: {error}") from error
    finally:
        connection.close()


def parse_tunnel_connections(metrics: str) -> float:
    values: list[float] = []
    pattern = re.compile(
        r"^cloudflared_tunnel_ha_connections(?:\{[^}]*\})?\s+"
        r"([0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)$"
    )
    for line in metrics.splitlines():
        match = pattern.fullmatch(line.strip())
        if match:
            values.append(float(match.group(1)))
    if not values:
        raise GateFailure("cloudflared connection metric is missing")
    connections = sum(values)
    if connections < 1:
        raise GateFailure("cloudflared has no active edge connection")
    return connections


def check_tunnel() -> float:
    ready_status, _ = request_tunnel_endpoint("/ready")
    if ready_status != 200:
        raise GateFailure(f"cloudflared readiness returned HTTP {ready_status}")
    metrics_status, metrics_payload = request_tunnel_endpoint("/metrics")
    if metrics_status != 200:
        raise GateFailure(f"cloudflared metrics returned HTTP {metrics_status}")
    try:
        metrics = metrics_payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise GateFailure("cloudflared metrics are not UTF-8") from error
    return parse_tunnel_connections(metrics)


def check_backup() -> dict[str, Any]:
    backup_directory = Path("/var/backups/signal-room")
    backups = sorted(
        backup_directory.glob("signal-room-*.sqlite3"),
        key=lambda item: item.stat().st_mtime,
    )
    if not backups:
        raise GateFailure("no internal SQLite backup exists")
    backup = backups[-1]
    checksum = backup.with_name(f"{backup.name}.sha256")
    if not checksum.is_file():
        raise GateFailure(f"missing checksum for {backup.name}")
    recorded = checksum.read_text(encoding="utf-8").split()
    if len(recorded) < 2 or recorded[1].lstrip("*") != backup.name:
        raise GateFailure(f"malformed checksum file for {backup.name}")
    digest = hashlib.sha256(backup.read_bytes()).hexdigest()
    if digest != recorded[0]:
        raise GateFailure(f"checksum mismatch for {backup.name}")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{backup}?mode=ro&immutable=1", uri=True)
        connection.execute("PRAGMA query_only = ON")
        quick = [row[0] for row in connection.execute("PRAGMA quick_check")]
        integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))
    except sqlite3.Error as error:
        raise GateFailure(f"database checks failed for {backup.name}: {error}") from error
    finally:
        if connection is not None:
            connection.close()
    if quick != ["ok"] or integrity != ["ok"] or foreign_keys:
        raise GateFailure(f"database checks failed for {backup.name}")
    return {
        "name": backup.name,
        "mtime": datetime.fromtimestamp(backup.stat().st_mtime, UTC).isoformat(),
        "sha256": digest,
    }


def journal_lines(*arguments: str) -> list[str]:
    output = command("/usr/bin/journalctl", *arguments, "--no-pager", "--quiet")
    return [line for line in output.splitlines() if line.strip()]


def check_journal(started_at: datetime) -> None:
    since = started_at.isoformat()
    errors = journal_lines("--boot", f"--since={since}", "--priority=err")
    if errors:
        raise GateFailure(f"system error journal entries appeared: {errors[-3:]}")
    warnings = journal_lines(
        "--boot",
        f"--since={since}",
        "--unit=signal-room-core.service",
        "--unit=signal-room-collector.service",
        "--unit=signal-room-web.service",
        "--unit=signal-room-notifier.service",
        "--priority=warning",
    )
    if warnings:
        raise GateFailure(f"Signal Room warning journal entries appeared: {warnings[-3:]}")
    kernel = "\n".join(journal_lines("--boot", "--dmesg", f"--since={since}")).lower()
    if any(marker in kernel for marker in ("out of memory", "oom-kill", "killed process")):
        raise GateFailure("kernel OOM evidence appeared")


def expected_release_path(version: str, build_sha: str) -> str:
    return f"/opt/signal-room/releases/{version}-{build_sha}"


def validate_notifications(value: object) -> None:
    if not isinstance(value, dict):
        raise GateFailure("notification diagnostics are malformed")
    if value.get("enabled") is not False:
        raise GateFailure(f"notifications unexpectedly enabled: {value}")
    if value.get("pending") != 0 or value.get("dead_letter") != 0:
        raise GateFailure(f"disabled notification state drifted: {value}")


def collect_sample(
    arguments: argparse.Namespace, started_at: datetime, boot_id: str
) -> dict[str, Any]:
    if Path("/proc/sys/kernel/random/boot_id").read_text().strip() != boot_id:
        raise GateFailure("the target container rebooted during the soak")
    current = str(Path("/opt/signal-room/current").resolve())
    expected = expected_release_path(arguments.expected_version, arguments.expected_sha)
    if current != expected:
        raise GateFailure(f"release selector drifted: {current}")

    health, health_ms = request_json("/api/health/ready")
    if health != {
        "ok": True,
        "database": "ready",
        "collector_fresh": True,
        "providers_fresh": True,
    }:
        raise GateFailure(f"readiness degraded: {health}")
    diagnostics, diagnostics_ms = request_json("/api/v1/diagnostics")
    if diagnostics.get("build_sha") != arguments.expected_sha:
        raise GateFailure("diagnostics build SHA drifted")
    if diagnostics.get("build_version") != arguments.expected_version:
        raise GateFailure("diagnostics release version drifted")
    if diagnostics.get("schema_version") != arguments.expected_schema:
        raise GateFailure("diagnostics schema version drifted")
    if diagnostics.get("configuration_revision") != arguments.expected_config_revision:
        raise GateFailure("diagnostics configuration revision drifted")
    if diagnostics.get("database_ok") is not True:
        raise GateFailure("diagnostics database degraded")
    if diagnostics.get("collector_fresh") is not True:
        raise GateFailure("diagnostics collector degraded")

    providers = diagnostics.get("providers")
    if not isinstance(providers, list) or not providers:
        raise GateFailure("diagnostics has no providers")
    provider_names = {provider.get("provider") for provider in providers}
    if provider_names != set(arguments.expected_provider):
        raise GateFailure(f"diagnostics provider set drifted: {sorted(provider_names)}")
    for provider in providers:
        if provider.get("status") != "healthy" or provider.get("consecutive_failures") != 0:
            raise GateFailure(f"provider degraded: {provider}")
        if not provider.get("last_success_at"):
            raise GateFailure(f"provider has no successful run: {provider}")
    validate_notifications(diagnostics.get("notifications"))

    rss_kib, process_swap_kib = check_units(arguments.require_tunnel)
    if rss_kib >= arguments.max_rss_mib * 1024:
        raise GateFailure(f"Signal Room RSS exceeded limit: {rss_kib / 1024:.2f} MiB")
    if process_swap_kib != 0:
        raise GateFailure(f"Signal Room process swap is nonzero: {process_swap_kib} KiB")
    meminfo = {
        line.partition(":")[0]: int(line.partition(":")[2].split()[0])
        for line in Path("/proc/meminfo").read_text().splitlines()
        if line.partition(":")[2].strip()
    }
    system_swap_kib = meminfo["SwapTotal"] - meminfo["SwapFree"]
    if system_swap_kib != 0:
        raise GateFailure(f"system swap is nonzero: {system_swap_kib} KiB")

    check_listener_and_isolation(arguments.require_tunnel)
    check_nftables()
    tunnel_connections = check_tunnel() if arguments.require_tunnel else None
    backup = check_backup()
    check_journal(started_at)
    sse_ms = check_sse()
    return {
        "at": utc_now().isoformat(),
        "health_ms": round(health_ms, 2),
        "diagnostics_ms": round(diagnostics_ms, 2),
        "sse_first_data_ms": round(sse_ms, 2),
        "rss_mib": round(rss_kib / 1024, 2),
        "process_swap_kib": process_swap_kib,
        "system_swap_kib": system_swap_kib,
        "configuration_revision": diagnostics.get("configuration_revision"),
        "tunnel_connections": tunnel_connections,
        "providers": {provider["provider"]: provider["last_success_at"] for provider in providers},
        "backup": backup,
    }


def append_json(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
        output.flush()
        os.fsync(output.fileno())


def write_state(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(f"{path.suffix}.partial")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(payload, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def on_stop(signum: int, _frame: object) -> None:
    global STOP_REASON
    STOP_REASON = signal.Signals(signum).name
    raise GateFailure(f"monitor terminated by {STOP_REASON}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--expected-version", default="1.0.0")
    parser.add_argument("--expected-schema", type=int, required=True)
    parser.add_argument("--expected-config-revision", required=True)
    parser.add_argument("--expected-provider", action="append", default=["backup", "proxmox"])
    parser.add_argument("--duration-seconds", type=int, default=86_400)
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument("--max-rss-mib", type=int, default=650)
    parser.add_argument("--require-fresh-backup", action="store_true")
    parser.add_argument("--require-provider-progress", action="store_true")
    parser.add_argument("--require-tunnel", action="store_true")
    parser.add_argument("--output-directory", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.duration_seconds < 1 or arguments.interval_seconds < 1:
        parser.error("durations must be positive")

    arguments.output_directory.mkdir(parents=True, exist_ok=True)
    evidence_path = arguments.output_directory / "evidence.jsonl"
    state_path = arguments.output_directory / "state.json"
    started_at = utc_now()
    deadline_at = started_at + timedelta(seconds=arguments.duration_seconds)
    started_monotonic = time.monotonic()
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    samples: list[dict[str, Any]] = []
    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)

    base_state: dict[str, Any] = {
        "build_sha": arguments.expected_sha,
        "version": arguments.expected_version,
        "schema": arguments.expected_schema,
        "configuration_revision": arguments.expected_config_revision,
        "boot_id": boot_id,
        "started_at": started_at.isoformat(),
        "deadline_at": deadline_at.isoformat(),
        "duration_seconds": arguments.duration_seconds,
        "interval_seconds": arguments.interval_seconds,
        "require_fresh_backup": arguments.require_fresh_backup,
        "require_provider_progress": arguments.require_provider_progress,
        "require_tunnel": arguments.require_tunnel,
    }
    append_json(evidence_path, {"event": "started", **base_state})
    write_state(state_path, {"status": "starting", **base_state})

    try:
        sample_number = 0
        while True:
            if STOP_REASON:
                raise GateFailure(f"monitor terminated by {STOP_REASON}")
            sample_number += 1
            sample = collect_sample(arguments, started_at, boot_id)
            samples.append(sample)
            append_json(evidence_path, {"event": "sample", "number": sample_number, **sample})
            elapsed = time.monotonic() - started_monotonic
            running_state = {
                "status": "running",
                **base_state,
                "elapsed_seconds": round(elapsed, 3),
                "sample_count": sample_number,
                "last_sample": sample,
                "max_rss_mib": max(item["rss_mib"] for item in samples),
                "max_health_ms": max(item["health_ms"] for item in samples),
                "max_diagnostics_ms": max(item["diagnostics_ms"] for item in samples),
                "max_sse_first_data_ms": max(item["sse_first_data_ms"] for item in samples),
            }
            write_state(state_path, running_state)
            if elapsed >= arguments.duration_seconds:
                break
            next_due = started_monotonic + sample_number * arguments.interval_seconds
            time.sleep(
                max(
                    0.1,
                    min(next_due - time.monotonic(), arguments.duration_seconds - elapsed),
                )
            )

        initial = samples[0]
        final = samples[-1]
        if arguments.require_fresh_backup and final["backup"]["mtime"] <= started_at.isoformat():
            raise GateFailure("no fresh internal backup completed during the soak")
        if arguments.require_provider_progress:
            for provider, initial_success in initial["providers"].items():
                if final["providers"].get(provider, "") <= initial_success:
                    raise GateFailure(f"provider did not advance during soak: {provider}")
        completed = {
            "status": "passed",
            **base_state,
            "completed_at": utc_now().isoformat(),
            "elapsed_seconds": round(time.monotonic() - started_monotonic, 3),
            "sample_count": len(samples),
            "initial_sample": initial,
            "final_sample": final,
            "max_rss_mib": max(item["rss_mib"] for item in samples),
            "max_health_ms": max(item["health_ms"] for item in samples),
            "max_diagnostics_ms": max(item["diagnostics_ms"] for item in samples),
            "max_sse_first_data_ms": max(item["sse_first_data_ms"] for item in samples),
        }
        append_json(evidence_path, {"event": "passed", **completed})
        write_state(state_path, completed)
        return 0
    except Exception as error:  # noqa: BLE001 -- preserve a redacted gate diagnostic
        failed = {
            "status": "failed",
            **base_state,
            "failed_at": utc_now().isoformat(),
            "elapsed_seconds": round(time.monotonic() - started_monotonic, 3),
            "sample_count": len(samples),
            "error": f"{type(error).__name__}: {error}",
        }
        append_json(evidence_path, {"event": "failed", **failed})
        write_state(state_path, failed)
        print(failed["error"], file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
