#!/usr/bin/env python3
"""Exercise the API/SSE and enforce the flagship latency/resource ceilings."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random

# This module invokes fixed absolute systemctl argv and never a shell.
import subprocess  # nosec B404
import sys
import time
from pathlib import Path
from typing import Any

import httpx

UNITS = (
    "signal-room-core.service",
    "signal-room-collector.service",
    "signal-room-web.service",
    "signal-room-notifier.service",
    "cloudflared.service",
)


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return math.inf
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)]


def service_pids() -> list[int]:
    pids: list[int] = []
    for unit in UNITS:
        result = subprocess.run(  # noqa: S603  # nosec B603
            ["/usr/bin/systemctl", "show", "--property=MainPID", "--value", unit],
            check=False,
            capture_output=True,
            text=True,
        )
        value = result.stdout.strip()
        if value.isdigit() and int(value) > 0:
            pids.append(int(value))
    return pids


def memory_kib(pids: list[int]) -> tuple[int, int]:
    rss = 0
    swap = 0
    for pid in pids:
        try:
            status = (Path("/proc") / str(pid) / "status").read_text(encoding="utf-8")
        except OSError:
            continue
        process_rss, process_swap = parse_memory_status(status)
        rss += process_rss
        swap += process_swap
    return rss, swap


def parse_memory_status(status: str) -> tuple[int, int]:
    values = {"VmRSS": 0, "VmSwap": 0}
    for line in status.splitlines():
        name, separator, raw = line.partition(":")
        if not separator or name not in values:
            continue
        fields = raw.split()
        if not fields or not fields[0].isdigit():
            raise RuntimeError(f"malformed {name} field in process status")
        values[name] = int(fields[0])
    return values["VmRSS"], values["VmSwap"]


async def collect_incidents(client: httpx.AsyncClient, minimum: int) -> int:
    count = 0
    cursor: str | None = None
    while count < minimum:
        response = await client.get(
            "/api/v1/incidents",
            params={"limit": 100, **({"cursor": cursor} if cursor else {})},
        )
        response.raise_for_status()
        payload = response.json()
        count += len(payload["items"])
        cursor = payload.get("next_cursor")
        if not cursor:
            break
    return count


async def sse_client(
    client: httpx.AsyncClient,
    ready: asyncio.Event,
    release: asyncio.Event,
) -> None:
    async with client.stream("GET", "/api/v1/stream", timeout=None) as response:
        response.raise_for_status()
        ready.set()
        async for line in response.aiter_lines():
            if release.is_set():
                return
            if line.startswith("data:"):
                break
        await release.wait()


async def run_gate(arguments: argparse.Namespace) -> dict[str, Any]:
    headers = {}
    token = os.getenv("CF_ACCESS_JWT", "")
    if token:
        headers["Cf-Access-Jwt-Assertion"] = token
    timeout = httpx.Timeout(10, connect=5)
    async with httpx.AsyncClient(
        base_url=arguments.base_url,
        headers=headers,
        timeout=timeout,
        trust_env=False,
        follow_redirects=False,
    ) as client:
        bootstrap_response = await client.get("/api/v1/bootstrap")
        bootstrap_response.raise_for_status()
        bootstrap = bootstrap_response.json()
        assets = [item["id"] for item in bootstrap["assets"]]
        if len(assets) < arguments.min_assets:
            raise RuntimeError(f"fixture has {len(assets)} assets; need {arguments.min_assets}")
        incident_count = await collect_incidents(client, arguments.min_incidents)
        if incident_count < arguments.min_incidents:
            raise RuntimeError(
                f"fixture has {incident_count} incidents; need {arguments.min_incidents}"
            )

        metrics = await client.get(
            f"/api/v1/assets/{assets[0]}/metrics",
            params={"range": "7d", "resolution": "1h"},
        )
        metrics.raise_for_status()
        if not metrics.json()["buckets"]:
            raise RuntimeError("seven-day metric fixture is empty")

        ready = [asyncio.Event() for _ in range(arguments.sse_clients)]
        release = asyncio.Event()
        streams = [asyncio.create_task(sse_client(client, event, release)) for event in ready]
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in ready)), timeout=arguments.sse_timeout
        )

        latencies: list[float] = []
        failures: list[str] = []
        stop_at = time.monotonic() + arguments.seconds

        async def worker(identifier: int) -> None:
            rng = random.Random(identifier)  # noqa: S311  # nosec B311
            routes = (
                lambda: "/api/v1/bootstrap",
                lambda: "/api/v1/diagnostics",
                lambda: "/api/v1/incidents?limit=100",
                lambda: f"/api/v1/assets/{rng.choice(assets)}",
                lambda: f"/api/v1/assets/{rng.choice(assets)}/metrics?range=7d&resolution=1h",
            )
            while time.monotonic() < stop_at:
                route = rng.choice(routes)()
                started = time.perf_counter()
                try:
                    response = await client.get(route)
                    response.raise_for_status()
                except (httpx.HTTPError, ValueError) as error:
                    failures.append(f"{route}: {type(error).__name__}")
                else:
                    latencies.append((time.perf_counter() - started) * 1000)

        pids = service_pids()
        start_rss, start_swap = memory_kib(pids)
        try:
            await asyncio.gather(*(worker(index) for index in range(arguments.workers)))
        finally:
            release.set()
            await asyncio.gather(*streams, return_exceptions=True)
        end_rss, end_swap = memory_kib(pids)

    p95 = percentile(latencies, 0.95)
    max_rss = max(start_rss, end_rss)
    result = {
        "requests": len(latencies),
        "failures": len(failures),
        "failure_examples": failures[:10],
        "p95_ms": round(p95, 2),
        "rss_mib": round(max_rss / 1024, 2),
        "swap_growth_mib": round(max(0, end_swap - start_swap) / 1024, 2),
        "assets": len(assets),
        "incidents_checked": incident_count,
        "sse_clients": arguments.sse_clients,
    }
    if failures:
        raise RuntimeError(json.dumps(result, sort_keys=True))
    if p95 >= arguments.max_p95_ms:
        raise RuntimeError(json.dumps(result, sort_keys=True))
    if max_rss > arguments.max_rss_mib * 1024:
        raise RuntimeError(json.dumps(result, sort_keys=True))
    if end_swap > start_swap:
        raise RuntimeError(json.dumps(result, sort_keys=True))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--seconds", type=int, default=300)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--sse-clients", type=int, default=8)
    parser.add_argument("--sse-timeout", type=int, default=20)
    parser.add_argument("--min-assets", type=int, default=50)
    parser.add_argument("--min-incidents", type=int, default=365)
    parser.add_argument("--max-p95-ms", type=float, default=500)
    parser.add_argument("--max-rss-mib", type=int, default=650)
    arguments = parser.parse_args()
    try:
        result = asyncio.run(run_gate(arguments))
    except (RuntimeError, httpx.HTTPError, TimeoutError) as error:
        print(f"load gate failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
