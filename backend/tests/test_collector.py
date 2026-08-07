from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from signal_room.collector import Collector
from signal_room.config import AppSettings
from signal_room.models import (
    HealthState,
    IncidentType,
    Observation,
    ProviderKind,
    TopologyConfig,
)


class CaptureCore:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.error = error

    async def call(self, method: str, params: dict[str, object] | None = None) -> object:
        self.calls.append((method, params or {}))
        if self.error:
            raise self.error
        return {}


def mixed_topology() -> TopologyConfig:
    return TopologyConfig.model_validate(
        {
            "version": 2,
            "revision": "mixed-test",
            "polling": {"jitter_ratio": 0},
            "probes": {"allowed_hosts": ["example.com"]},
            "assets": [
                {
                    "id": "fixture-asset",
                    "label": "Fixture",
                    "kind": "node",
                    "checks": [{"id": "fixture-check", "type": "fixture"}],
                },
                {
                    "id": "pve-node",
                    "label": "PVE",
                    "kind": "node",
                    "checks": [{"id": "pve-check", "type": "proxmox_node", "node": "pve"}],
                },
                {
                    "id": "backup-job",
                    "label": "Backup",
                    "kind": "external",
                    "checks": [
                        {
                            "id": "backup-check",
                            "type": "proxmox_backup",
                            "node": "pve",
                            "backup_job_id": "nightly",
                        }
                    ],
                },
                {
                    "id": "web-check",
                    "label": "Web",
                    "kind": "service",
                    "checks": [
                        {"id": "https-check", "type": "https", "url": "https://example.com"}
                    ],
                },
                {
                    "id": "tls-check",
                    "label": "TLS",
                    "kind": "external",
                    "checks": [{"id": "certificate", "type": "tls", "url": "https://example.com"}],
                },
            ],
        }
    )


async def test_collector_honours_provider_interval_and_updates_heartbeat(
    settings: AppSettings,
    topology: TopologyConfig,
) -> None:
    core = CaptureCore()
    collector = Collector(settings, topology, core)

    await collector.collect_due(1_000)
    assert collector.fixture.tick == 1
    assert any(method == "collector_heartbeat" for method, _ in core.calls)

    await collector.collect_due(1_001)
    assert collector.fixture.tick == 1

    await collector.collect_due(1_000 + topology.polling.fixture_seconds)
    assert collector.fixture.tick == 2
    batches = [params for method, params in core.calls if method == "ingest_batch"]
    assert len(batches) == 2
    assert all(batch["provider"] == "fixture" for batch in batches)


def test_collector_maps_every_provider_and_builds_unavailable_observations(
    settings: AppSettings,
) -> None:
    topology = mixed_topology()
    live = settings.model_copy(
        update={
            "mode": "live",
            "pve_base_url": "https://pve.test:8006",
            "pve_token_id": "signal-room@pve!<token-id>",
            "pve_token_secret": "secret",  # pragma: allowlist secret
            "pve_ca_bundle": settings.config_path,
        }
    )
    collector = Collector(live, topology, CaptureCore())
    for kind in ProviderKind:
        assert len(collector._assets(kind)) == 1
        assert collector._interval(kind) > 0
        assert collector._collector(kind) is not None
        observations = collector._unavailable_observations(kind, datetime.now(UTC))
        assert len(observations) == 1
        assert observations[0].condition == IncidentType.MONITORING_UNAVAILABLE
        assert observations[0].details["severity"] == "critical"

    offline = Collector(settings, topology, CaptureCore())
    assert offline._collector(ProviderKind.PROXMOX) is None
    assert offline._collector(ProviderKind.BACKUP) is None


async def test_collect_kind_reports_success_unavailability_timeout_and_failure(
    settings: AppSettings,
    topology: TopologyConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = CaptureCore()
    collector = Collector(settings, topology, core)
    healthy = Observation(
        asset_id="atlas-node",
        check_id="fixture-health",
        provider=ProviderKind.FIXTURE,
        health=HealthState.HEALTHY,
        message="Healthy",
    )

    async def collect_healthy(assets: object) -> list[Observation]:
        return [healthy]

    monkeypatch.setattr(collector.fixture, "collect", collect_healthy)
    assert await collector._collect_kind(ProviderKind.FIXTURE)
    assert core.calls[-1][1]["success"] is True

    unavailable = healthy.model_copy(
        update={
            "health": HealthState.UNKNOWN,
            "condition": IncidentType.MONITORING_UNAVAILABLE,
        }
    )

    async def collect_unavailable(assets: object) -> list[Observation]:
        return [unavailable]

    monkeypatch.setattr(collector.fixture, "collect", collect_unavailable)
    assert not await collector._collect_kind(ProviderKind.FIXTURE)
    assert core.calls[-1][1]["error_code"] == "provider_unavailable"

    async def collect_failure(assets: object) -> list[Observation]:
        raise RuntimeError("credential must not escape")

    monkeypatch.setattr(collector.fixture, "collect", collect_failure)
    assert not await collector._collect_kind(ProviderKind.FIXTURE)
    assert core.calls[-1][1]["error_code"] == "collection_failed"
    assert "credential" not in str(core.calls[-1][1])
    assert len(core.calls[-1][1]["observations"]) == len(topology.assets)

    async def collect_slowly(assets: object) -> list[Observation]:
        await asyncio.sleep(0.01)
        return [healthy]

    fast_polling = topology.polling.model_copy(update={"provider_timeout_seconds": 0})
    timed = Collector(settings, topology.model_copy(update={"polling": fast_polling}), core)
    monkeypatch.setattr(timed.fixture, "collect", collect_slowly)
    assert not await timed._collect_kind(ProviderKind.FIXTURE)
    assert core.calls[-1][1]["error_code"] == "timeout"
    assert await collector._collect_kind(ProviderKind.HTTPS)


async def test_collect_once_and_due_cover_scheduler_paths(
    settings: AppSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topology = mixed_topology()
    collector = Collector(settings, topology, CaptureCore())
    kinds: list[ProviderKind] = []

    async def collect(kind: ProviderKind) -> bool:
        kinds.append(kind)
        return True

    monkeypatch.setattr(collector, "_collect_kind", collect)
    await collector.collect_once()
    assert kinds == list(ProviderKind)

    kinds.clear()
    monkeypatch.setattr("signal_room.collector.time.monotonic", lambda: 500.0)
    await collector.collect_due()
    assert kinds == [ProviderKind.FIXTURE, ProviderKind.HTTPS, ProviderKind.TLS]
    kinds.clear()
    await collector.collect_due(501.0)
    assert kinds == []


async def test_wait_stop_and_provider_retry_paths(
    settings: AppSettings,
    topology: TopologyConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collector = Collector(settings, topology, CaptureCore())
    assert not await collector._wait_or_stop(0)
    collector.stop()
    assert await collector._wait_or_stop(10)

    retrying = Collector(settings, topology, CaptureCore())

    async def fail_and_stop(kind: ProviderKind) -> bool:
        retrying.stop()
        return False

    async def do_not_wait(seconds: float) -> bool:
        return False

    monkeypatch.setattr(retrying, "_collect_kind", fail_and_stop)
    monkeypatch.setattr(retrying, "_wait_or_stop", do_not_wait)
    monkeypatch.setattr("signal_room.collector.random.uniform", lambda low, high: 0.0)
    await retrying._provider_loop(ProviderKind.FIXTURE)

    exploding = Collector(settings, topology, CaptureCore())

    async def explode_and_stop(kind: ProviderKind) -> bool:
        exploding.stop()
        raise RuntimeError("core unavailable")

    monkeypatch.setattr(exploding, "_collect_kind", explode_and_stop)
    monkeypatch.setattr(exploding, "_wait_or_stop", do_not_wait)
    await exploding._provider_loop(ProviderKind.FIXTURE)


async def test_heartbeat_and_forever_loops_shutdown_cleanly(
    settings: AppSettings,
    topology: TopologyConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collector = Collector(settings, topology, CaptureCore())

    async def heartbeat_failure() -> None:
        raise RuntimeError("core unavailable")

    async def stop_wait(seconds: float) -> bool:
        collector.stop()
        return True

    monkeypatch.setattr(collector, "_record_heartbeat", heartbeat_failure)
    monkeypatch.setattr(collector, "_wait_or_stop", stop_wait)
    await collector._heartbeat_loop()

    running = Collector(settings, topology, CaptureCore())

    async def idle_provider(kind: ProviderKind) -> None:
        await running._stop.wait()

    async def idle_heartbeat() -> None:
        await running._stop.wait()

    monkeypatch.setattr(running, "_provider_loop", idle_provider)
    monkeypatch.setattr(running, "_heartbeat_loop", idle_heartbeat)
    task = asyncio.create_task(running.run_forever())
    await asyncio.sleep(0)
    running.stop()
    await task


async def test_core_submission_failures_propagate_to_retry_loop(
    settings: AppSettings, topology: TopologyConfig
) -> None:
    collector = Collector(settings, topology, CaptureCore(RuntimeError("core down")))
    with pytest.raises(RuntimeError, match="core down"):
        await collector._collect_kind(ProviderKind.FIXTURE)
