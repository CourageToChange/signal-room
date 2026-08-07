from __future__ import annotations

import asyncio
import json
import socket
import ssl
from datetime import UTC, datetime, timedelta
from typing import Any

import certifi
import httpx
import pytest
from signal_room.models import (
    AssetDefinition,
    AssetKind,
    CheckDefinition,
    CheckKind,
    ProbePolicy,
    ProviderKind,
    ThresholdConfig,
)
from signal_room.providers import (
    FixtureProvider,
    HttpProvider,
    ProxmoxProvider,
    TlsProvider,
    UnsafeEndpointError,
    _ratio,
    _resource_health,
    assets_for_provider,
    resolve_public_addresses,
)


def asset(
    identifier: str,
    check: CheckDefinition,
    kind: AssetKind = AssetKind.SERVICE,
) -> AssetDefinition:
    return AssetDefinition(id=identifier, label=identifier.title(), kind=kind, checks=[check])


def https_check(
    identifier: str = "https-check",
    *,
    url: str = "https://health.example.test/health",
    statuses: list[int] | None = None,
    redirects: bool = False,
    ports: list[int] | None = None,
) -> CheckDefinition:
    return CheckDefinition(
        id=identifier,
        type=CheckKind.HTTPS,
        url=url,
        expected_statuses=statuses or [200],
        allowed_ports=ports or [443],
        allow_same_host_redirects=redirects,
    )


async def public_resolver(host: str, port: int) -> set[str]:
    return {"93.184.216.34"}


async def test_address_resolution_rejects_empty_invalid_and_non_public_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers: list[tuple[Any, ...]] = []

    async def getaddrinfo(
        self: object, host: str, port: int, *, type: int
    ) -> list[tuple[Any, ...]]:
        return answers

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", getaddrinfo)
    with pytest.raises(UnsafeEndpointError, match="did not resolve"):
        await resolve_public_addresses("empty.example", 443)
    answers[:] = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("invalid", 443))]
    with pytest.raises(UnsafeEndpointError, match="invalid address"):
        await resolve_public_addresses("invalid.example", 443)
    answers[:] = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
    with pytest.raises(UnsafeEndpointError, match="non-public"):
        await resolve_public_addresses("private.example", 443)
    answers[:] = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700:4700::1111%4", 443)),
    ]
    assert await resolve_public_addresses("public.example", 443) == {
        "8.8.8.8",
        "2606:4700:4700::1111",
    }


def test_resource_normalization_covers_every_threshold_class() -> None:
    thresholds = ThresholdConfig()
    assert _ratio("50", "100") == 0.5
    assert _ratio(-2, 4) == 0
    assert _ratio("bad", 4) is None
    assert _ratio(1, 0) is None
    assert (
        _resource_health(status="stopped", cpu=None, memory=None, disk=None, thresholds=thresholds)[
            0
        ]
        == "down"
    )
    assert (
        _resource_health(status="running", cpu=0.1, memory=0.98, disk=0.1, thresholds=thresholds)[2]
        == "critical"
    )
    assert (
        _resource_health(status="online", cpu=0.1, memory=0.1, disk=0.96, thresholds=thresholds)[2]
        == "critical"
    )
    warning = _resource_health(
        status="available", cpu=0.91, memory=0.91, disk=0.86, thresholds=thresholds
    )
    assert warning[0] == "degraded" and "CPU" in warning[1]
    assert (
        _resource_health(status="active", cpu=0.1, memory=0.1, disk=0.1, thresholds=thresholds)[0]
        == "healthy"
    )


async def test_fixture_provider_covers_every_asset_shape() -> None:
    check = CheckDefinition(id="fixture-check", type=CheckKind.FIXTURE)
    assets = [
        asset("fixture-node", check, AssetKind.NODE),
        asset("fixture-guest", check, AssetKind.GUEST),
        asset("fixture-storage", check, AssetKind.STORAGE),
        asset("backup-fixture", check, AssetKind.EXTERNAL),
        asset("fixture-service", check, AssetKind.SERVICE),
    ]
    provider = FixtureProvider(ThresholdConfig())
    observations = await provider.collect(assets)
    assert len(observations) == 5
    assert observations[0].cpu_ratio is not None
    assert observations[1].memory_ratio is not None
    assert observations[2].disk_ratio == 0.58
    assert observations[3].details["age_hours"] == 11
    assert observations[4].latency_ms is not None


async def test_proxmox_resource_collection_handles_missing_malformed_and_pressure() -> None:
    resources = {
        "data": [
            {
                "type": "node",
                "node": "pve",
                "status": "online",
                "cpu": 0.1,
                "mem": 98,
                "maxmem": 100,
                "disk": 1,
                "maxdisk": 100,
                "uptime": 50,
            },
            {
                "type": "lxc",
                "node": "pve",
                "vmid": 104,
                "status": "stopped",
                "cpu": 0,
                "mem": 1,
                "maxmem": 100,
            },
            {
                "type": "storage",
                "node": "pve",
                "storage": "local",
                "status": "available",
                "disk": "bad",
                "maxdisk": 100,
                "cpu": "bad",
                "uptime": "bad",
            },
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=resources, request=request)

    provider = ProxmoxProvider(
        base_url="https://pve.example.test:8006",
        token_id="audit@pve!collector",
        token_secret="secret",  # pragma: allowlist secret
        ca_bundle=certifi.where(),
        thresholds=ThresholdConfig(),
        transport=httpx.MockTransport(handler),
    )
    targets = [
        asset(
            "node-target",
            CheckDefinition(id="node", type=CheckKind.PROXMOX_NODE, node="pve"),
            AssetKind.NODE,
        ),
        asset(
            "guest-target",
            CheckDefinition(id="guest", type=CheckKind.PROXMOX_GUEST, node="pve", guest_id=104),
            AssetKind.GUEST,
        ),
        asset(
            "storage-target",
            CheckDefinition(
                id="storage", type=CheckKind.PROXMOX_STORAGE, node="pve", storage="local"
            ),
            AssetKind.STORAGE,
        ),
        asset(
            "missing-target",
            CheckDefinition(id="missing", type=CheckKind.PROXMOX_GUEST, node="pve", guest_id=999),
            AssetKind.GUEST,
        ),
    ]
    observations = await provider.collect(targets)
    assert [item.health for item in observations] == ["degraded", "down", "unknown", "unknown"]
    assert observations[0].condition == "resource_pressure"
    assert observations[1].condition == "asset_down"
    assert observations[2].message == "Proxmox returned malformed resource telemetry"
    assert observations[3].message == "Configured resource was not returned by Proxmox"

    for payload, status in [({"unexpected": []}, 200), ({"data": {}}, 200), ({}, 503)]:

        def broken(
            request: httpx.Request, body: dict[str, Any] = payload, code: int = status
        ) -> httpx.Response:
            return httpx.Response(code, content=json.dumps(body), request=request)

        failing = ProxmoxProvider(
            base_url="https://pve.example.test:8006",
            token_id="audit@pve!collector",
            token_secret="must-not-leak",  # pragma: allowlist secret
            ca_bundle=certifi.where(),
            thresholds=ThresholdConfig(),
            transport=httpx.MockTransport(broken),
        )
        result = await failing.collect([targets[0]])
        assert result[0].health == "unknown"
        assert "must-not-leak" not in result[0].message


async def test_backup_collection_matches_exact_jobs_and_latest_attempt() -> None:
    now = datetime.now(UTC)
    tasks = [
        {
            "node": "pve",
            "job-id": "failed-job",
            "status": "OK",
            "endtime": int((now - timedelta(hours=2)).timestamp()),
        },
        {
            "node": "pve",
            "job-id": "failed-job",
            "status": "ERROR",
            "endtime": int((now - timedelta(hours=1)).timestamp()),
        },
        {
            "node": "pve",
            "job-id": "fresh-job",
            "status": "OK",
            "endtime": int((now - timedelta(hours=1)).timestamp()),
        },
        {
            "node": "pve",
            "job-id": "warning-job",
            "status": "OK",
            "endtime": int((now - timedelta(hours=200)).timestamp()),
        },
        {
            "node": "pve",
            "job-id": "critical-job",
            "status": "OK",
            "endtime": int((now - timedelta(hours=250)).timestamp()),
        },
        {"node": "other", "job-id": "fresh-job", "status": "OK", "endtime": int(now.timestamp())},
        {"node": "pve", "job-id": "bad-time", "status": "OK", "endtime": "invalid"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": tasks}, request=request)

    provider = ProxmoxProvider(
        base_url="https://pve.example.test:8006",
        token_id="audit@pve!collector",
        token_secret="secret",  # pragma: allowlist secret
        ca_bundle=certifi.where(),
        thresholds=ThresholdConfig(),
        transport=httpx.MockTransport(handler),
    )
    job_names = [
        "failed-job",
        "fresh-job",
        "warning-job",
        "critical-job",
        "missing-job",
        "bad-time",
    ]
    targets = [
        asset(
            f"backup-{index}",
            CheckDefinition(
                id=f"backup-{index}", type=CheckKind.PROXMOX_BACKUP, node="pve", backup_job_id=job
            ),
            AssetKind.EXTERNAL,
        )
        for index, job in enumerate(job_names)
    ]
    result = await provider.collect_backups(targets)
    assert [item.condition for item in result] == [
        "backup_failed",
        None,
        "backup_stale",
        "backup_stale",
        "backup_stale",
        "backup_stale",
    ]
    assert [item.health for item in result[:5]] == ["down", "healthy", "degraded", "down", "down"]
    assert result[0].details["latest_success_at"] is not None

    def unavailable(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": ["malformed"]}, request=request)

    broken = ProxmoxProvider(
        base_url="https://pve.example.test:8006",
        token_id="audit@pve!collector",
        token_secret="secret",  # pragma: allowlist secret
        ca_bundle=certifi.where(),
        thresholds=ThresholdConfig(),
        transport=httpx.MockTransport(unavailable),
    )
    assert (await broken.collect_backups([targets[0]]))[0].health == "unknown"


async def test_backup_collection_reads_guest_results_from_scheduled_parent_logs() -> None:
    now = datetime.now(UTC)
    tasks = [
        {
            "node": "pve",
            "id": "",
            "upid": "UPID:pve:running-parent",
            "status": "",
            "starttime": int((now - timedelta(minutes=5)).timestamp()),
        },
        {
            "node": "pve",
            "id": "",
            "upid": "UPID:pve:new-parent",
            "status": "job errors",
            "starttime": int((now - timedelta(hours=1, minutes=5)).timestamp()),
            "endtime": int((now - timedelta(hours=1)).timestamp()),
        },
        {
            "node": "pve",
            "id": "",
            "upid": "UPID:pve:old-parent",
            "status": "OK",
            "starttime": int((now - timedelta(hours=25, minutes=5)).timestamp()),
            "endtime": int((now - timedelta(hours=25)).timestamp()),
        },
    ]
    logs = {
        "running-parent": ["INFO: Starting Backup of VM 109 (lxc)"],
        "new-parent": [
            "INFO: Starting Backup of VM 107 (lxc)",
            "ERROR: Backup of VM 107 failed - private diagnostic",
            "INFO: Starting Backup of VM 108 (lxc)",
            "INFO: Finished Backup of VM 108 (00:00:10)",
        ],
        "old-parent": [
            "INFO: Starting Backup of VM 107 (lxc)",
            "INFO: Finished Backup of VM 107 (00:00:10)",
            "INFO: Starting Backup of VM 108 (lxc)",
            "INFO: Finished Backup of VM 108 (00:00:10)",
        ],
    }
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/tasks"):
            return httpx.Response(200, json={"data": tasks}, request=request)
        name = next(key for key in logs if key in request.url.path)
        payload = [{"n": index, "t": line} for index, line in enumerate(logs[name], 1)]
        return httpx.Response(200, json={"data": payload}, request=request)

    provider = ProxmoxProvider(
        base_url="https://pve.example.test:8006",
        token_id="audit@pve!collector",
        token_secret="secret",  # pragma: allowlist secret
        ca_bundle=certifi.where(),
        thresholds=ThresholdConfig(),
        transport=httpx.MockTransport(handler),
    )
    targets = [
        asset(
            f"backup-{guest_id}",
            CheckDefinition(
                id=f"backup-{guest_id}",
                type=CheckKind.PROXMOX_BACKUP,
                node="pve",
                guest_id=guest_id,
            ),
            AssetKind.EXTERNAL,
        )
        for guest_id in (107, 108, 109, 110)
    ]

    result = await provider.collect_backups(targets)

    assert [item.health for item in result] == ["down", "healthy", "unknown", "down"]
    assert [item.condition for item in result] == [
        "backup_failed",
        None,
        None,
        "backup_stale",
    ]
    assert result[0].details["latest_success_at"] is not None
    assert "private diagnostic" not in result[0].message
    assert all(request.method == "GET" for request in requests)
    assert sum(request.url.path.endswith("/log") for request in requests) == 3
    await provider.collect_backups(targets)
    assert sum(request.url.path.endswith("/log") for request in requests) == 4


async def test_http_probe_pins_addresses_redirects_and_bounds_responses() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"Location": "/final"}, request=request)
        if request.url.path == "/cross":
            return httpx.Response(
                302, headers={"Location": "https://evil.example/"}, request=request
            )
        if request.url.path == "/unexpected":
            return httpx.Response(503, request=request)
        if request.url.path == "/declared-large":
            return httpx.Response(200, headers={"Content-Length": "999999"}, request=request)
        if request.url.path == "/invalid-length":
            return httpx.Response(
                200, content=b"ok", headers={"Content-Length": "invalid"}, request=request
            )
        if request.url.path == "/stream-large":
            return httpx.Response(200, content=b"x" * 2048, request=request)
        return httpx.Response(200, content=b"ok", request=request)

    policy = ProbePolicy(allowed_hosts=["health.example.test"], max_response_bytes=1024)
    provider = HttpProvider(
        policy, resolver=public_resolver, transport=httpx.MockTransport(handler)
    )
    checks = [
        https_check("ok", url="https://health.example.test/ok"),
        https_check("unexpected", url="https://health.example.test/unexpected"),
        https_check("redirect", url="https://health.example.test/redirect", redirects=True),
        https_check("cross", url="https://health.example.test/cross", redirects=True),
        https_check("declared", url="https://health.example.test/declared-large"),
        https_check("streamed", url="https://health.example.test/stream-large"),
        https_check("invalid", url="https://health.example.test/invalid-length"),
    ]
    result = await provider.collect(
        [asset(f"http-{index}", check) for index, check in enumerate(checks)]
    )
    assert [item.health for item in result] == [
        "healthy",
        "down",
        "healthy",
        "down",
        "down",
        "down",
        "healthy",
    ], {
        "results": [(item.asset_id, item.message, item.details) for item in result],
        "requests": [(str(request.url), dict(request.headers)) for request in requests],
    }
    assert requests[0].url.host == "93.184.216.34"
    assert requests[0].headers["Host"] == "health.example.test"
    assert any(request.url.path == "/final" for request in requests)

    not_allowed = HttpProvider(
        ProbePolicy(allowed_hosts=["other.example"]),
        resolver=public_resolver,
        transport=httpx.MockTransport(handler),
    )
    blocked = await not_allowed.collect([asset("blocked", checks[0])])
    assert blocked[0].health == "down"

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UnsafeEndpointError, match="valid HTTPS"):
            await provider._pinned_request(client, "http://health.example.test", checks[0])


async def test_http_probe_formats_ipv6_and_nonstandard_allowed_ports() -> None:
    seen: list[httpx.Request] = []

    async def ipv6_resolver(host: str, port: int) -> set[str]:
        return {"2606:4700:4700::1111"}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, request=request)

    check = https_check(
        "custom-port",
        url="https://health.example.test:8443/path",
        ports=[8443],
    )
    provider = HttpProvider(
        ProbePolicy(allowed_hosts=["health.example.test"]),
        resolver=ipv6_resolver,
        transport=httpx.MockTransport(handler),
    )
    assert (await provider.collect([asset("ipv6-service", check)]))[0].health == "healthy"
    assert seen[0].url.port == 8443
    assert seen[0].headers["Host"] == "health.example.test:8443"


class FakeSslObject:
    def __init__(self, expiry: str | None) -> None:
        self.expiry = expiry

    def getpeercert(self) -> dict[str, str]:
        return {"notAfter": self.expiry} if self.expiry else {}


class FakeTlsWriter:
    def __init__(self, expiry: str | None) -> None:
        self.expiry = expiry
        self.closed = False

    def get_extra_info(self, name: str) -> FakeSslObject | None:
        return FakeSslObject(self.expiry) if name == "ssl_object" else None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


async def test_tls_probe_reports_healthy_warning_critical_and_safe_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expiry: str | None = "certificate"
    days = 60

    async def open_connection(*args: object, **kwargs: object) -> tuple[None, FakeTlsWriter]:
        return None, FakeTlsWriter(expiry)

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    monkeypatch.setattr(ssl, "create_default_context", lambda: object())
    monkeypatch.setattr(
        ssl,
        "cert_time_to_seconds",
        lambda value: (datetime.now(UTC) + timedelta(days=days)).timestamp(),
    )
    tls_check = CheckDefinition(
        id="tls-check",
        type=CheckKind.TLS,
        url="https://health.example.test/",
        allowed_ports=[443],
    )
    provider = TlsProvider(
        ThresholdConfig(),
        ProbePolicy(allowed_hosts=["health.example.test"]),
        resolver=public_resolver,
    )
    target = asset("certificate", tls_check, AssetKind.EXTERNAL)
    assert (await provider.collect([target]))[0].health == "healthy"
    days = 15
    assert (await provider.collect([target]))[0].health == "degraded"
    days = 2
    assert (await provider.collect([target]))[0].health == "down"
    expiry = None
    assert (await provider.collect([target]))[0].message == "TLS certificate check failed safely"

    blocked = TlsProvider(
        ThresholdConfig(), ProbePolicy(allowed_hosts=[]), resolver=public_resolver
    )
    assert (await blocked.collect([target]))[0].health == "down"


def test_asset_provider_filter_accepts_enum_and_string() -> None:
    fixture = asset(
        "fixture-filter",
        CheckDefinition(id="fixture-filter-check", type=CheckKind.FIXTURE),
    )
    http = asset("http-filter", https_check("http-filter-check"))
    assert assets_for_provider([fixture, http], {ProviderKind.FIXTURE}) == [fixture]
    assert assets_for_provider([fixture, http], {"https"}) == [http]
