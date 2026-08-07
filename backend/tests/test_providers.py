from __future__ import annotations

import json

import certifi
import httpx
from signal_room.models import CheckDefinition, CheckKind, TopologyConfig
from signal_room.providers import ProxmoxProvider


async def test_proxmox_provider_uses_only_get_and_normalizes_resources(
    topology: TopologyConfig,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = {
            "data": [
                {
                    "type": "node",
                    "node": "atlas",
                    "status": "online",
                    "cpu": 0.25,
                    "mem": 50,
                    "maxmem": 100,
                    "disk": 40,
                    "maxdisk": 100,
                    "uptime": 600,
                },
                {
                    "type": "lxc",
                    "node": "atlas",
                    "vmid": 104,
                    "status": "running",
                    "cpu": 0.10,
                    "mem": 60,
                    "maxmem": 100,
                    "disk": 20,
                    "maxdisk": 100,
                },
            ]
        }
        return httpx.Response(200, content=json.dumps(payload), request=request)

    provider = ProxmoxProvider(
        base_url="https://proxmox.example.invalid:8006",
        token_id="monitor@example.invalid!signal-room",
        token_secret="test-secret",  # pragma: allowlist secret
        ca_bundle=certifi.where(),
        thresholds=topology.thresholds,
        transport=httpx.MockTransport(handler),
    )
    assets = [
        topology.assets[0].model_copy(
            update={
                "checks": [
                    CheckDefinition(id="node-check", type=CheckKind.PROXMOX_NODE, node="atlas")
                ]
            }
        ),
        topology.assets[1].model_copy(
            update={
                "checks": [
                    CheckDefinition(
                        id="guest-check", type=CheckKind.PROXMOX_GUEST, node="atlas", guest_id=104
                    )
                ]
            }
        ),
    ]
    observations = await provider.collect(assets)

    assert [request.method for request in requests] == ["GET"]
    assert requests[0].headers["Authorization"].startswith("PVEAPIToken=")
    assert all(observation.health == "healthy" for observation in observations)
    assert observations[1].memory_ratio == 0.6


async def test_proxmox_failure_returns_generic_unknown_state(topology: TopologyConfig) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    provider = ProxmoxProvider(
        base_url="https://proxmox.example.invalid:8006",
        token_id="monitor@example.invalid!signal-room",
        token_secret="must-not-appear",  # pragma: allowlist secret
        ca_bundle=certifi.where(),
        thresholds=topology.thresholds,
        transport=httpx.MockTransport(handler),
    )
    asset = topology.assets[0].model_copy(
        update={
            "checks": [CheckDefinition(id="node-check", type=CheckKind.PROXMOX_NODE, node="atlas")]
        }
    )
    observation = (await provider.collect([asset]))[0]
    assert observation.health == "unknown"
    assert "must-not-appear" not in observation.message
    assert "proxmox.example.invalid" not in observation.message
