from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import uuid4

from .config import AppSettings
from .core import CoreTransport
from .models import (
    AssetDefinition,
    HealthState,
    IncidentType,
    Observation,
    ProviderKind,
    TopologyConfig,
)
from .providers import FixtureProvider, HttpProvider, ProxmoxProvider, TlsProvider

LOGGER = logging.getLogger("signal_room.collector")
Collect = Callable[[], Awaitable[list[Observation]]]


class Collector:
    """Independent, deadline-driven provider scheduler with no database access."""

    def __init__(
        self,
        settings: AppSettings,
        topology: TopologyConfig,
        core: CoreTransport,
    ) -> None:
        self.settings = settings
        self.topology = topology
        self.core = core
        self.fixture = FixtureProvider(topology.thresholds)
        self.http = HttpProvider(topology.probes)
        self.tls = TlsProvider(topology.thresholds, topology.probes)
        self.proxmox = (
            ProxmoxProvider(
                base_url=settings.pve_base_url,
                token_id=settings.pve_token_id,
                token_secret=settings.pve_token_secret,
                ca_bundle=str(settings.pve_ca_bundle),
                thresholds=topology.thresholds,
            )
            if settings.mode == "live"
            else None
        )
        self._last_runs: dict[ProviderKind, float] = {}
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    def _assets(self, kind: ProviderKind) -> list[AssetDefinition]:
        return [
            asset
            for asset in self.topology.assets
            if any(check.provider == kind for check in asset.checks)
        ]

    def _interval(self, kind: ProviderKind) -> int:
        return {
            ProviderKind.FIXTURE: self.topology.polling.fixture_seconds,
            ProviderKind.PROXMOX: self.topology.polling.proxmox_seconds,
            ProviderKind.BACKUP: self.topology.polling.backup_seconds,
            ProviderKind.HTTPS: self.topology.polling.https_seconds,
            ProviderKind.TLS: self.topology.polling.tls_seconds,
        }[kind]

    def _collector(self, kind: ProviderKind) -> Collect | None:
        assets = self._assets(kind)
        if not assets:
            return None
        if kind == ProviderKind.FIXTURE:
            return lambda: self.fixture.collect(assets)
        if kind == ProviderKind.HTTPS:
            return lambda: self.http.collect(assets)
        if kind == ProviderKind.TLS:
            return lambda: self.tls.collect(assets)
        proxmox = self.proxmox
        if kind == ProviderKind.PROXMOX and proxmox:
            return lambda: proxmox.collect(assets)
        if kind == ProviderKind.BACKUP and proxmox:
            return lambda: proxmox.collect_backups(assets)
        return None

    def _unavailable_observations(
        self, kind: ProviderKind, observed_at: datetime
    ) -> list[Observation]:
        return [
            Observation(
                asset_id=asset.id,
                check_id=check.id,
                provider=kind,
                observed_at=observed_at,
                health=HealthState.UNKNOWN,
                condition=IncidentType.MONITORING_UNAVAILABLE,
                message=f"{kind.value.title()} provider is unavailable",
                details={"source": kind.value, "severity": "critical"},
            )
            for asset in self._assets(kind)
            for check in asset.checks
            if check.provider == kind
        ]

    async def _collect_kind(self, kind: ProviderKind) -> bool:
        collect = self._collector(kind)
        if collect is None:
            return True
        run_id = str(uuid4())
        attempted_at = datetime.now(UTC)
        success = True
        error_code: str | None = None
        message = "Provider batch completed"
        try:
            async with asyncio.timeout(self.topology.polling.provider_timeout_seconds):
                observations = await collect()
            if observations and all(
                item.condition == IncidentType.MONITORING_UNAVAILABLE for item in observations
            ):
                success = False
                error_code = "provider_unavailable"
                message = "Provider returned no trusted telemetry"
        except TimeoutError:
            success = False
            error_code = "timeout"
            message = "Provider deadline expired"
            observations = self._unavailable_observations(kind, attempted_at)
        except Exception:  # noqa: BLE001 -- credentials and provider internals are redacted
            success = False
            error_code = "collection_failed"
            message = "Provider collection failed"
            observations = self._unavailable_observations(kind, attempted_at)
            LOGGER.exception("%s provider failed", kind.value)
        completed_at = datetime.now(UTC)
        await self.core.call(
            "ingest_batch",
            {
                "provider": kind.value,
                "run_id": run_id,
                "attempted_at": attempted_at.isoformat(),
                "completed_at": completed_at.isoformat(),
                "success": success,
                "error_code": error_code,
                "message": message,
                "observations": [item.model_dump(mode="json") for item in observations],
            },
        )
        return success

    async def collect_once(self) -> None:
        for kind in ProviderKind:
            await self._collect_kind(kind)
        await self._record_heartbeat()

    async def collect_due(self, monotonic_now: float | None = None) -> None:
        now = monotonic_now if monotonic_now is not None else time.monotonic()
        for kind in ProviderKind:
            if self._collector(kind) is None:
                continue
            last_run = self._last_runs.get(kind)
            if last_run is None or now - last_run >= self._interval(kind):
                await self._collect_kind(kind)
                self._last_runs[kind] = now
        await self._record_heartbeat()

    async def _record_heartbeat(self) -> None:
        await self.core.call("collector_heartbeat", {"at": datetime.now(UTC).isoformat()})

    async def _wait_or_stop(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=max(0.0, seconds))
            return True
        except TimeoutError:
            return False

    async def _provider_loop(self, kind: ProviderKind) -> None:
        interval = self._interval(kind)
        deadline = time.monotonic()
        failures = 0
        while not self._stop.is_set():
            if await self._wait_or_stop(deadline - time.monotonic()):
                return
            try:
                success = await self._collect_kind(kind)
            except Exception:  # noqa: BLE001 -- core outages use bounded retry too
                LOGGER.exception("could not submit %s provider batch", kind.value)
                success = False
            failures = 0 if success else failures + 1
            backoff = min(
                self.topology.polling.max_backoff_seconds,
                interval * (2 ** min(failures, 6)),
            )
            base = interval if success else backoff
            jitter = base * self.topology.polling.jitter_ratio
            deadline = (
                time.monotonic() + base + random.uniform(-jitter, jitter)  # noqa: S311  # nosec B311
            )

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._record_heartbeat()
            except Exception:  # noqa: BLE001
                LOGGER.exception("collector heartbeat could not reach core")
            if await self._wait_or_stop(15):
                return

    async def run_forever(self) -> None:
        tasks = [
            asyncio.create_task(self._provider_loop(kind), name=f"provider-{kind.value}")
            for kind in ProviderKind
            if self._collector(kind) is not None
        ]
        tasks.append(asyncio.create_task(self._heartbeat_loop(), name="collector-heartbeat"))
        try:
            await self._stop.wait()
        finally:
            self._stop.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
