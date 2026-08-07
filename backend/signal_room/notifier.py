from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

import httpx

from .config import AppSettings
from .core import CoreTransport
from .providers import resolve_public_addresses

LOGGER = logging.getLogger("signal_room.notifier")


class Notifier:
    def __init__(self, settings: AppSettings, core: CoreTransport) -> None:
        self.settings = settings
        self.core = core
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def _pinned_post(self, url: str, content: bytes, headers: dict[str, str]) -> None:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("notification destination must be an HTTPS URL without userinfo")
        port = parsed.port or 443
        if port != 443:
            raise ValueError("notification destination must use port 443")
        addresses = await resolve_public_addresses(parsed.hostname, port)
        address = sorted(addresses, key=lambda item: ipaddress.ip_address(item).version)[0]
        netloc = f"[{address}]" if ":" in address else address
        pinned = urlunsplit(("https", netloc, parsed.path or "/", parsed.query, ""))
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(8),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.post(
                pinned,
                content=content,
                headers={"Host": parsed.hostname, **headers},
                extensions={"sni_hostname": parsed.hostname},
            )
            response.raise_for_status()

    async def _deliver(self, item: dict[str, object]) -> None:
        event_uuid = str(item["event_uuid"])
        payload = json.dumps(item["payload"], separators=(",", ":"), sort_keys=True).encode()
        timestamp = str(int(datetime.now(UTC).timestamp()))
        signed = timestamp.encode() + b"." + payload
        signature = hmac.new(
            self.settings.webhook_secret.encode(), signed, hashlib.sha256
        ).hexdigest()
        await self._pinned_post(
            self.settings.webhook_url,
            payload,
            {
                "Content-Type": "application/json",
                "User-Agent": "Signal-Room/1.0 notifier",
                "X-Signal-Room-Event-ID": event_uuid,
                "X-Signal-Room-Timestamp": timestamp,
                "X-Signal-Room-Signature": f"sha256={signature}",
            },
        )

    async def run_once(self) -> None:
        enabled = self.settings.webhook_enabled
        await self.core.call(
            "notification_heartbeat",
            {"enabled": enabled, "at": datetime.now(UTC).isoformat()},
        )
        if not enabled:
            return
        items = await self.core.call(
            "notifications_due", {"at": datetime.now(UTC).isoformat(), "limit": 20}
        )
        for item in items:
            at = datetime.now(UTC)
            try:
                await self._deliver(item)
            except (httpx.HTTPError, OSError, ValueError) as error:
                diagnostic = type(error).__name__
                LOGGER.warning("notification delivery failed for event %s", item["event_uuid"])
                await self.core.call(
                    "notification_mark",
                    {
                        "event_uuid": item["event_uuid"],
                        "delivered": False,
                        "at": at.isoformat(),
                        "diagnostic": diagnostic,
                    },
                )
            else:
                await self.core.call(
                    "notification_mark",
                    {
                        "event_uuid": item["event_uuid"],
                        "delivered": True,
                        "at": at.isoformat(),
                    },
                )

    async def _deadman(self) -> None:
        if not self.settings.deadman_url:
            return
        payload = json.dumps(
            {"service": "signal-room", "at": datetime.now(UTC).isoformat()},
            separators=(",", ":"),
        ).encode()
        await self._pinned_post(
            self.settings.deadman_url,
            payload,
            {"Content-Type": "application/json", "User-Agent": "Signal-Room/1.0 dead-man"},
        )
        await self.core.call(
            "notification_heartbeat",
            {
                "enabled": self.settings.webhook_enabled,
                "success": True,
                "at": datetime.now(UTC).isoformat(),
            },
        )

    async def run_forever(self) -> None:
        loop = asyncio.get_running_loop()
        next_deadman = loop.time()
        while not self._stop.is_set():
            try:
                await self.run_once()
                if self.settings.deadman_url and loop.time() >= next_deadman:
                    await self._deadman()
                    next_deadman = loop.time() + 300
            except Exception:  # noqa: BLE001 -- boundary redacts secret-bearing failures
                LOGGER.exception("notifier cycle failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=15)
            except TimeoutError:
                pass
