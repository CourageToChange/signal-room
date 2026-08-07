from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from signal_room.config import AppSettings, load_topology

ROOT = Path(__file__).resolve().parents[2]


def test_example_topology_is_a_valid_acyclic_graph() -> None:
    topology = load_topology(ROOT / "config" / "config.example.yaml")
    assert len(topology.assets) == 7
    assert topology.assets[1].parent_id == "atlas-node"


def test_live_example_exercises_each_provider_contract() -> None:
    topology = load_topology(ROOT / "config" / "config.live.example.yaml")
    assert {check.provider for asset in topology.assets for check in asset.checks} == {
        "proxmox",
        "https",
        "tls",
        "backup",
    }


def test_production_refuses_development_authentication() -> None:
    with pytest.raises(ValidationError, match="production web requires Cloudflare Access"):
        AppSettings(
            environment="production",
            runtime_role="web",
            mode="live",
            auth_mode="development",
            build_sha="abcdef123456",  # pragma: allowlist secret
        )


def test_live_mode_refuses_missing_credentials() -> None:
    with pytest.raises(ValidationError, match="Proxmox"):
        AppSettings(environment="test", mode="live", auth_mode="development")


def test_live_web_role_does_not_require_collector_secret() -> None:
    settings = AppSettings(
        environment="test",
        runtime_role="web",
        mode="live",
        auth_mode="development",
    )
    assert settings.pve_token_secret == ""


def test_production_collector_does_not_require_web_authentication(tmp_path: Path) -> None:
    ca_bundle = tmp_path / "pve-root-ca.pem"
    ca_bundle.write_text("test", encoding="ascii")
    settings = AppSettings(
        environment="production",
        runtime_role="collector",
        mode="live",
        auth_mode="development",
        public_origin="https://signal.noorfamily.uk",
        build_sha="abcdef123456",  # pragma: allowlist secret
        pve_base_url="https://pve.internal:8006",
        pve_token_id="signal-room@pve!<token-id>",
        pve_token_secret="test-only-secret",  # pragma: allowlist secret
        pve_ca_bundle=ca_bundle,
    )
    assert settings.runtime_role == "collector"
