from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
import time
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from typing import Any, Protocol, cast
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import httpx

from .models import (
    AssetDefinition,
    AssetKind,
    CheckDefinition,
    CheckKind,
    HealthState,
    IncidentType,
    Observation,
    ProbePolicy,
    ProviderKind,
    ThresholdConfig,
)


class Provider(Protocol):
    async def collect(self, assets: list[AssetDefinition]) -> list[Observation]: ...


class UnsafeEndpointError(ValueError):
    pass


Resolver = Callable[[str, int], Awaitable[set[str]]]


async def resolve_public_addresses(host: str, port: int) -> set[str]:
    loop = asyncio.get_running_loop()
    records = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = {str(record[4][0]).split("%", 1)[0] for record in records}
    if not addresses:
        raise UnsafeEndpointError("probe hostname did not resolve")
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as error:
            raise UnsafeEndpointError("probe hostname returned an invalid address") from error
        if not address.is_global:
            raise UnsafeEndpointError("probe hostname resolved to a non-public address")
    return addresses


def _ratio(value: Any, maximum: Any) -> float | None:
    try:
        numerator = float(value)
        denominator = float(maximum)
    except (TypeError, ValueError):
        return None
    return max(0.0, numerator / denominator) if denominator > 0 else None


def _resource_health(
    *,
    status: str,
    cpu: float | None,
    memory: float | None,
    disk: float | None,
    thresholds: ThresholdConfig,
) -> tuple[HealthState, str, str | None, IncidentType | None]:
    if status.lower() not in {"running", "online", "available", "active"}:
        return (
            HealthState.DOWN,
            f"Resource reports {status or 'unavailable'}",
            "critical",
            IncidentType.ASSET_DOWN,
        )
    if memory is not None and memory >= thresholds.memory_critical_ratio:
        return (
            HealthState.DEGRADED,
            f"Memory is {memory:.0%}",
            "critical",
            IncidentType.RESOURCE_PRESSURE,
        )
    if disk is not None and disk >= thresholds.disk_critical_ratio:
        return (
            HealthState.DEGRADED,
            f"Disk is {disk:.0%} full",
            "critical",
            IncidentType.RESOURCE_PRESSURE,
        )
    warnings: list[str] = []
    if cpu is not None and cpu >= thresholds.cpu_warning_ratio:
        warnings.append(f"CPU {cpu:.0%}")
    if memory is not None and memory >= thresholds.memory_warning_ratio:
        warnings.append(f"memory {memory:.0%}")
    if disk is not None and disk >= thresholds.disk_warning_ratio:
        warnings.append(f"disk {disk:.0%}")
    if warnings:
        return (
            HealthState.DEGRADED,
            "Resource pressure: " + ", ".join(warnings),
            "warning",
            IncidentType.RESOURCE_PRESSURE,
        )
    return HealthState.HEALTHY, "Operating within configured thresholds", None, None


def _checks(
    assets: Iterable[AssetDefinition], kinds: set[CheckKind]
) -> list[tuple[AssetDefinition, CheckDefinition]]:
    return [(asset, check) for asset in assets for check in asset.checks if check.type in kinds]


class FixtureProvider:
    def __init__(self, thresholds: ThresholdConfig) -> None:
        self.thresholds = thresholds
        self.tick = 0

    async def collect(self, assets: list[AssetDefinition]) -> list[Observation]:
        self.tick += 1
        now = datetime.now(UTC)
        observations: list[Observation] = []
        for index, (asset, check) in enumerate(_checks(assets, {CheckKind.FIXTURE})):
            wave = ((self.tick + index * 3) % 20) / 1000
            common = {
                "asset_id": asset.id,
                "check_id": check.id,
                "provider": ProviderKind.FIXTURE,
                "observed_at": now,
                "health": HealthState.HEALTHY,
            }
            if asset.kind == AssetKind.NODE:
                observations.append(
                    Observation(
                        **common,
                        message="Host telemetry nominal",
                        cpu_ratio=0.23 + wave,
                        memory_ratio=0.47 + wave,
                        disk_ratio=0.41,
                        details={"source": "fixture"},
                    )
                )
            elif asset.kind == AssetKind.GUEST:
                observations.append(
                    Observation(
                        **common,
                        message="Guest running",
                        cpu_ratio=0.18 + wave,
                        memory_ratio=0.52 + wave,
                        disk_ratio=0.36,
                        details={"source": "fixture"},
                    )
                )
            elif asset.kind == AssetKind.STORAGE:
                observations.append(
                    Observation(
                        **common,
                        message="Storage capacity healthy",
                        disk_ratio=0.58,
                        details={"source": "fixture"},
                    )
                )
            elif "backup" in asset.id:
                observations.append(
                    Observation(
                        **common,
                        message="Latest scheduled backup completed",
                        details={"source": "fixture", "age_hours": 11},
                    )
                )
            else:
                observations.append(
                    Observation(
                        **common,
                        message="HTTPS check passed",
                        latency_ms=24 + index * 3 + wave * 100,
                        details={"source": "fixture", "status_code": 200},
                    )
                )
        return observations


class ProxmoxProvider:
    _MAX_BACKUP_PARENT_LOGS = 16
    _MAX_BACKUP_LOG_CACHE = 64

    def __init__(
        self,
        *,
        base_url: str,
        token_id: str,
        token_secret: str,
        ca_bundle: str,
        thresholds: ThresholdConfig,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"PVEAPIToken={token_id}={token_secret}"}
        self.ca_bundle = ca_bundle
        self.thresholds = thresholds
        self.transport = transport
        self._backup_guest_task_cache: dict[str, list[dict[str, Any]]] = {}

    async def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=self.headers,
            verify=self.ca_bundle,
            timeout=httpx.Timeout(5),
            transport=self.transport,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.get(path, params=params)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or "data" not in payload:
                raise ValueError("Proxmox returned an invalid envelope")
            return payload["data"]

    @staticmethod
    def _reference(item: dict[str, Any]) -> str | None:
        resource_type = str(item.get("type", ""))
        node = item.get("node")
        if resource_type == "node":
            return f"node/{node}"
        if resource_type in {"lxc", "qemu"}:
            return f"guest/{node}/{item.get('vmid')}"
        if resource_type == "storage":
            return f"storage/{node}/{item.get('storage')}"
        return None

    @staticmethod
    def _check_reference(check: CheckDefinition) -> str:
        if check.type == CheckKind.PROXMOX_NODE:
            return f"node/{check.node}"
        if check.type == CheckKind.PROXMOX_GUEST:
            return f"guest/{check.node}/{check.guest_id}"
        if check.type == CheckKind.PROXMOX_STORAGE:
            return f"storage/{check.node}/{check.storage}"
        raise ValueError("backup checks do not use the resource endpoint")

    async def collect(self, assets: list[AssetDefinition]) -> list[Observation]:
        targets = _checks(
            assets,
            {
                CheckKind.PROXMOX_NODE,
                CheckKind.PROXMOX_GUEST,
                CheckKind.PROXMOX_STORAGE,
            },
        )
        now = datetime.now(UTC)
        try:
            payload = await self._get("/api2/json/cluster/resources")
            if not isinstance(payload, list):
                raise ValueError("Proxmox resources payload is not a list")
        except (httpx.HTTPError, ValueError, TypeError):
            return [
                Observation(
                    asset_id=asset.id,
                    check_id=check.id,
                    provider=ProviderKind.PROXMOX,
                    observed_at=now,
                    health=HealthState.UNKNOWN,
                    condition=IncidentType.MONITORING_UNAVAILABLE,
                    message="Proxmox telemetry request failed",
                    details={"source": "proxmox", "severity": "critical"},
                )
                for asset, check in targets
            ]
        by_ref = {
            reference: item
            for item in payload
            if isinstance(item, dict) and (reference := self._reference(item)) is not None
        }
        observations: list[Observation] = []
        for asset, check in targets:
            item = by_ref.get(self._check_reference(check))
            if item is None:
                observations.append(
                    Observation(
                        asset_id=asset.id,
                        check_id=check.id,
                        provider=ProviderKind.PROXMOX,
                        observed_at=now,
                        health=HealthState.UNKNOWN,
                        condition=IncidentType.MONITORING_UNAVAILABLE,
                        message="Configured resource was not returned by Proxmox",
                        details={"source": "proxmox", "severity": "critical"},
                    )
                )
                continue
            try:
                cpu = float(item["cpu"]) if item.get("cpu") is not None else None
                memory = _ratio(item.get("mem"), item.get("maxmem"))
                disk = _ratio(item.get("disk"), item.get("maxdisk"))
                health, message, severity, condition = _resource_health(
                    status=str(item.get("status", "unknown")),
                    cpu=cpu,
                    memory=memory,
                    disk=disk,
                    thresholds=self.thresholds,
                )
                uptime = int(item.get("uptime") or 0)
            except (TypeError, ValueError, OverflowError):
                health = HealthState.UNKNOWN
                message = "Proxmox returned malformed resource telemetry"
                severity = "critical"
                condition = IncidentType.MONITORING_UNAVAILABLE
                cpu = memory = disk = None
                uptime = 0
            observations.append(
                Observation(
                    asset_id=asset.id,
                    check_id=check.id,
                    provider=ProviderKind.PROXMOX,
                    observed_at=now,
                    health=health,
                    condition=condition,
                    message=message,
                    cpu_ratio=cpu,
                    memory_ratio=memory,
                    disk_ratio=disk,
                    details={
                        "source": "proxmox",
                        "severity": severity,
                        "uptime_seconds": uptime,
                    },
                )
            )
        return observations

    @staticmethod
    def _task_matches(task: dict[str, Any], check: CheckDefinition) -> bool:
        node = str(task.get("node") or "")
        if node and node != check.node:
            return False
        if check.guest_id is not None:
            identifiers = " ".join(
                str(task.get(field) or "") for field in ("id", "vmid", "worker_id", "upid")
            )
            if (
                str(check.guest_id) not in identifiers.split(":")
                and str(check.guest_id) not in identifiers.split()
            ):
                return False
        if check.backup_job_id:
            job_identifiers = {
                str(task.get(field) or "") for field in ("job-id", "job_id", "worker_id", "id")
            }
            if check.backup_job_id not in job_identifiers:
                return False
        return True

    @staticmethod
    def _task_time(task: dict[str, Any]) -> int:
        try:
            return int(task.get("endtime") or task.get("starttime") or 0)
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _guest_task_from_log(
        cls,
        task: dict[str, Any],
        check: CheckDefinition,
        log: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if check.guest_id is None:
            return None
        guest_id = check.guest_id
        messages = [str(item.get("t") or "") for item in log]
        started = any(f"Starting Backup of VM {guest_id} (" in message for message in messages)
        finished = any(f"Finished Backup of VM {guest_id} (" in message for message in messages)
        failed = any(f"Backup of VM {guest_id} failed" in message for message in messages)
        if not (started or finished or failed):
            return None
        parent_status = str(task.get("status") or "").upper()
        if finished:
            status = "OK"
        elif failed or task.get("endtime") is not None:
            status = "ERROR"
        else:
            status = parent_status or "RUNNING"
        return {
            "node": task.get("node"),
            "id": str(guest_id),
            "vmid": guest_id,
            "status": status,
            "starttime": task.get("starttime"),
            "endtime": task.get("endtime"),
            "upid": task.get("upid"),
        }

    async def _guest_tasks_from_parent_logs(
        self,
        node: str,
        tasks: list[dict[str, Any]],
        checks: list[CheckDefinition],
    ) -> list[dict[str, Any]]:
        guest_checks = [check for check in checks if check.guest_id is not None]
        if not guest_checks:
            return []
        parent_tasks = [
            task for task in tasks if not str(task.get("id") or "") and task.get("upid")
        ]
        parent_tasks.sort(key=self._task_time, reverse=True)
        expanded: list[dict[str, Any]] = []
        for task in parent_tasks[: self._MAX_BACKUP_PARENT_LOGS]:
            raw_upid = str(task["upid"])
            cache_key = f"{node}:{raw_upid}"
            if cache_key in self._backup_guest_task_cache:
                expanded.extend(self._backup_guest_task_cache[cache_key])
                continue
            upid = quote(raw_upid, safe="")
            try:
                payload = await self._get(
                    f"/api2/json/nodes/{node}/tasks/{upid}/log",
                    params={"start": "0", "limit": "1000"},
                )
                if not isinstance(payload, list) or any(
                    not isinstance(item, dict) for item in payload
                ):
                    continue
            except (httpx.HTTPError, ValueError, TypeError):
                continue
            log = cast(list[dict[str, Any]], payload)
            derived: list[dict[str, Any]] = []
            for check in guest_checks:
                if synthetic := self._guest_task_from_log(task, check, log):
                    derived.append(synthetic)
            expanded.extend(derived)
            if task.get("endtime") is not None:
                if len(self._backup_guest_task_cache) >= self._MAX_BACKUP_LOG_CACHE:
                    self._backup_guest_task_cache.pop(next(iter(self._backup_guest_task_cache)))
                self._backup_guest_task_cache[cache_key] = derived
        return expanded

    async def collect_backups(self, assets: list[AssetDefinition]) -> list[Observation]:
        targets = _checks(assets, {CheckKind.PROXMOX_BACKUP})
        now = datetime.now(UTC)
        tasks_by_node: dict[str, list[dict[str, Any]] | None] = {}
        for node in sorted({cast(str, check.node) for _, check in targets}):
            try:
                payload = await self._get(
                    f"/api2/json/nodes/{node}/tasks",
                    params={"typefilter": "vzdump", "limit": "500"},
                )
                if not isinstance(payload, list) or any(
                    not isinstance(item, dict) for item in payload
                ):
                    raise ValueError("invalid backup task payload")
                node_tasks = cast(list[dict[str, Any]], payload)
                checks = [check for _, check in targets if check.node == node]
                tasks_by_node[node] = [
                    *node_tasks,
                    *(await self._guest_tasks_from_parent_logs(node, node_tasks, checks)),
                ]
            except (httpx.HTTPError, ValueError, TypeError):
                tasks_by_node[node] = None

        observations: list[Observation] = []
        for asset, check in targets:
            node_tasks_or_none = tasks_by_node[cast(str, check.node)]
            if node_tasks_or_none is None:
                observations.append(
                    Observation(
                        asset_id=asset.id,
                        check_id=check.id,
                        provider=ProviderKind.BACKUP,
                        observed_at=now,
                        health=HealthState.UNKNOWN,
                        condition=IncidentType.MONITORING_UNAVAILABLE,
                        message="Backup task history could not be read",
                        details={"source": "proxmox-backup", "severity": "critical"},
                    )
                )
                continue
            matches = [task for task in node_tasks_or_none if self._task_matches(task, check)]

            latest_attempt = max(matches, key=self._task_time, default=None)
            successes = [task for task in matches if str(task.get("status", "")).upper() == "OK"]
            latest_success = max(successes, key=self._task_time, default=None)
            if latest_attempt is None:
                observations.append(
                    Observation(
                        asset_id=asset.id,
                        check_id=check.id,
                        provider=ProviderKind.BACKUP,
                        observed_at=now,
                        health=HealthState.DOWN,
                        condition=IncidentType.BACKUP_STALE,
                        message="No matching backup attempt was returned",
                        details={"source": "proxmox-backup", "severity": "critical"},
                    )
                )
                continue
            attempt_status = str(latest_attempt.get("status") or "unknown")
            if attempt_status.upper() == "RUNNING":
                observations.append(
                    Observation(
                        asset_id=asset.id,
                        check_id=check.id,
                        provider=ProviderKind.BACKUP,
                        observed_at=now,
                        health=HealthState.UNKNOWN,
                        message="Matching backup attempt is still in progress",
                        details={
                            "source": "proxmox-backup",
                            "severity": None,
                            "latest_attempt_at": self._task_time(latest_attempt),
                            "latest_success_at": self._task_time(latest_success)
                            if latest_success
                            else None,
                        },
                    )
                )
                continue
            if attempt_status.upper() != "OK":
                observations.append(
                    Observation(
                        asset_id=asset.id,
                        check_id=check.id,
                        provider=ProviderKind.BACKUP,
                        observed_at=now,
                        health=HealthState.DOWN,
                        condition=IncidentType.BACKUP_FAILED,
                        message=f"Latest matching backup attempt reported {attempt_status[:40]}",
                        details={
                            "source": "proxmox-backup",
                            "severity": "critical",
                            "latest_attempt_at": self._task_time(latest_attempt),
                            "latest_success_at": self._task_time(latest_success)
                            if latest_success
                            else None,
                        },
                    )
                )
                continue
            finished_at = datetime.fromtimestamp(self._task_time(latest_attempt), tz=UTC)
            age_hours = max(0.0, (now - finished_at).total_seconds() / 3600)
            if age_hours >= self.thresholds.backup_critical_hours:
                health, severity = HealthState.DOWN, "critical"
            elif age_hours >= self.thresholds.backup_warning_hours:
                health, severity = HealthState.DEGRADED, "warning"
            else:
                health, severity = HealthState.HEALTHY, None
            observations.append(
                Observation(
                    asset_id=asset.id,
                    check_id=check.id,
                    provider=ProviderKind.BACKUP,
                    observed_at=now,
                    health=health,
                    condition=IncidentType.BACKUP_STALE if severity else None,
                    message=f"Latest matching backup succeeded {age_hours:.1f} hours ago",
                    details={
                        "source": "proxmox-backup",
                        "severity": severity,
                        "age_hours": round(age_hours, 1),
                        "latest_attempt_at": self._task_time(latest_attempt),
                        "latest_success_at": self._task_time(latest_success)
                        if latest_success
                        else None,
                    },
                )
            )
        return observations


class HttpProvider:
    def __init__(
        self,
        policy: ProbePolicy,
        *,
        resolver: Resolver = resolve_public_addresses,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.policy = policy
        self.resolver = resolver
        self.transport = transport
        self._semaphore = asyncio.Semaphore(policy.max_concurrency)

    async def collect(self, assets: list[AssetDefinition]) -> list[Observation]:
        targets = _checks(assets, {CheckKind.HTTPS})
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(5),
            follow_redirects=False,
            headers={"User-Agent": "Signal-Room/1.0 health-check"},
            transport=self.transport,
            trust_env=False,
            limits=httpx.Limits(max_connections=self.policy.max_concurrency),
        ) as client:
            return await asyncio.gather(
                *(self._bounded_check(client, asset, check) for asset, check in targets)
            )

    async def _bounded_check(
        self, client: httpx.AsyncClient, asset: AssetDefinition, check: CheckDefinition
    ) -> Observation:
        async with self._semaphore:
            return await self._check(client, asset, check)

    async def _pinned_request(
        self, client: httpx.AsyncClient, url: str, check: CheckDefinition
    ) -> tuple[int, str | None]:
        parsed = urlsplit(url)
        host = parsed.hostname
        if host is None or parsed.scheme != "https":
            raise UnsafeEndpointError("probe URL is not a valid HTTPS URL")
        if host.lower().rstrip(".") not in {
            value.lower().rstrip(".") for value in self.policy.allowed_hosts
        }:
            raise UnsafeEndpointError("probe host is not allowlisted")
        port = parsed.port or 443
        if port not in check.allowed_ports:
            raise UnsafeEndpointError("probe port is not allowed")
        addresses = await self.resolver(host, port)
        address = sorted(addresses)[0]
        netloc = f"[{address}]" if ":" in address else address
        if port != 443:
            netloc += f":{port}"
        pinned_url = urlunsplit((parsed.scheme, netloc, parsed.path or "/", parsed.query, ""))
        host_header = host if port == 443 else f"{host}:{port}"
        async with client.stream(
            "GET",
            pinned_url,
            headers={"Host": host_header},
            extensions={"sni_hostname": host},
        ) as response:
            content_length = response.headers.get("content-length")
            if content_length:
                try:
                    declared_length = int(content_length)
                except ValueError:
                    pass
                else:
                    if declared_length > self.policy.max_response_bytes:
                        raise UnsafeEndpointError("health response exceeded the size limit")
            read = 0
            async for chunk in response.aiter_bytes():
                read += len(chunk)
                if read > self.policy.max_response_bytes:
                    raise UnsafeEndpointError("health response exceeded the size limit")
            return response.status_code, response.headers.get("location")

    async def _check(
        self, client: httpx.AsyncClient, asset: AssetDefinition, check: CheckDefinition
    ) -> Observation:
        now = datetime.now(UTC)
        started = time.perf_counter()
        try:
            if check.url is None:
                raise UnsafeEndpointError("HTTPS check has no configured URL")
            original_url = str(check.url)
            status, location = await self._pinned_request(client, original_url, check)
            if 300 <= status < 400 and location and check.allow_same_host_redirects:
                redirected = urljoin(original_url, location)
                original = urlsplit(original_url)
                target = urlsplit(redirected)
                if target.scheme != "https" or target.hostname != original.hostname:
                    raise UnsafeEndpointError("redirect target was not same-host HTTPS")
                status, _ = await self._pinned_request(client, redirected, check)
            status_ok = status in check.expected_statuses
            latency = (time.perf_counter() - started) * 1000
            return Observation(
                asset_id=asset.id,
                check_id=check.id,
                provider=ProviderKind.HTTPS,
                observed_at=now,
                health=HealthState.HEALTHY if status_ok else HealthState.DOWN,
                condition=None if status_ok else IncidentType.HTTP_FAILED,
                message=(
                    f"{check.mode.title()} check returned {status}"
                    if status_ok
                    else "HTTPS check returned an unexpected status"
                ),
                latency_ms=round(latency, 1),
                details={
                    "source": "https",
                    "status_code": status,
                    "check_mode": check.mode,
                    "severity": None if status_ok else "critical",
                },
            )
        except (httpx.HTTPError, OSError, UnsafeEndpointError, ValueError):
            return Observation(
                asset_id=asset.id,
                check_id=check.id,
                provider=ProviderKind.HTTPS,
                observed_at=now,
                health=HealthState.DOWN,
                condition=IncidentType.HTTP_FAILED,
                message="HTTPS check could not safely reach the configured endpoint",
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                details={"source": "https", "severity": "critical"},
            )


class TlsProvider:
    def __init__(
        self,
        thresholds: ThresholdConfig,
        policy: ProbePolicy,
        *,
        resolver: Resolver = resolve_public_addresses,
    ) -> None:
        self.thresholds = thresholds
        self.policy = policy
        self.resolver = resolver
        self._semaphore = asyncio.Semaphore(policy.max_concurrency)

    async def collect(self, assets: list[AssetDefinition]) -> list[Observation]:
        return await asyncio.gather(
            *(
                self._bounded_check(asset, check)
                for asset, check in _checks(assets, {CheckKind.TLS})
            )
        )

    async def _bounded_check(self, asset: AssetDefinition, check: CheckDefinition) -> Observation:
        async with self._semaphore:
            return await self._check(asset, check)

    async def _check(self, asset: AssetDefinition, check: CheckDefinition) -> Observation:
        now = datetime.now(UTC)
        try:
            if check.url is None:
                raise UnsafeEndpointError("TLS check has no configured URL")
            parsed = urlsplit(str(check.url))
            host = parsed.hostname
            if host is None:
                raise UnsafeEndpointError("TLS check URL has no hostname")
            port = parsed.port or 443
            if host.lower().rstrip(".") not in {
                value.lower().rstrip(".") for value in self.policy.allowed_hosts
            }:
                raise UnsafeEndpointError("TLS host is not allowlisted")
            if port not in check.allowed_ports:
                raise UnsafeEndpointError("TLS port is not allowed")
            addresses = await self.resolver(host, port)
            address = sorted(addresses)[0]
            context = ssl.create_default_context()
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(address, port, ssl=context, server_hostname=host), timeout=5
            )
            ssl_object = writer.get_extra_info("ssl_object")
            certificate = ssl_object.getpeercert() if ssl_object else {}
            writer.close()
            await writer.wait_closed()
            expires_raw = certificate.get("notAfter")
            if not expires_raw:
                raise ValueError("peer certificate did not include an expiry")
            expires_at = datetime.fromtimestamp(ssl.cert_time_to_seconds(expires_raw), tz=UTC)
            days = (expires_at - now).total_seconds() / 86_400
            if days <= self.thresholds.certificate_critical_days:
                health, severity = HealthState.DOWN, "critical"
            elif days <= self.thresholds.certificate_warning_days:
                health, severity = HealthState.DEGRADED, "warning"
            else:
                health, severity = HealthState.HEALTHY, None
            return Observation(
                asset_id=asset.id,
                check_id=check.id,
                provider=ProviderKind.TLS,
                observed_at=now,
                health=health,
                condition=IncidentType.CERTIFICATE_EXPIRING if severity else None,
                message=f"Certificate has {days:.0f} days remaining",
                details={
                    "source": "tls",
                    "severity": severity,
                    "certificate_days_remaining": round(days, 1),
                },
            )
        except (TimeoutError, OSError, ssl.SSLError, ValueError, UnsafeEndpointError):
            return Observation(
                asset_id=asset.id,
                check_id=check.id,
                provider=ProviderKind.TLS,
                observed_at=now,
                health=HealthState.DOWN,
                condition=IncidentType.CERTIFICATE_EXPIRING,
                message="TLS certificate check failed safely",
                details={"source": "tls", "severity": "critical"},
            )


def assets_for_provider(
    assets: Iterable[AssetDefinition], providers: set[ProviderKind | str]
) -> list[AssetDefinition]:
    values = {str(provider) for provider in providers}
    return [
        asset for asset in assets if any(str(check.provider) in values for check in asset.checks)
    ]
