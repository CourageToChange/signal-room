from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest
from signal_room.config import AppSettings
from signal_room.notifier import Notifier


class CaptureCore:
    def __init__(self, due: list[dict[str, object]] | None = None) -> None:
        self.due = due or []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        values = params or {}
        self.calls.append((method, values))
        return self.due if method == "notifications_due" else {"ok": True}


def notifier_settings(settings: AppSettings, **updates: object) -> AppSettings:
    return settings.model_copy(
        update={
            "webhook_url": "https://hooks.example.test/signal-room",
            "webhook_secret": "test-signing-secret",  # pragma: allowlist secret
            **updates,
        }
    )


async def test_notifier_disabled_cycle_only_updates_heartbeat(settings: AppSettings) -> None:
    core = CaptureCore()
    notifier = Notifier(settings, core)
    await notifier.run_once()
    assert [method for method, _ in core.calls] == ["notification_heartbeat"]
    assert core.calls[0][1]["enabled"] is False
    notifier.stop()
    assert notifier._stop.is_set()


async def test_signed_delivery_uses_canonical_payload_and_event_id(
    settings: AppSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = CaptureCore()
    notifier = Notifier(notifier_settings(settings), core)
    captured: dict[str, object] = {}

    async def post(url: str, content: bytes, headers: dict[str, str]) -> None:
        captured.update(url=url, content=content, headers=headers)

    monkeypatch.setattr(notifier, "_pinned_post", post)
    await notifier._deliver(
        {
            "event_uuid": "event-123",
            "payload": {"z": 1, "a": "redacted"},
        }
    )
    assert captured["url"] == "https://hooks.example.test/signal-room"
    assert captured["content"] == b'{"a":"redacted","z":1}'
    headers = captured["headers"]
    assert isinstance(headers, dict)
    timestamp = headers["X-Signal-Room-Timestamp"]
    expected = hmac.new(
        b"test-signing-secret",
        timestamp.encode() + b'.{"a":"redacted","z":1}',
        hashlib.sha256,
    ).hexdigest()
    assert headers["X-Signal-Room-Signature"] == f"sha256={expected}"
    assert headers["X-Signal-Room-Event-ID"] == "event-123"


async def test_notifier_marks_success_and_redacted_failure_diagnostics(
    settings: AppSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    items = [
        {"event_uuid": "success", "payload": {}},
        {"event_uuid": "failure", "payload": {}},
    ]
    core = CaptureCore(items)
    notifier = Notifier(notifier_settings(settings), core)

    async def deliver(item: dict[str, object]) -> None:
        if item["event_uuid"] == "failure":
            raise ValueError("secret URL and credentials must not be persisted")

    monkeypatch.setattr(notifier, "_deliver", deliver)
    await notifier.run_once()
    marks = [params for method, params in core.calls if method == "notification_mark"]
    assert marks[0]["delivered"] is True
    assert marks[1]["delivered"] is False
    assert marks[1]["diagnostic"] == "ValueError"
    assert "secret" not in json.dumps(marks)


class FakeResponse:
    def raise_for_status(self) -> None:
        return None


class FakeClient:
    posts: list[tuple[str, bytes, dict[str, str], dict[str, str]]] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def post(
        self,
        url: str,
        *,
        content: bytes,
        headers: dict[str, str],
        extensions: dict[str, str],
    ) -> FakeResponse:
        self.posts.append((url, content, headers, extensions))
        return FakeResponse()


async def test_pinned_notification_destination_rejects_unsafe_urls_and_pins_dns(
    settings: AppSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    notifier = Notifier(notifier_settings(settings), CaptureCore())
    for url in (
        "http://hooks.example.test/",
        "https://user:pass@hooks.example.test/",  # pragma: allowlist secret
        "https://hooks.example.test:8443/",
    ):
        with pytest.raises(ValueError):
            await notifier._pinned_post(url, b"{}", {})

    async def resolve(host: str, port: int) -> set[str]:
        return {"2606:4700:4700::1111", "93.184.216.34"}

    FakeClient.posts.clear()
    monkeypatch.setattr("signal_room.notifier.resolve_public_addresses", resolve)
    monkeypatch.setattr("signal_room.notifier.httpx.AsyncClient", FakeClient)
    await notifier._pinned_post(
        "https://hooks.example.test/path?event=1",
        b"{}",
        {"X-Test": "1"},
    )
    url, _, headers, extensions = FakeClient.posts[0]
    assert url == "https://93.184.216.34/path?event=1"
    assert headers["Host"] == "hooks.example.test"
    assert extensions["sni_hostname"] == "hooks.example.test"


async def test_deadman_is_optional_and_records_success(
    settings: AppSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = CaptureCore()
    notifier = Notifier(settings, core)
    await notifier._deadman()
    assert core.calls == []

    notifier = Notifier(
        notifier_settings(settings, deadman_url="https://deadman.example.test/ping"), core
    )
    payloads: list[bytes] = []

    async def post(url: str, content: bytes, headers: dict[str, str]) -> None:
        payloads.append(content)

    monkeypatch.setattr(notifier, "_pinned_post", post)
    await notifier._deadman()
    assert json.loads(payloads[0])["service"] == "signal-room"
    assert core.calls[-1][1]["success"] is True


async def test_run_forever_executes_due_deadman_then_stops(
    settings: AppSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    notifier = Notifier(
        notifier_settings(settings, deadman_url="https://deadman.example.test/ping"),
        CaptureCore(),
    )
    cycles: list[str] = []

    async def run_once() -> None:
        cycles.append("cycle")

    async def deadman() -> None:
        cycles.append("deadman")
        notifier.stop()

    monkeypatch.setattr(notifier, "run_once", run_once)
    monkeypatch.setattr(notifier, "_deadman", deadman)
    await notifier.run_forever()
    assert cycles == ["cycle", "deadman"]
