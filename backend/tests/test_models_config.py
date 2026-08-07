from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from signal_room.config import (
    AppSettings,
    export_config_schema,
    load_runbooks,
    load_topology,
    production_environment,
)
from signal_room.models import (
    AssetDefinition,
    AssetKind,
    CheckDefinition,
    CheckKind,
    MaintenanceCreateRequest,
    ProbePolicy,
    ThresholdConfig,
    TopologyConfig,
)

ROOT = Path(__file__).resolve().parents[2]


def check(**updates: object) -> CheckDefinition:
    values: dict[str, object] = {"id": "check-one", "type": CheckKind.FIXTURE}
    values.update(updates)
    return CheckDefinition.model_validate(values)


def node(identifier: str = "node-one", **updates: object) -> AssetDefinition:
    values: dict[str, object] = {
        "id": identifier,
        "label": identifier,
        "kind": AssetKind.NODE,
        "checks": [check()],
    }
    values.update(updates)
    return AssetDefinition.model_validate(values)


def topology(assets: list[AssetDefinition], **updates: object) -> TopologyConfig:
    values: dict[str, object] = {
        "version": 2,
        "revision": "test-v2",
        "assets": assets,
    }
    values.update(updates)
    return TopologyConfig.model_validate(values)


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"expected_statuses": [200, 200]}, "unique"),
        ({"expected_statuses": [99]}, "valid HTTP"),
        ({"allowed_ports": [443, 443]}, "unique"),
        ({"allowed_ports": [65536]}, "valid TCP"),
        ({"type": "proxmox_node"}, "require a node"),
        ({"type": "proxmox_guest", "node": "pve"}, "require guest_id"),
        ({"type": "proxmox_storage", "node": "pve"}, "require storage"),
        ({"type": "proxmox_backup", "node": "pve"}, "require backup_job_id"),
        ({"type": "https"}, "require a URL"),
        ({"type": "https", "url": "http://public.example/"}, "must use HTTPS"),
        (
            {
                "type": "https",
                "url": "https://user:pass@public.example/",  # pragma: allowlist secret
            },
            "userinfo",
        ),
        (
            {"type": "tls", "url": "https://public.example:8443/"},
            "allowed_ports",
        ),
        ({"type": "fixture", "url": "https://public.example/"}, "only https"),
    ],
)
def test_check_contract_rejects_ambiguous_or_unsafe_inputs(
    values: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        check(**values)


def test_check_provider_mapping_is_total() -> None:
    values = [
        check(type="fixture"),
        check(type="proxmox_node", node="pve"),
        check(type="proxmox_guest", node="pve", guest_id=104),
        check(type="proxmox_storage", node="pve", storage="local"),
        check(type="proxmox_backup", node="pve", backup_job_id="nightly"),
        check(type="https", url="https://public.example/"),
        check(type="tls", url="https://public.example/"),
    ]
    assert [str(item.provider) for item in values] == [
        "fixture",
        "proxmox",
        "proxmox",
        "proxmox",
        "backup",
        "https",
        "tls",
    ]


def test_asset_and_topology_graph_validation() -> None:
    with pytest.raises(ValidationError, match="duplicate dependencies"):
        node(depends_on=["parent", "parent"])
    with pytest.raises(ValidationError, match="cannot depend on itself"):
        node(depends_on=["node-one"])
    with pytest.raises(ValidationError, match="duplicate check"):
        node(checks=[check(), check()])
    assert node().parent_id is None

    parent = node("parent")
    child = node("child", depends_on=["parent"], sort_order=2)
    assert child.parent_id == "parent"
    configured = topology([child, parent])
    assert [item.id for item in configured.topological_assets()] == ["parent", "child"]
    with pytest.raises(ValidationError, match="asset ids"):
        topology([parent, parent])
    with pytest.raises(ValidationError, match="unknown dependency"):
        topology([node("orphan", depends_on=["missing"])])
    with pytest.raises(ValidationError, match="cycle"):
        topology(
            [
                node("cycle-a", depends_on=["cycle-b"]),
                node("cycle-b", depends_on=["cycle-a"]),
            ]
        )
    probe_asset = node(
        "probe",
        checks=[check(type="https", url="https://public.example/health")],
    )
    with pytest.raises(ValidationError, match="allowlisted"):
        topology([probe_asset])
    assert topology([probe_asset], probes=ProbePolicy(allowed_hosts=["PUBLIC.EXAMPLE."])).assets


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        (
            {"memory_warning_ratio": 0.9, "memory_critical_ratio": 0.8},
            "memory",
        ),
        ({"disk_warning_ratio": 0.9, "disk_critical_ratio": 0.8}, "disk"),
        ({"backup_warning_hours": 10, "backup_critical_hours": 9}, "backup"),
        (
            {"certificate_warning_days": 10, "certificate_critical_days": 11},
            "certificate",
        ),
    ],
)
def test_threshold_ordering(updates: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        ThresholdConfig(**updates)


def test_maintenance_request_enforces_scope_duration_and_order() -> None:
    start = datetime(2026, 7, 15, 12, tzinfo=UTC)
    valid = MaintenanceCreateRequest(
        asset_ids=["node-one"],
        starts_at=start,
        ends_at=start + timedelta(hours=1),
        reason="Upgrade",
    )
    assert valid.reason == "Upgrade"
    for values, message in [
        ({"ends_at": start}, "after"),
        ({"ends_at": start + timedelta(hours=25)}, "24 hours"),
        ({"asset_ids": ["node-one", "node-one"]}, "unique"),
    ]:
        payload = valid.model_dump()
        payload.update(values)
        with pytest.raises(ValidationError, match=message):
            MaintenanceCreateRequest.model_validate(payload)


def test_settings_role_boundaries_and_derived_views(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = AppSettings(
        environment="test",
        runtime_role="web",
        auth_mode="access",
        access_team_domain="https://team.cloudflareaccess.com",
        access_audience="audience",
        allowed_emails=" Owner@Example.Test,second@example.test ",
        trusted_hosts="Signal.Example.Test, localhost",
    )
    assert settings.email_allowlist == {"owner@example.test", "second@example.test"}
    assert settings.host_allowlist == {"signal.example.test", "localhost"}
    assert not settings.webhook_enabled
    settings.assert_command_role("serve")
    with pytest.raises(ValueError, match="not permitted"):
        settings.assert_command_role("collect")
    AppSettings(environment="test", runtime_role="all").assert_command_role("anything")

    ca = tmp_path / "ca.pem"
    ca.write_text("CA", encoding="ascii")
    collector = AppSettings(
        environment="test",
        runtime_role="collector",
        mode="live",
        pve_base_url="https://pve.internal:8006",
        pve_token_id="audit@pve!collector",
        pve_token_secret="secret",  # pragma: allowlist secret
        pve_ca_bundle=ca,
    )
    collector.assert_command_role("collect")

    invalid_settings = [
        ({"public_origin": "ftp://invalid"}, "HTTP"),
        ({"public_origin": "https://signal.example/path"}, "cannot include"),
        (
            {"runtime_role": "web", "auth_mode": "access"},
            "team domain",
        ),
        (
            {
                "runtime_role": "collector",
                "access_team_domain": "https://team.example",
            },
            "non-web",
        ),
        ({"runtime_role": "collector", "static_dir": "/private"}, "non-web"),
        ({"runtime_role": "collector", "mode": "live"}, "Proxmox"),
        (
            {"runtime_role": "web", "pve_base_url": "https://pve.internal"},
            "non-collector",
        ),
        ({"webhook_url": "https://hooks.example"}, "together"),
        (
            {
                "webhook_url": "http://hooks.example",
                "webhook_secret": "secret",  # pragma: allowlist secret
            },
            "HTTPS",
        ),
        ({"deadman_url": "http://deadman.example"}, "HTTPS"),
        (
            {
                "runtime_role": "web",
                "webhook_url": "https://hooks.example",
                "webhook_secret": "secret",  # pragma: allowlist secret
            },
            "non-notifier",
        ),
    ]
    for values, message in invalid_settings:
        with pytest.raises(ValidationError, match=message):
            AppSettings(environment="test", **values)

    production = {
        "environment": "production",
        "public_origin": "https://signal.noorfamily.uk",
        "build_sha": "abcdef123456",  # pragma: allowlist secret
    }
    with pytest.raises(ValidationError, match="combined"):
        AppSettings(**production)
    with pytest.raises(ValidationError, match="live mode"):
        AppSettings(**production, runtime_role="core", mode="fixture")
    with pytest.raises(ValidationError, match="placeholder"):
        AppSettings(
            environment="production",
            runtime_role="maintenance",
            public_origin="https://signal.example.invalid",
            build_sha="abcdef123456",  # pragma: allowlist secret
        )
    with pytest.raises(ValidationError, match="Cloudflare"):
        AppSettings(**production, runtime_role="web", auth_mode="development")

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("", encoding="ascii")
    with pytest.raises(ValidationError, match="forbids loading"):
        AppSettings(**production, runtime_role="maintenance")


def test_config_file_helpers_schema_and_safe_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        load_topology(empty)
    root = Path(__file__).resolve().parents[2]
    assert load_runbooks(root / "config" / "runbooks.yaml").runbooks
    assert load_topology(root / "config" / "config.example.yaml").version == 2
    schema = tmp_path / "schema" / "topology.schema.json"
    export_config_schema(schema)
    assert '"version"' in schema.read_text(encoding="utf-8")
    monkeypatch.setenv("SIGNAL_ROOM_MODE", "fixture")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-leak")
    assert production_environment() == {"SIGNAL_ROOM_MODE": "fixture"}


def test_split_environment_examples_only_use_known_strict_settings() -> None:
    examples = sorted((ROOT / "deploy/env").glob("*.env.example"))
    assert {path.name for path in examples} == {
        "collector.env.example",
        "core.env.example",
        "maintenance.env.example",
        "notifier.env.example",
        "web.env.example",
    }
    known = set(AppSettings.model_fields)
    for path in examples:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#"):
                continue
            key, separator, _ = line.partition("=")
            assert separator == "=", f"malformed environment line in {path.name}"
            assert key.startswith("SIGNAL_ROOM_")
            assert key.removeprefix("SIGNAL_ROOM_").lower() in known
