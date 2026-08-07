from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    """Base class for every persisted and public contract.

    Configuration and API drift is dangerous for an operations console.  Unknown
    fields therefore fail closed instead of being silently ignored.
    """

    model_config = ConfigDict(extra="forbid")


class AssetKind(StrEnum):
    NODE = "node"
    GUEST = "guest"
    STORAGE = "storage"
    SERVICE = "service"
    EXTERNAL = "external"


class CheckKind(StrEnum):
    FIXTURE = "fixture"
    PROXMOX_NODE = "proxmox_node"
    PROXMOX_GUEST = "proxmox_guest"
    PROXMOX_STORAGE = "proxmox_storage"
    PROXMOX_BACKUP = "proxmox_backup"
    HTTPS = "https"
    TLS = "tls"


class ProviderKind(StrEnum):
    FIXTURE = "fixture"
    PROXMOX = "proxmox"
    BACKUP = "backup"
    HTTPS = "https"
    TLS = "tls"


class HealthState(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"
    UNKNOWN = "unknown"


class IncidentState(StrEnum):
    OPEN = "open"
    RECOVERING = "recovering"
    RESOLVED = "resolved"
    CLOSED = "closed"


class IncidentType(StrEnum):
    MONITORING_UNAVAILABLE = "monitoring_unavailable"
    ASSET_DOWN = "asset_down"
    RESOURCE_PRESSURE = "resource_pressure"
    BACKUP_FAILED = "backup_failed"
    BACKUP_STALE = "backup_stale"
    HTTP_FAILED = "http_failed"
    CERTIFICATE_EXPIRING = "certificate_expiring"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class CheckDefinition(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    type: CheckKind
    node: str | None = Field(default=None, min_length=1, max_length=80)
    guest_id: int | None = Field(default=None, ge=1, le=999_999_999)
    storage: str | None = Field(default=None, min_length=1, max_length=80)
    backup_job_id: str | None = Field(default=None, min_length=1, max_length=120)
    url: HttpUrl | None = None
    mode: Literal["origin", "edge"] = "origin"
    expected_statuses: list[int] = Field(default_factory=lambda: [200], min_length=1)
    allowed_ports: list[int] = Field(default_factory=lambda: [443], min_length=1, max_length=8)
    allow_same_host_redirects: bool = False

    @model_validator(mode="after")
    def validate_contract(self) -> CheckDefinition:
        if len(set(self.expected_statuses)) != len(self.expected_statuses):
            raise ValueError("expected statuses must be unique")
        if any(code < 100 or code > 599 for code in self.expected_statuses):
            raise ValueError("expected statuses must be valid HTTP status codes")
        if len(set(self.allowed_ports)) != len(self.allowed_ports):
            raise ValueError("allowed ports must be unique")
        if any(port < 1 or port > 65_535 for port in self.allowed_ports):
            raise ValueError("allowed ports must be valid TCP ports")

        proxmox = {
            CheckKind.PROXMOX_NODE,
            CheckKind.PROXMOX_GUEST,
            CheckKind.PROXMOX_STORAGE,
            CheckKind.PROXMOX_BACKUP,
        }
        if self.type in proxmox and not self.node:
            raise ValueError(f"{self.type} checks require a node")
        if self.type == CheckKind.PROXMOX_GUEST and self.guest_id is None:
            raise ValueError("proxmox_guest checks require guest_id")
        if self.type == CheckKind.PROXMOX_STORAGE and not self.storage:
            raise ValueError("proxmox_storage checks require storage")
        if self.type == CheckKind.PROXMOX_BACKUP and not (
            self.backup_job_id or self.guest_id is not None
        ):
            raise ValueError("proxmox_backup checks require backup_job_id or guest_id")

        if self.type in {CheckKind.HTTPS, CheckKind.TLS}:
            if self.url is None:
                raise ValueError(f"{self.type} checks require a URL")
            if self.url.scheme != "https":
                raise ValueError("outbound checks must use HTTPS")
            if self.url.username or self.url.password:
                raise ValueError("outbound check URLs cannot contain userinfo")
            if (self.url.port or 443) not in self.allowed_ports:
                raise ValueError("URL port is not in allowed_ports")
        elif self.url is not None:
            raise ValueError("only https and tls checks accept a URL")
        return self

    @property
    def provider(self) -> ProviderKind:
        if self.type == CheckKind.FIXTURE:
            return ProviderKind.FIXTURE
        if self.type in {
            CheckKind.PROXMOX_NODE,
            CheckKind.PROXMOX_GUEST,
            CheckKind.PROXMOX_STORAGE,
        }:
            return ProviderKind.PROXMOX
        if self.type == CheckKind.PROXMOX_BACKUP:
            return ProviderKind.BACKUP
        if self.type == CheckKind.HTTPS:
            return ProviderKind.HTTPS
        return ProviderKind.TLS


class AssetDefinition(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    label: str = Field(min_length=1, max_length=80)
    kind: AssetKind
    depends_on: list[str] = Field(default_factory=list, max_length=16)
    checks: list[CheckDefinition] = Field(min_length=1, max_length=16)
    runbook_id: str | None = Field(default=None, max_length=80)
    sort_order: int = Field(default=0, ge=0, le=10_000)

    @model_validator(mode="after")
    def validate_asset(self) -> AssetDefinition:
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError(f"asset {self.id!r} has duplicate dependencies")
        if self.id in self.depends_on:
            raise ValueError(f"asset {self.id!r} cannot depend on itself")
        check_ids = {check.id for check in self.checks}
        if len(check_ids) != len(self.checks):
            raise ValueError(f"asset {self.id!r} has duplicate check ids")
        return self

    @property
    def parent_id(self) -> str | None:
        """Compatibility view used only by the v0.1 drill renderer."""

        return self.depends_on[0] if self.depends_on else None


class PollingConfig(StrictModel):
    proxmox_seconds: int = Field(default=20, ge=10, le=3600)
    https_seconds: int = Field(default=30, ge=10, le=3600)
    backup_seconds: int = Field(default=900, ge=60, le=86_400)
    tls_seconds: int = Field(default=21_600, ge=3600, le=86_400)
    fixture_seconds: int = Field(default=5, ge=1, le=300)
    provider_timeout_seconds: int = Field(default=8, ge=1, le=60)
    max_backoff_seconds: int = Field(default=300, ge=10, le=3600)
    jitter_ratio: float = Field(default=0.1, ge=0, le=0.5)


class ThresholdConfig(StrictModel):
    cpu_warning_ratio: float = Field(default=0.90, gt=0, le=1)
    memory_warning_ratio: float = Field(default=0.90, gt=0, le=1)
    memory_critical_ratio: float = Field(default=0.97, gt=0, le=1)
    disk_warning_ratio: float = Field(default=0.85, gt=0, le=1)
    disk_critical_ratio: float = Field(default=0.95, gt=0, le=1)
    backup_warning_hours: int = Field(default=192, ge=1)
    backup_critical_hours: int = Field(default=240, ge=1)
    certificate_warning_days: int = Field(default=30, ge=1)
    certificate_critical_days: int = Field(default=7, ge=1)
    failure_observations: int = Field(default=3, ge=1, le=20)
    recovery_observations: int = Field(default=2, ge=1, le=20)
    correlation_window_seconds: int = Field(default=60, ge=5, le=600)
    resource_warning_seconds: int = Field(default=300, ge=60, le=3600)

    @model_validator(mode="after")
    def validate_ordering(self) -> ThresholdConfig:
        if self.memory_critical_ratio < self.memory_warning_ratio:
            raise ValueError("critical memory threshold must be at least the warning threshold")
        if self.disk_critical_ratio < self.disk_warning_ratio:
            raise ValueError("critical disk threshold must be at least the warning threshold")
        if self.backup_critical_hours < self.backup_warning_hours:
            raise ValueError("critical backup age must be at least the warning age")
        if self.certificate_critical_days > self.certificate_warning_days:
            raise ValueError("critical certificate window must be inside warning window")
        return self


class ProbePolicy(StrictModel):
    allowed_hosts: list[str] = Field(default_factory=list, max_length=128)
    max_concurrency: int = Field(default=8, ge=1, le=32)
    max_response_bytes: int = Field(default=65_536, ge=1024, le=1_048_576)


class TopologyConfig(StrictModel):
    version: Literal[2]
    revision: str = Field(min_length=1, max_length=80)
    polling: PollingConfig = Field(default_factory=PollingConfig)
    thresholds: ThresholdConfig = Field(default_factory=ThresholdConfig)
    probes: ProbePolicy = Field(default_factory=ProbePolicy)
    assets: list[AssetDefinition] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_graph_and_probes(self) -> TopologyConfig:
        by_id = {asset.id: asset for asset in self.assets}
        if len(by_id) != len(self.assets):
            raise ValueError("asset ids must be unique")
        for asset in self.assets:
            for dependency in asset.depends_on:
                if dependency not in by_id:
                    raise ValueError(f"unknown dependency {dependency!r} for {asset.id!r}")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(asset_id: str) -> None:
            if asset_id in visiting:
                raise ValueError("asset dependency graph contains a cycle")
            if asset_id in visited:
                return
            visiting.add(asset_id)
            for dependency in by_id[asset_id].depends_on:
                visit(dependency)
            visiting.remove(asset_id)
            visited.add(asset_id)

        for asset_id in by_id:
            visit(asset_id)

        allowed = {host.lower().rstrip(".") for host in self.probes.allowed_hosts}
        for asset in self.assets:
            for check in asset.checks:
                if check.type in {CheckKind.HTTPS, CheckKind.TLS}:
                    if check.url is None:
                        raise ValueError(f"{check.type} checks require a URL")
                    host = (check.url.host or "").lower().rstrip(".")
                    if host not in allowed:
                        raise ValueError(f"probe host {host!r} is not explicitly allowlisted")
        return self

    def topological_assets(self) -> list[AssetDefinition]:
        emitted: set[str] = set()
        ordered: list[AssetDefinition] = []
        while len(ordered) < len(self.assets):
            ready = [
                asset
                for asset in self.assets
                if asset.id not in emitted and set(asset.depends_on) <= emitted
            ]
            for asset in sorted(ready, key=lambda item: (item.sort_order, item.id)):
                ordered.append(asset)
                emitted.add(asset.id)
        return ordered


class Runbook(StrictModel):
    title: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=300)
    checks: list[str] = Field(min_length=1, max_length=12)


class RunbookConfig(StrictModel):
    version: Literal[1]
    runbooks: dict[str, Runbook]


class Observation(StrictModel):
    asset_id: str
    check_id: str = "default"
    provider: ProviderKind = ProviderKind.FIXTURE
    provider_run_id: str | None = None
    observed_at: datetime = Field(default_factory=utc_now)
    health: HealthState
    condition: IncidentType | None = None
    message: str = Field(max_length=240)
    latency_ms: float | None = Field(default=None, ge=0)
    cpu_ratio: float | None = Field(default=None, ge=0)
    memory_ratio: float | None = Field(default=None, ge=0)
    disk_ratio: float | None = Field(default=None, ge=0)
    details: dict[str, Any] = Field(default_factory=dict)


class AssetStateView(StrictModel):
    asset_id: str
    health: HealthState
    last_observed_at: datetime | None = None
    unhealthy_since_at: datetime | None = None
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    message: str = "Awaiting first observation"
    latency_ms: float | None = None
    cpu_ratio: float | None = None
    memory_ratio: float | None = None
    disk_ratio: float | None = None


class AssetView(StrictModel):
    id: str
    label: str
    kind: AssetKind
    depends_on: list[str] = Field(default_factory=list)
    parent_id: str | None = None
    check_ids: list[str] = Field(default_factory=list)
    runbook_id: str | None = None
    sort_order: int
    retired_at: datetime | None = None


class ProviderStateView(StrictModel):
    provider: ProviderKind
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    consecutive_failures: int = 0
    status: Literal["never", "healthy", "failed", "stale"] = "never"
    message: str = "Provider has not run"


class IncidentEventView(StrictModel):
    id: int
    event_uuid: str
    incident_id: str
    created_at: datetime
    kind: str
    message: str
    actor_subject: str | None = None
    actor_email: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class IncidentNoteView(StrictModel):
    id: int
    incident_id: str
    created_at: datetime
    author: str
    body: str


class IncidentSummary(StrictModel):
    id: str
    previous_incident_id: str | None = None
    fingerprint: str
    root_asset_id: str
    incident_type: IncidentType
    severity: Severity
    state: IncidentState
    version: int
    title: str
    summary: str
    opened_at: datetime
    acknowledged_at: datetime | None = None
    acknowledged_by: str | None = None
    recovered_at: datetime | None = None
    closed_at: datetime | None = None
    closed_by: str | None = None
    affected_asset_ids: list[str] = Field(default_factory=list)


class IncidentView(IncidentSummary):
    events: list[IncidentEventView] = Field(default_factory=list)
    notes: list[IncidentNoteView] = Field(default_factory=list)
    runbook: Runbook | None = None


class Capabilities(StrictModel):
    can_mutate: bool
    drill_available: bool = True
    data_source: Literal["fixture", "live"]


class BootstrapResponse(StrictModel):
    build_version: str = "1.0.0"
    build_sha: str
    generated_at: datetime
    collector_last_seen_at: datetime | None = None
    stale: bool
    assets: list[AssetView]
    states: list[AssetStateView]
    providers: list[ProviderStateView] = Field(default_factory=list)
    incidents: list[IncidentSummary]
    capabilities: Capabilities
    last_event_id: int = 0


class AssetDetailResponse(StrictModel):
    asset: AssetView
    state: AssetStateView
    active_incidents: list[IncidentSummary] = Field(default_factory=list)


class MetricBucket(StrictModel):
    started_at: datetime
    ended_at: datetime
    sample_count: int
    expected_samples: int
    completeness: float = Field(ge=0, le=1)
    health: HealthState
    cpu_ratio: float | None = None
    memory_ratio: float | None = None
    disk_ratio: float | None = None
    latency_ms: float | None = None


class MetricThresholds(StrictModel):
    cpu_warning_ratio: float
    memory_warning_ratio: float
    memory_critical_ratio: float
    disk_warning_ratio: float
    disk_critical_ratio: float


class MetricsResponse(StrictModel):
    asset_id: str
    range: Literal["1h", "24h", "7d", "30d", "180d"]
    resolution: Literal["raw", "5m", "1h", "1d"]
    generated_at: datetime
    completeness: float = Field(ge=0, le=1)
    thresholds: MetricThresholds
    buckets: list[MetricBucket]


class NoteRequest(StrictModel):
    body: str = Field(min_length=1, max_length=2000)


class MaintenanceCreateRequest(StrictModel):
    asset_ids: list[str] = Field(min_length=1, max_length=100)
    starts_at: datetime
    ends_at: datetime
    reason: str = Field(min_length=1, max_length=240)

    @model_validator(mode="after")
    def validate_window(self) -> MaintenanceCreateRequest:
        if self.ends_at <= self.starts_at:
            raise ValueError("maintenance end must be after its start")
        if (self.ends_at - self.starts_at).total_seconds() > 86_400:
            raise ValueError("maintenance windows cannot exceed 24 hours")
        if len(set(self.asset_ids)) != len(self.asset_ids):
            raise ValueError("maintenance asset ids must be unique")
        return self


class MaintenanceWindowView(StrictModel):
    id: str
    asset_ids: list[str]
    starts_at: datetime
    ends_at: datetime
    reason: str
    created_at: datetime
    created_by: str
    cancelled_at: datetime | None = None
    cancelled_by: str | None = None
    version: int


class ActionResponse(StrictModel):
    incident: IncidentView


class MaintenanceActionResponse(StrictModel):
    maintenance: MaintenanceWindowView


class IncidentPage(StrictModel):
    items: list[IncidentSummary]
    next_cursor: str | None = None


class TimelinePage(StrictModel):
    items: list[IncidentEventView]
    next_cursor: str | None = None


class NotificationStatus(StrictModel):
    enabled: bool
    pending: int = 0
    delivered: int = 0
    dead_letter: int = 0
    suppressed: int = 0
    last_success_at: datetime | None = None


class DiagnosticsResponse(StrictModel):
    request_id: str
    build_version: str
    build_sha: str
    schema_version: int
    configuration_revision: str
    database_ok: bool
    collector_fresh: bool
    providers: list[ProviderStateView]
    notifications: NotificationStatus


class StreamEventView(StrictModel):
    id: int
    event_uuid: str
    created_at: datetime
    topic: Literal["snapshot", "incident", "provider", "notification", "maintenance"]
    kind: str
    subject_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
