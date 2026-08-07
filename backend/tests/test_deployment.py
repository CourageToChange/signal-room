import hashlib
import http.client
import json
import os
import runpy
import sqlite3
import stat
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("unit", "role", "supplementary_groups"),
    [
        ("signal-room-backup.service", "signal-room-core", "signal-room"),
        (
            "signal-room-collector.service",
            "signal-room-collector",
            "signal-room signal-room-ingest",
        ),
        (
            "signal-room-core.service",
            "signal-room-core",
            "signal-room signal-room-query signal-room-ingest signal-room-notify",
        ),
        ("signal-room-migrate.service", "signal-room-core", "signal-room"),
        (
            "signal-room-notifier.service",
            "signal-room-notifier",
            "signal-room signal-room-notify",
        ),
        ("signal-room-web.service", "signal-room-web", "signal-room signal-room-query"),
    ],
)
def test_service_separates_private_credentials_and_transport_groups(
    unit: str, role: str, supplementary_groups: str
) -> None:
    text = (ROOT / "deploy" / unit).read_text(encoding="utf-8")

    assert f"User={role}\n" in text
    assert f"Group={role}\n" in text
    assert f"SupplementaryGroups={supplementary_groups}\n" in text
    role_environment = f"EnvironmentFile=/etc/signal-room/{role.removeprefix('signal-room-')}.env"
    if unit in {"signal-room-backup.service", "signal-room-migrate.service"}:
        role_environment = (
            "EnvironmentFile=/etc/signal-room/maintenance.env"
            if unit == "signal-room-backup.service"
            else "EnvironmentFile=/etc/signal-room/core.env"
        )
    release_environment = "EnvironmentFile=/opt/signal-room/current/RELEASE_ENV"
    assert text.index(role_environment) < text.index(release_environment)

    if unit == "signal-room-core.service":
        assert "RuntimeDirectoryMode=0711\n" in text
        assert "StateDirectoryMode=0700\n" in text
        assert "UMask=0077\n" in text


def test_cloudflared_service_is_token_file_only_and_hardened() -> None:
    text = (ROOT / "deploy" / "cloudflared.service").read_text(encoding="utf-8")

    assert "User=cloudflared\nGroup=cloudflared\n" in text
    assert "--token-file /etc/cloudflared/tunnel.token" in text
    assert "--metrics 127.0.0.1:20241" in text
    assert "--edge-ip-version 4" in text
    assert "--no-autoupdate" in text
    assert "EnvironmentFile=" not in text
    assert "SupplementaryGroups=" not in text
    assert "CapabilityBoundingSet=\n" in text
    assert "AmbientCapabilities=\n" in text
    assert "ProtectSystem=strict\n" in text
    assert "TimeoutStartSec=0\n" in text
    assert "TimeoutStopSec=20s\n" in text
    assert "MemoryHigh=96M\nMemoryMax=256M\n" in text
    assert "RestrictAddressFamilies=AF_UNIX AF_INET AF_NETLINK\n" in text
    assert "AF_INET6" not in text
    assert "WantedBy=multi-user.target\n" in text
    assert "PartOf=signal-room.target" not in text


def test_nftables_example_is_single_host_only_and_keeps_address_placeholders() -> None:
    text = (ROOT / "deploy" / "nftables.example.conf").read_text(encoding="utf-8")

    assert text.startswith(
        "# WARNING: THIS FILE MUST ONLY EVER BE APPLIED INSIDE the target container.\n"
        "# `flush ruleset` removes every existing table in the network namespace where it runs.\n"
        "# Never apply this file on the Proxmox host or any other guest.\n"
    )
    assert "\nflush ruleset\n" in text
    for placeholder in ("DNS_IP_1", "DNS_IP_2", "PVE_API_IP"):
        assert placeholder in text


def test_release_metadata_is_lf_only_and_installer_accepts_crlf(tmp_path: Path) -> None:
    namespace = runpy.run_path(str(ROOT / "scripts" / "build-release.py"))
    destination = tmp_path / "metadata.txt"

    namespace["write_text_lf"](destination, "private\nsecond\n")

    assert destination.read_bytes() == b"private\nsecond\n"
    installer = (ROOT / "deploy" / "install-release.sh").read_text(encoding="utf-8")
    assert "tr -d '\\r\\n'" in installer
    assert "line=\"${line%$'\\r'}\"" in installer


def test_release_workflow_stamps_and_reproduces_exact_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = runpy.run_path(str(ROOT / "scripts" / "build-release.py"))
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)

    namespace["configure_reproducible_environment"](1_700_000_000)

    assert os.environ["SOURCE_DATE_EPOCH"] == "1700000000"
    with pytest.raises(RuntimeError, match="cannot be negative"):
        namespace["configure_reproducible_environment"](-1)

    workflow = (ROOT / ".github" / "workflows" / "verify.yml").read_text(encoding="utf-8")
    assert '--build-sha "$GITHUB_SHA" --source-date-epoch "$epoch"' in workflow
    assert 'cmp --silent "$archive"' in workflow
    assert "mapfile -d '' bundles" in workflow


def test_load_gate_ignores_empty_unrelated_proc_status_fields() -> None:
    namespace = runpy.run_path(str(ROOT / "deploy" / "load-gate.py"))

    assert namespace["parse_memory_status"](
        "Name:\tpython\nx86_Thread_features:\nVmRSS:\t12345 kB\nVmSwap:\t0 kB\n"
    ) == (12345, 0)

    with pytest.raises(RuntimeError, match="malformed VmRSS"):
        namespace["parse_memory_status"]("VmRSS:\nVmSwap:\t0 kB\n")


def test_soak_monitor_is_release_generic_and_fails_closed_on_bad_state() -> None:
    namespace = runpy.run_path(str(ROOT / "deploy" / "soak-monitor.py"))
    gate_failure = namespace["GateFailure"]

    assert namespace["expected_release_path"]("1.0.0", "abc") == (
        "/opt/signal-room/releases/1.0.0-abc"
    )
    assert namespace["parse_kib"]({"VmRSS": "123 KiB"}, "VmRSS") == 123
    with pytest.raises(gate_failure, match="malformed"):
        namespace["parse_kib"]({"VmRSS": ""}, "VmRSS")
    namespace["validate_notifications"](
        {
            "enabled": False,
            "pending": 0,
            "delivered": 0,
            "dead_letter": 0,
            "suppressed": 2,
        }
    )
    with pytest.raises(gate_failure, match="drifted"):
        namespace["validate_notifications"]({"enabled": False, "pending": 1, "dead_letter": 0})
    assert namespace["parse_tunnel_connections"]("cloudflared_tunnel_ha_connections 4\n") == 4
    with pytest.raises(gate_failure, match="no active"):
        namespace["parse_tunnel_connections"](
            'cloudflared_tunnel_ha_connections{protocol="quic"} 0\n'
        )


def test_soak_monitor_converts_tunnel_http_exceptions_to_gate_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = runpy.run_path(str(ROOT / "deploy" / "soak-monitor.py"))
    gate_failure = namespace["GateFailure"]

    class BrokenConnection:
        def request(self, _method: str, _path: str) -> None:
            raise http.client.BadStatusLine("invalid status")

        def close(self) -> None:
            pass

    def broken_connection(*_args: object, **_kwargs: object) -> BrokenConnection:
        return BrokenConnection()

    monkeypatch.setattr(http.client, "HTTPConnection", broken_connection)

    with pytest.raises(
        gate_failure,
        match=r"cloudflared /ready failed: BadStatusLine: invalid status",
    ):
        namespace["request_tunnel_endpoint"]("/ready")


def test_soak_monitor_rejects_permissive_or_unexpected_nftables_rules() -> None:
    namespace = runpy.run_path(str(ROOT / "deploy" / "soak-monitor.py"))
    gate_failure = namespace["GateFailure"]
    comments = namespace["EXPECTED_NFT_ACCEPT_COMMENTS"]

    def ruleset(*, input_policy: str = "drop", extra_comment: str | None = None) -> dict:
        items: list[dict] = [
            {"table": {"family": "inet", "name": "signal_room_filter"}},
            {
                "set": {
                    "family": "inet",
                    "table": "signal_room_filter",
                    "name": "denied_v4",
                }
            },
            {
                "set": {
                    "family": "inet",
                    "table": "signal_room_filter",
                    "name": "cloudflare_tunnel_v4",
                }
            },
            {
                "chain": {
                    "family": "inet",
                    "table": "signal_room_filter",
                    "name": "input",
                    "policy": input_policy,
                }
            },
            {
                "chain": {
                    "family": "inet",
                    "table": "signal_room_filter",
                    "name": "output",
                    "policy": "drop",
                }
            },
        ]
        for comment in sorted(comments):
            items.append(
                {
                    "rule": {
                        "family": "inet",
                        "table": "signal_room_filter",
                        "comment": comment,
                        "expr": [{"accept": None}],
                    }
                }
            )
        if extra_comment:
            items.append(
                {
                    "rule": {
                        "family": "inet",
                        "table": "signal_room_filter",
                        "comment": extra_comment,
                        "expr": [{"accept": None}],
                    }
                }
            )
        return {"nftables": items}

    namespace["validate_nftables_ruleset"](ruleset())
    with pytest.raises(gate_failure, match="policy drifted"):
        namespace["validate_nftables_ruleset"](ruleset(input_policy="accept"))
    with pytest.raises(gate_failure, match="accept rules drifted"):
        namespace["validate_nftables_ruleset"](ruleset(extra_comment="unexpected allow"))


def test_virtual_environment_entrypoints_are_relocated_before_activation(
    tmp_path: Path,
) -> None:
    stage = (tmp_path / "release.partial.123").resolve()
    target = (tmp_path / "release-final").resolve()
    bin_directory = stage / ".venv" / "bin"
    bin_directory.mkdir(parents=True)
    signal_room = bin_directory / "signal-room"
    pip = bin_directory / "pip"
    untouched = bin_directory / "activate"
    signal_room.write_bytes(
        f"#!{stage}/.venv/bin/python\nfrom signal_room.cli import main\n".encode()
    )
    pip.write_bytes(f"#!{stage}/.venv/bin/python\nprint('pip')\n".encode())
    untouched.write_bytes(b"export VIRTUAL_ENV=/old/path\n")
    namespace = runpy.run_path(str(ROOT / "deploy" / "relocate-venv.py"))

    rewritten = namespace["relocate_entrypoints"](stage, target)

    assert rewritten == [pip, signal_room]
    assert signal_room.read_bytes().startswith(f"#!{target}/.venv/bin/python\n".encode())
    assert pip.read_bytes().startswith(f"#!{target}/.venv/bin/python\n".encode())
    assert untouched.read_bytes() == b"export VIRTUAL_ENV=/old/path\n"

    installer = (ROOT / "deploy" / "install-release.sh").read_text(encoding="utf-8")
    relocation = 'python3 "$STAGE/deploy/relocate-venv.py" "$STAGE" "$TARGET"'
    assert installer.index(relocation) < installer.index('mv -- "$STAGE" "$TARGET"')
    assert 'ln -s -- "$CURRENT_LINK/deploy/$unit_name" "$UNIT_NEXT"' in installer
    assert 'rm -f -- "$CURRENT_LINK"' in installer
    assert 'ln -s -- /etc/systemd/system/signal-room.target "$ENABLE_NEXT"' in installer
    assert 'ln -s -- /etc/systemd/system/signal-room-backup.timer "$ENABLE_NEXT"' in installer
    assert 'test "$(systemctl is-enabled signal-room.target 2>/dev/null)" = enabled' in installer
    assert "systemctl start signal-room-backup.timer" in installer
    assert "/etc/systemd/system/timers.target.wants/signal-room-backup.timer" in installer
    assert "install -d -o signal-room-core -g signal-room-core -m 0700" in installer
    assert "signal-room-query signal-room-ingest signal-room-notify" in installer

    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "*.sh text eol=lf\n" in attributes
    assert "*.service text eol=lf\n" in attributes


def test_release_identity_is_bound_to_directory_sbom_and_exact_wheel(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(str(ROOT / "deploy" / "verify-release.py"))
    verification_error = namespace["VerificationError"]
    build_sha = "a" * 40
    bundle = tmp_path / f"signal-room-private-1.0.0-{build_sha[:12]}"
    bundle.mkdir()
    (bundle / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    (bundle / "BUILD_SHA").write_text(f"{build_sha}\n", encoding="utf-8")
    (bundle / "RELEASE_ENV").write_bytes(f"SIGNAL_ROOM_BUILD_SHA={build_sha}\n".encode("ascii"))
    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "metadata": {
            "component": {
                "type": "application",
                "name": "signal-room-private",
                "version": "1.0.0",
            },
            "properties": [{"name": "signal-room:build-sha", "value": build_sha}],
        },
    }
    (bundle / "sbom.cdx.json").write_text(json.dumps(sbom), encoding="utf-8")

    assert namespace["_validate_identity"](bundle, "private") == ("1.0.0", build_sha)

    (bundle / "RELEASE_ENV").write_bytes(f"SIGNAL_ROOM_BUILD_SHA={'b' * 40}\n".encode("ascii"))
    with pytest.raises(verification_error, match="RELEASE_ENV"):
        namespace["_validate_identity"](bundle, "private")
    (bundle / "RELEASE_ENV").write_bytes(f"SIGNAL_ROOM_BUILD_SHA={build_sha}\n".encode("ascii"))

    sbom["metadata"]["properties"].append(  # type: ignore[index,union-attr]
        {"name": "signal-room:build-sha", "value": build_sha}
    )
    (bundle / "sbom.cdx.json").write_text(json.dumps(sbom), encoding="utf-8")
    with pytest.raises(verification_error, match="duplicate properties"):
        namespace["_validate_identity"](bundle, "private")


def test_private_release_verification_requires_the_exact_universal_wheel(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(str(ROOT / "deploy" / "verify-release.py"))
    verification_error = namespace["VerificationError"]
    build_sha = "b" * 40
    bundle = tmp_path / f"signal-room-private-1.0.0-{build_sha[:12]}"
    files: dict[str, bytes] = {
        "VERSION": b"1.0.0\n",
        "BUILD_SHA": f"{build_sha}\n".encode(),
        "BUNDLE_KIND": b"private\n",
        "RELEASE_ENV": f"SIGNAL_ROOM_BUILD_SHA={build_sha}\n".encode(),
        "config-schema.json": b"{}\n",
        "requirements.lock": b"dependency==1 --hash=sha256:00\n",
        "web/index.html": b"<!doctype html><title>Signal Room</title>",
        "deploy/install-release.sh": b"#!/usr/bin/env bash\n",
        "deploy/cloudflared.service": b"[Service]\n",
        "deploy/nftables.example.conf": b"table inet signal_room_filter {}\n",
        "deploy/soak-monitor.py": b"pass\n",
        "deploy/sqlite-release-snapshot.py": b"pass\n",
        "deploy/verify-release.py": b"pass\n",
        "deploy/wait-for-core.py": b"pass\n",
        "migrations/001_initial.sql": b"SELECT 1;\n",
        "wheelhouse/dependency-1-py3-none-any.whl": b"wheelhouse",
        "wheels/signal_room-1.0.0-py3-none-any.whl": b"application",
        "sbom.cdx.json": json.dumps(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.6",
                "metadata": {
                    "component": {
                        "type": "application",
                        "name": "signal-room-private",
                        "version": "1.0.0",
                    },
                    "properties": [{"name": "signal-room:build-sha", "value": build_sha}],
                },
            }
        ).encode(),
    }

    def write_bundle() -> None:
        manifest: list[str] = []
        for relative, content in sorted(files.items()):
            target = bundle / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            manifest.append(f"{hashlib.sha256(content).hexdigest()}  {relative}")
        (bundle / "SHA256SUMS").write_text("\n".join(manifest) + "\n", encoding="utf-8")

    write_bundle()
    assert namespace["verify"](bundle) == "private"

    wrong_wheel = "wheels/signal_room-9.9.9-py3-none-any.whl"
    files[wrong_wheel] = files.pop("wheels/signal_room-1.0.0-py3-none-any.whl")
    (bundle / "wheels" / "signal_room-1.0.0-py3-none-any.whl").unlink()
    write_bundle()
    with pytest.raises(verification_error, match="wheel version"):
        namespace["verify"](bundle)


def test_verified_sqlite_release_snapshot_captures_wal_and_rejects_bad_state(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(str(ROOT / "deploy" / "sqlite-release-snapshot.py"))
    snapshot_error = namespace["SnapshotError"]
    source = tmp_path / "source.sqlite3"
    snapshot = tmp_path / "snapshot.sqlite3"
    connection = sqlite3.connect(source)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE child (parent_id INTEGER REFERENCES parent(id))")
        connection.execute("INSERT INTO parent VALUES (1)")
        connection.execute("INSERT INTO child VALUES (1)")
        connection.commit()

        namespace["backup"](source, snapshot)
    finally:
        connection.close()

    with closing(sqlite3.connect(snapshot)) as copied, copied:
        assert copied.execute("SELECT parent_id FROM child").fetchall() == [(1,)]
        assert copied.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        assert copied.execute("PRAGMA foreign_key_check").fetchall() == []
    with pytest.raises(snapshot_error, match="already exists"):
        namespace["backup"](source, snapshot)

    invalid = tmp_path / "invalid.sqlite3"
    with closing(sqlite3.connect(invalid)) as broken, broken:
        broken.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        broken.execute("CREATE TABLE child (parent_id INTEGER REFERENCES parent(id))")
        broken.execute("INSERT INTO child VALUES (99)")
    with pytest.raises(snapshot_error, match="foreign_key_check"):
        namespace["backup"](invalid, tmp_path / "invalid-snapshot.sqlite3")
    assert not (tmp_path / "invalid-snapshot.sqlite3").exists()


@pytest.mark.skipif(os.name != "posix", reason="release restore is intentionally POSIX-only")
def test_verified_sqlite_release_snapshot_restores_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import grp
    import pwd

    namespace = runpy.run_path(str(ROOT / "deploy" / "sqlite-release-snapshot.py"))
    source = tmp_path / "source.sqlite3"
    snapshot = tmp_path / "snapshot.sqlite3"
    destination = tmp_path / "destination.sqlite3"
    with closing(sqlite3.connect(source)) as database, database:
        database.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        database.execute("INSERT INTO marker VALUES ('before')")
    namespace["backup"](source, snapshot)
    with closing(sqlite3.connect(destination)) as database, database:
        database.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        database.execute("INSERT INTO marker VALUES ('after')")
    Path(f"{destination}-wal").write_bytes(b"old wal")
    Path(f"{destination}-shm").write_bytes(b"old shm")

    identity = type("Identity", (), {"pw_uid": os.getuid(), "gr_gid": os.getgid()})()
    monkeypatch.setattr(pwd, "getpwnam", lambda _name: identity)
    monkeypatch.setattr(grp, "getgrnam", lambda _name: identity)

    namespace["restore"](snapshot, destination)

    with closing(sqlite3.connect(destination)) as restored, restored:
        assert restored.execute("SELECT value FROM marker").fetchall() == [("before",)]
    assert not Path(f"{destination}-wal").exists()
    assert not Path(f"{destination}-shm").exists()


def test_release_installer_rolls_code_and_database_back_as_one_unit() -> None:
    installer = (ROOT / "deploy" / "install-release.sh").read_text(encoding="utf-8")

    assert "flock --exclusive --nonblock 9" in installer
    assert "readonly INSTALL_LOCK=/run/signal-room-install.lock" in installer
    assert 'find "$SOURCE" \\( ! -user root -o -perm /022 \\)' in installer
    assert "trap on_exit EXIT" in installer
    stop = installer.index("stop_release_services ||")
    rollback_snapshot = installer.index('ROLLBACK_DB="$SMOKE_ROOT/release-rollback.sqlite3"')
    switch = installer.index('mv -Tf -- "$NEXT_LINK" "$CURRENT_LINK"')
    may_migrate = installer.index("NEW_SERVICES_MAY_HAVE_RUN=1")
    start = installer.index("systemctl start signal-room.target || exit 70")
    assert stop < rollback_snapshot < switch < may_migrate < start

    rollback = installer.index("rollback_activation()")
    restore = installer.index('restore "$ROLLBACK_DB" "$STATE_DB"', rollback)
    old_link = installer.index('ln -s -- "$PREVIOUS" "$ROLLBACK_LINK"', rollback)
    old_start = installer.index("systemctl start signal-room.target || return 1", rollback)
    assert restore < old_link < old_start
    assert installer.count("http://127.0.0.1:8080/api/health/ready") == 2
    assert (
        "database rollback was not attempted because release processes may still be running"
        in installer
    )


def test_private_release_build_identity_is_immutable_and_artifact_owned(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(str(ROOT / "scripts" / "build-release.py"))
    build_sha = "c" * 40
    private = tmp_path / "private"
    public = tmp_path / "public"
    private.mkdir()
    public.mkdir()

    namespace["metadata"](private, "1.0.0", build_sha, "private")
    namespace["metadata"](public, "1.0.0", build_sha, "public")

    assert private.joinpath("RELEASE_ENV").read_bytes() == (
        f"SIGNAL_ROOM_BUILD_SHA={build_sha}\n".encode()
    )
    assert not public.joinpath("RELEASE_ENV").exists()
    assert all(
        "SIGNAL_ROOM_BUILD_SHA" not in path.read_text(encoding="utf-8")
        for path in (ROOT / "deploy" / "env").glob("*.example")
    )


def test_core_startup_waits_for_a_real_unix_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    namespace = runpy.run_path(str(ROOT / "deploy" / "wait-for-core.py"))
    readiness_error = namespace["CoreReadinessError"]
    path = tmp_path / "query.sock"
    socket_metadata = type("Metadata", (), {"st_mode": stat.S_IFSOCK})()
    monkeypatch.setattr(Path, "lstat", lambda _path: socket_metadata)

    namespace["wait_for_sockets"]((path,), 1.0, 0.01)

    symlink_metadata = type("Metadata", (), {"st_mode": stat.S_IFLNK})()
    monkeypatch.setattr(Path, "lstat", lambda _path: symlink_metadata)
    with pytest.raises(readiness_error, match="symlink"):
        namespace["wait_for_sockets"]((path,), 1.0, 0.01)

    regular_metadata = type("Metadata", (), {"st_mode": stat.S_IFREG})()
    monkeypatch.setattr(Path, "lstat", lambda _path: regular_metadata)
    with pytest.raises(readiness_error, match="not a Unix socket"):
        namespace["wait_for_sockets"]((path,), 1.0, 0.01)

    def missing_socket(_path: Path) -> os.stat_result:
        raise FileNotFoundError

    monkeypatch.setattr(Path, "lstat", missing_socket)
    clock = iter((0.0, 0.5, 1.1))
    monkeypatch.setattr(namespace["time"], "monotonic", lambda: next(clock))
    monkeypatch.setattr(namespace["time"], "sleep", lambda _seconds: None)
    with pytest.raises(readiness_error, match="deadline"):
        namespace["wait_for_sockets"]((path,), 1.0, 0.01)
    with pytest.raises(readiness_error, match="at least one"):
        namespace["wait_for_sockets"]((), 1.0, 0.01)

    core_unit = (ROOT / "deploy" / "signal-room-core.service").read_text(encoding="utf-8")
    start = core_unit.index("ExecStart=")
    ready = core_unit.index("ExecStartPost=")
    assert start < ready
    assert "wait-for-core.py --timeout 10 /run/signal-room/query.sock" in core_unit
    assert all(
        socket in core_unit
        for socket in ("query.sock", "ingest.sock", "notifier.sock", "maintenance.sock")
    )
    workflow = (ROOT / ".github" / "workflows" / "verify.yml").read_text(encoding="utf-8")
    assert "ln -s /bin/true /opt/signal-room/current/.venv/bin/python" in workflow
