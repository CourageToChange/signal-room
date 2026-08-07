from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient
from signal_room.api import EventBroadcaster, create_app
from signal_room.auth import AccessTokenVerifier, Identity
from signal_room.config import AppSettings
from signal_room.core import CoreRequestError, CoreUnavailableError
from signal_room.models import IncidentSummary

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 15, 18, 0, tzinfo=UTC)


def _demo_snapshot() -> dict[str, Any]:
    payload = json.loads(
        (ROOT / "frontend/src/demo/generated/pressure-drop.json").read_text(encoding="utf-8")
    )
    return payload["frames"][5]["snapshot"]


def _stream_event(identifier: int = 1) -> dict[str, Any]:
    return {
        "id": identifier,
        "event_uuid": f"event-{identifier}",
        "created_at": NOW.isoformat(),
        "topic": "incident",
        "kind": "incident.opened",
        "subject_id": "incident-1",
        "payload": {"state": "open"},
    }


class FixtureCore:
    def __init__(self) -> None:
        self.snapshot = _demo_snapshot()
        raw_incident = self.snapshot["incidents"][0]
        self.incident = {
            **raw_incident,
            "events": [
                {
                    "id": 1,
                    "event_uuid": "event-1",
                    "incident_id": self.snapshot["incidents"][0]["id"],
                    "created_at": NOW.isoformat(),
                    "kind": "incident.opened",
                    "message": "Incident opened",
                    "metadata": {},
                }
            ],
            "notes": [],
            "runbook": None,
        }
        self.snapshot["incidents"] = [
            {
                key: value
                for key, value in raw_incident.items()
                if key in IncidentSummary.model_fields
            }
        ]
        self.maintenance = {
            "id": "maintenance-1",
            "asset_ids": [self.snapshot["assets"][0]["id"]],
            "starts_at": (NOW + timedelta(hours=1)).isoformat(),
            "ends_at": (NOW + timedelta(hours=2)).isoformat(),
            "reason": "Planned test",
            "created_at": NOW.isoformat(),
            "created_by": "owner@example.test",
            "cancelled_at": None,
            "cancelled_by": None,
            "version": 1,
        }
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.errors: dict[str, Exception] = {}
        self.events: list[dict[str, Any]] = []

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        values = params or {}
        self.calls.append((method, values))
        if method in self.errors:
            raise self.errors[method]
        if method == "bootstrap":
            return self.snapshot
        if method == "readiness":
            return {"ok": True, "database": "ready"}
        if method == "stream_events":
            return [item for item in self.events if item["id"] > values.get("after", 0)]
        if method == "asset_detail":
            return {
                "asset": self.snapshot["assets"][0],
                "state": self.snapshot["states"][0],
                "active_incidents": [self.snapshot["incidents"][0]],
            }
        if method == "metrics":
            return {
                "asset_id": values["asset_id"],
                "range": values["range"],
                "resolution": "5m" if values["resolution"] == "auto" else values["resolution"],
                "generated_at": NOW.isoformat(),
                "completeness": 1.0,
                "thresholds": {
                    "cpu_warning_ratio": 0.8,
                    "memory_warning_ratio": 0.75,
                    "memory_critical_ratio": 0.9,
                    "disk_warning_ratio": 0.8,
                    "disk_critical_ratio": 0.9,
                },
                "buckets": [],
            }
        if method == "incident_page":
            return {"items": [self.snapshot["incidents"][0]], "next_cursor": "next"}
        if method in {"incident", "acknowledge", "note", "close"}:
            return self.incident
        if method == "timeline":
            return {"items": self.incident["events"], "next_cursor": None}
        if method == "maintenance_list":
            return [self.maintenance]
        if method == "maintenance_create":
            return self.maintenance
        if method == "maintenance_cancel":
            return {
                **self.maintenance,
                "cancelled_at": NOW.isoformat(),
                "cancelled_by": "owner@example.test",
                "version": 2,
            }
        if method == "diagnostics":
            return {
                "request_id": values["request_id"],
                "build_version": "1.0.0",
                "build_sha": "fixture",
                "schema_version": 4,
                "configuration_revision": "test-v2",
                "database_ok": True,
                "collector_fresh": True,
                "providers": [],
                "notifications": {"enabled": False},
            }
        raise AssertionError(f"unexpected core method {method}")


def mutation_headers(**extra: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Origin": "http://testserver",
        "X-Signal-Room-CSRF": "1",
        "If-Match": 'W/"1"',
        "Idempotency-Key": "request-12345678",
        **extra,
    }


def test_health_and_bootstrap_contract(settings: AppSettings) -> None:
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/health/live").json() == {"ok": True}
        ready = client.get("/api/health/ready")
        assert ready.status_code == 503
        bootstrap = client.get("/api/v1/bootstrap")
        assert bootstrap.status_code == 200
        payload = bootstrap.json()
        assert len(payload["assets"]) == 7
        assert payload["capabilities"]["data_source"] == "fixture"
        assert bootstrap.headers["content-security-policy"].startswith("default-src 'self'")
        assert bootstrap.headers["cache-control"] == "no-store"


def test_all_v1_query_and_mutation_routes(settings: AppSettings) -> None:
    core = FixtureCore()
    app = create_app(settings, core=core)
    asset_id = core.snapshot["assets"][0]["id"]
    incident_id = core.incident["id"]
    with TestClient(app) as client:
        assert client.get("/api/health/ready").status_code == 200
        assert client.get("/api/v1/bootstrap").json()["incidents"]
        assert client.get(f"/api/v1/assets/{asset_id}").status_code == 200
        metrics = client.get(f"/api/v1/assets/{asset_id}/metrics?range=24h&resolution=auto").json()
        assert metrics["resolution"] == "5m"
        page = client.get("/api/v1/incidents?state=open&cursor=old&limit=10").json()
        assert page["next_cursor"] == "next"
        assert client.get(f"/api/v1/incidents/{incident_id}").status_code == 200
        assert client.get(f"/api/v1/incidents/{incident_id}/timeline?limit=10").json()["items"]
        assert client.get("/api/v1/maintenance?include_expired=true").json()
        diagnostic = client.get("/api/v1/diagnostics", headers={"X-Request-ID": "request.good-123"})
        assert diagnostic.json()["request_id"] == "request.good-123"

        acknowledged = client.post(
            f"/api/v1/incidents/{incident_id}/acknowledge", json={}, headers=mutation_headers()
        )
        assert acknowledged.status_code == 200
        noted = client.post(
            f"/api/v1/incidents/{incident_id}/notes",
            json={"body": "Checked the dependency path"},
            headers=mutation_headers(**{"Idempotency-Key": "note-request-123"}),
        )
        assert noted.status_code == 200
        closed = client.post(
            f"/api/v1/incidents/{incident_id}/close",
            json={},
            headers=mutation_headers(**{"Idempotency-Key": "close-request-123"}),
        )
        assert closed.status_code == 200
        created = client.post(
            "/api/v1/maintenance",
            json={
                "asset_ids": [asset_id],
                "starts_at": (NOW + timedelta(hours=1)).isoformat(),
                "ends_at": (NOW + timedelta(hours=2)).isoformat(),
                "reason": "Upgrade",
            },
            headers=mutation_headers(**{"Idempotency-Key": "maint-request-123"}),
        )
        assert created.status_code == 200
        cancelled = client.post(
            "/api/v1/maintenance/maintenance-1/cancel",
            json={},
            headers=mutation_headers(**{"Idempotency-Key": "cancel-request-123"}),
        )
        assert cancelled.json()["maintenance"]["version"] == 2

    page_call = next(params for method, params in core.calls if method == "incident_page")
    assert page_call == {"states": ["open"], "cursor": "old", "limit": 10}
    note_call = next(params for method, params in core.calls if method == "note")
    assert note_call["actor_subject"] == "local-development"
    assert note_call["version"] == 1


def test_mutation_security_boundary_and_problem_contract(settings: AppSettings) -> None:
    core = FixtureCore()
    app = create_app(settings, core=core)
    path = f"/api/v1/incidents/{core.incident['id']}/acknowledge"
    with TestClient(app) as client:
        assert client.post(path, content="{}").status_code == 415
        assert client.post(path, json={}).status_code == 403
        assert (
            client.post(path, json={}, headers={"Origin": "http://testserver"}).status_code == 403
        )

        bad_version = client.post(path, json={}, headers=mutation_headers(**{"If-Match": "nope"}))
        assert bad_version.status_code == 428
        assert bad_version.headers["content-type"].startswith("application/problem+json")
        assert bad_version.headers["x-frame-options"] == "DENY"
        assert bad_version.json()["request_id"] == bad_version.headers["x-request-id"]
        assert (
            client.post(
                path,
                json={},
                headers=mutation_headers(**{"If-Match": "0"}),
            ).status_code
            == 428
        )
        assert (
            client.post(
                path,
                json={},
                headers=mutation_headers(**{"Idempotency-Key": "short"}),
            ).status_code
            == 422
        )
        assert (
            client.post(
                f"/api/v1/incidents/{core.incident['id']}/notes",
                json={"body": ""},
                headers=mutation_headers(),
            ).status_code
            == 422
        )


def test_host_body_request_id_rate_and_static_cache_boundaries(settings: AppSettings) -> None:
    core = FixtureCore()
    limited = settings.model_copy(update={"mutation_limit_per_minute": 1})
    app = create_app(limited, core=core)
    path = f"/api/v1/incidents/{core.incident['id']}/acknowledge"
    with TestClient(app) as client:
        invalid_host = client.get("/api/health/live", headers={"Host": "evil.example"})
        assert invalid_host.status_code == 400
        assert invalid_host.headers["x-content-type-options"] == "nosniff"
        oversized = client.post(
            path,
            content=b"x" * (settings.request_body_limit_bytes + 1),
            headers=mutation_headers(),
        )
        assert oversized.status_code == 413
        invalid_length = client.get("/api/health/live", headers={"Content-Length": "invalid"})
        assert invalid_length.status_code == 413
        invalid_request_id = client.get("/api/health/live", headers={"X-Request-ID": "bad id"})
        assert invalid_request_id.headers["x-request-id"] != "bad id"
        assert client.post(path, json={}, headers=mutation_headers()).status_code == 200
        limited_response = client.post(
            path,
            json={},
            headers=mutation_headers(**{"Idempotency-Key": "another-request"}),
        )
        assert limited_response.status_code == 429
        assert limited_response.headers["retry-after"] == "60"
        assert client.get("/missing-screen").status_code == 200
        assert settings.static_dir is not None
        asset_name = next((settings.static_dir / "assets").iterdir()).name
        asset = client.get(f"/assets/{asset_name}")
        assert asset.status_code == 200
        assert asset.headers["cache-control"].endswith("immutable")


async def test_chunked_body_is_bounded_without_content_length(settings: AppSettings) -> None:
    core = FixtureCore()

    async def chunks() -> AsyncIterator[bytes]:
        yield b'{"body":"'
        yield b"x" * settings.request_body_limit_bytes
        yield b'"}'

    transport = httpx.ASGITransport(app=create_app(settings, core=core))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            f"/api/v1/incidents/{core.incident['id']}/notes",
            content=chunks(),
            headers=mutation_headers(),
        )
    assert response.status_code == 413
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    ("error", "status", "contains_detail"),
    [
        (CoreRequestError("not_found", "missing"), 404, True),
        (CoreRequestError("conflict", "changed"), 409, True),
        (CoreRequestError("idempotency_conflict", "reused"), 409, True),
        (CoreRequestError("invalid_parameter", "bad"), 422, True),
        (CoreRequestError("invalid_idempotency_key", "bad key"), 422, True),
        (CoreRequestError("unexpected", "private detail"), 500, False),
        (CoreUnavailableError("socket missing"), 503, False),
    ],
)
def test_core_failures_are_mapped_and_redacted(
    settings: AppSettings, error: Exception, status: int, contains_detail: bool
) -> None:
    core = FixtureCore()
    core.errors["asset_detail"] = error
    with TestClient(create_app(settings, core=core)) as client:
        response = client.get("/api/v1/assets/atlas-node")
    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/problem+json")
    assert (str(error) in response.json()["detail"]) is contains_detail


def test_validation_method_and_explicit_api_404_errors(settings: AppSettings) -> None:
    core = FixtureCore()
    with TestClient(create_app(settings, core=core)) as client:
        assert client.get("/api/v1/assets/a/metrics?range=forever").status_code == 422
        method = client.post("/missing-screen", json={}, headers=mutation_headers())
        assert method.status_code == 405
        missing = client.get("/api/v1/not-a-route")
        assert missing.status_code == 404
        assert missing.json()["detail"] == "API endpoint does not exist"
        assert client.get("/api/v1/incidents?limit=0").status_code == 422


def test_access_authentication_is_required_except_for_health(
    settings: AppSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def verify(_: AccessTokenVerifier, token: str) -> Identity:
        if token != "valid-token":
            from signal_room.auth import AuthenticationError

            raise AuthenticationError("rejected")
        return Identity(subject="access-user", email="owner@example.test")

    monkeypatch.setattr(AccessTokenVerifier, "verify", verify)
    access = settings.model_copy(
        update={
            "auth_mode": "access",
            "access_team_domain": "https://team.cloudflareaccess.com",
            "access_audience": "audience",
            "allowed_emails": "owner@example.test",
        }
    )
    core = FixtureCore()
    with TestClient(create_app(access, core=core)) as client:
        assert client.get("/api/health/live").status_code == 200
        assert client.get("/api/v1/bootstrap").status_code == 403
        assert (
            client.get(
                "/api/v1/bootstrap", headers={"Cf-Access-Jwt-Assertion": "valid-token"}
            ).status_code
            == 200
        )


async def test_event_broadcaster_replays_cache_and_core_events() -> None:
    core = FixtureCore()
    core.snapshot["last_event_id"] = 0
    core.events = [_stream_event(1)]
    broadcaster = EventBroadcaster(core)
    queue = broadcaster.subscribe()
    await broadcaster.start()
    event = await asyncio.wait_for(queue.get(), timeout=1.5)
    assert event.id == 1
    assert [item.id for item in await broadcaster.replay(0)] == [1]
    assert [item.id for item in await broadcaster.replay(99)] == []
    broadcaster.unsubscribe(queue)
    await broadcaster.close()


def _request(receive: Any, *, last_event_id: str = "0") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/stream",
            "raw_path": b"/api/v1/stream",
            "query_string": b"",
            "headers": [(b"last-event-id", last_event_id.encode())],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 1),
            "root_path": "",
            "app": None,
        },
        receive=receive,
    )


async def _disconnect() -> dict[str, str]:
    return {"type": "http.disconnect"}


async def test_sse_route_replays_and_enforces_connection_limit(settings: AppSettings) -> None:
    core = FixtureCore()
    core.events = [_stream_event(2)]
    app = create_app(settings.model_copy(update={"sse_connection_limit": 1}), core=core)
    endpoint = next(route.endpoint for route in app.routes if route.path == "/api/v1/stream")
    response = await endpoint(_request(_disconnect, last_event_id="not-a-number"))
    assert isinstance(response, StreamingResponse)
    chunks = [chunk async for chunk in response.body_iterator]
    assert "event: incident" in "".join(chunks)

    first = await endpoint(_request(_disconnect))
    second = await endpoint(_request(_disconnect))
    assert isinstance(first, StreamingResponse)
    assert isinstance(second, JSONResponse)
    assert second.status_code == 429
