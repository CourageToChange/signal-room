from __future__ import annotations

import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import RunbookConfig, TopologyConfig

RuntimeRole = Literal["all", "core", "web", "collector", "notifier", "maintenance"]


class AppSettings(BaseSettings):
    """Strict, role-aware process settings.

    A single schema keeps deployment validation reproducible, while the validator
    ensures that a process fails startup when it receives another role's secret.
    """

    model_config = SettingsConfigDict(
        env_prefix="SIGNAL_ROOM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        case_sensitive=False,
    )

    environment: Literal["development", "test", "production"] = "development"
    runtime_role: RuntimeRole = "all"
    mode: Literal["fixture", "live"] = "fixture"
    auth_mode: Literal["development", "access"] = "development"
    config_path: Path = Path("config/config.example.yaml")
    runbooks_path: Path = Path("config/runbooks.yaml")
    static_dir: Path | None = None
    db_path: Path = Path("data/signal-room.sqlite3")
    query_socket: Path = Path("run/query.sock")
    ingest_socket: Path = Path("run/ingest.sock")
    notifier_socket: Path = Path("run/notifier.sock")
    maintenance_socket: Path = Path("run/maintenance.sock")
    public_origin: str = "http://127.0.0.1:8080"
    trusted_hosts: str = "127.0.0.1,localhost"
    log_level: str = "INFO"
    build_sha: str = "development"

    access_team_domain: str = ""
    access_audience: str = ""
    allowed_emails: str = ""
    access_clock_leeway_seconds: int = Field(default=30, ge=0, le=120)

    pve_base_url: str = ""
    pve_token_id: str = ""
    pve_token_secret: str = ""
    pve_ca_bundle: Path | None = None

    webhook_url: str = ""
    webhook_secret: str = ""
    deadman_url: str = ""

    retention_sample_days: int = Field(default=7, ge=7, le=7)
    retention_rollup_days: int = Field(default=180, ge=180, le=180)
    retention_incident_days: int = Field(default=365, ge=365, le=365)
    backup_retention_days: int = Field(default=14, ge=14, le=90)
    mutation_limit_per_minute: int = Field(default=60, ge=1, le=600)
    request_body_limit_bytes: int = Field(default=32_768, ge=1024, le=1_048_576)
    sse_connection_limit: int = Field(default=8, ge=1, le=32)

    @property
    def email_allowlist(self) -> set[str]:
        return {email.strip().lower() for email in self.allowed_emails.split(",") if email.strip()}

    @property
    def host_allowlist(self) -> set[str]:
        return {host.strip().lower() for host in self.trusted_hosts.split(",") if host.strip()}

    @property
    def webhook_enabled(self) -> bool:
        return bool(self.webhook_url and self.webhook_secret)

    def assert_command_role(self, command: str) -> None:
        allowed: dict[str, set[str]] = {
            "core": {"core"},
            "serve": {"web"},
            "collect": {"collector"},
            "notify": {"notifier"},
            "migrate": {"maintenance"},
            "backup": {"maintenance"},
            "validate-config": {"maintenance", "core", "collector", "web", "notifier"},
            "export-demo": {"maintenance"},
        }
        if self.runtime_role == "all" and self.environment != "production":
            return
        if self.runtime_role not in allowed.get(command, set()):
            raise ValueError(
                f"command {command!r} is not permitted for runtime role {self.runtime_role!r}"
            )

    @model_validator(mode="after")
    def enforce_environment_boundaries(self) -> AppSettings:
        if not self.public_origin.startswith(("http://", "https://")):
            raise ValueError("public origin must be an HTTP(S) origin")
        parsed_origin = urlsplit(self.public_origin)
        if parsed_origin.path not in {"", "/"} or parsed_origin.query or parsed_origin.fragment:
            raise ValueError("public origin cannot include a path, query, or fragment")

        if self.environment == "production":
            if self.runtime_role == "all":
                raise ValueError("production forbids combined runtime roles")
            if Path(".env").exists():
                raise ValueError("production forbids loading settings from .env")
            if self.mode == "fixture" and self.runtime_role in {"core", "collector"}:
                raise ValueError("production core and collector require live mode")
            placeholders = ("replace", "change-me", "example.invalid", "development")
            inspected = [self.public_origin, self.build_sha]
            if any(marker in value.lower() for value in inspected for marker in placeholders):
                raise ValueError("production settings contain a placeholder value")

        owns_access = self.runtime_role in {"all", "web"}
        owns_pve = self.runtime_role in {"all", "collector"}
        owns_webhook = self.runtime_role in {"all", "notifier"}

        if self.auth_mode == "access" and owns_access:
            if not self.access_team_domain.startswith("https://"):
                raise ValueError("Access team domain must be an HTTPS origin")
            if not self.access_audience or not self.email_allowlist:
                raise ValueError("Access audience and exact email allowlist are required")
        if self.environment == "production" and owns_access and self.auth_mode != "access":
            raise ValueError("production web requires Cloudflare Access authentication")
        if not owns_access and any(
            (self.access_team_domain, self.access_audience, self.allowed_emails)
        ):
            raise ValueError("Access settings were supplied to a non-web process")
        if not owns_access and self.static_dir is not None:
            raise ValueError("private frontend path was supplied to a non-web process")

        if self.mode == "live" and owns_pve:
            if not self.pve_base_url.startswith("https://"):
                raise ValueError("live collection requires an HTTPS Proxmox base URL")
            if not self.pve_token_id or not self.pve_token_secret:
                raise ValueError("live collection requires a dedicated Proxmox API token")
            if self.pve_ca_bundle is None or not self.pve_ca_bundle.is_file():
                raise ValueError("live collection requires a readable Proxmox CA bundle")
        if not owns_pve and any((self.pve_base_url, self.pve_token_id, self.pve_token_secret)):
            raise ValueError("Proxmox credentials were supplied to a non-collector process")

        if bool(self.webhook_url) != bool(self.webhook_secret):
            raise ValueError("webhook URL and secret must be configured together")
        if self.webhook_url and not self.webhook_url.startswith("https://"):
            raise ValueError("webhook URL must use HTTPS")
        if self.deadman_url and not self.deadman_url.startswith("https://"):
            raise ValueError("dead-man URL must use HTTPS")
        if not owns_webhook and any((self.webhook_url, self.webhook_secret, self.deadman_url)):
            raise ValueError("notification secrets were supplied to a non-notifier process")
        return self


def _load_yaml(path: Path) -> object:
    with path.open("r", encoding="utf-8") as handle:
        value = yaml.safe_load(handle)
    if value is None:
        raise ValueError(f"configuration file {path} is empty")
    return value


def load_topology(path: Path) -> TopologyConfig:
    return TopologyConfig.model_validate(_load_yaml(path))


def load_runbooks(path: Path) -> RunbookConfig:
    return RunbookConfig.model_validate(_load_yaml(path))


def export_config_schema(destination: Path) -> None:
    """Write the exact v2 configuration JSON schema used by the release."""

    import json

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(TopologyConfig.model_json_schema(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def production_environment() -> dict[str, str]:
    """Return only Signal Room variables for safe diagnostics/tests."""

    return {key: value for key, value in os.environ.items() if key.startswith("SIGNAL_ROOM_")}
