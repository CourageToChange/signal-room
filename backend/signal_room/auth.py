from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, cast

import httpx
import jwt


class AuthenticationError(Exception):
    pass


@dataclass(frozen=True)
class Identity:
    subject: str
    email: str


class AccessTokenVerifier:
    def __init__(
        self,
        team_domain: str,
        audience: str,
        allowed_emails: set[str],
        *,
        clock_leeway_seconds: int = 30,
    ) -> None:
        self.team_domain = team_domain.rstrip("/")
        self.audience = audience
        self.allowed_emails = allowed_emails
        self.clock_leeway_seconds = clock_leeway_seconds
        self._jwks: dict[str, Any] | None = None
        self._jwks_expires_at = 0.0
        self._jwks_stale_until = 0.0
        self._lock = asyncio.Lock()

    async def _fetch_jwks(self) -> dict[str, Any]:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(5), follow_redirects=False, trust_env=False
        ) as client:
            response = await client.get(f"{self.team_domain}/cdn-cgi/access/certs")
            response.raise_for_status()
            payload = cast(dict[str, Any], response.json())
        keys = payload.get("keys")
        if not isinstance(keys, list) or not keys:
            raise AuthenticationError("Access signing keys are unavailable")
        if any(not isinstance(item, dict) or not item.get("kid") for item in keys):
            raise AuthenticationError("Access signing keys are malformed")
        return payload

    async def _get_jwks(self, *, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if not force and self._jwks and now < self._jwks_expires_at:
            return self._jwks
        async with self._lock:
            now = time.monotonic()
            if not force and self._jwks and now < self._jwks_expires_at:
                return self._jwks
            try:
                payload = await self._fetch_jwks()
            except (httpx.HTTPError, ValueError, AuthenticationError):
                # Retain a last-known-good set for a bounded rotation outage. Tokens
                # still undergo full signature and current-claim validation.
                if self._jwks and now < self._jwks_stale_until:
                    return self._jwks
                raise AuthenticationError("Access signing keys are unavailable") from None
            self._jwks = payload
            self._jwks_expires_at = now + 21_600
            self._jwks_stale_until = now + 86_400
            return payload

    async def verify(self, token: str) -> Identity:
        if not token:
            raise AuthenticationError("Access token is required")
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256":
                raise AuthenticationError("Access signing algorithm is not allowed")
            key_id = header.get("kid")
            if not isinstance(key_id, str) or not key_id:
                raise AuthenticationError("Access signing key id is missing")
            jwks = await self._get_jwks()
            key_data = next((item for item in jwks["keys"] if item.get("kid") == key_id), None)
            if key_data is None:
                jwks = await self._get_jwks(force=True)
                key_data = next((item for item in jwks["keys"] if item.get("kid") == key_id), None)
            if key_data is None:
                raise AuthenticationError("Access signing key is not recognized")
            key = jwt.PyJWK.from_dict(key_data).key
            claims = jwt.decode(
                token,
                key=key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.team_domain,
                leeway=self.clock_leeway_seconds,
                options={"require": ["sub", "exp", "iat", "aud", "iss", "email"]},
            )
            email_claim = claims["email"]
            subject_claim = claims["sub"]
            if not isinstance(email_claim, str) or not isinstance(subject_claim, str):
                raise AuthenticationError("Access identity claims are malformed")
            email = email_claim.strip().lower()
            subject = subject_claim.strip()
            if not subject or email not in self.allowed_emails:
                raise AuthenticationError("Access identity is not allowlisted")
            return Identity(subject=subject, email=email)
        except AuthenticationError:
            raise
        except (jwt.PyJWTError, httpx.HTTPError, KeyError, TypeError, ValueError) as error:
            raise AuthenticationError("Access token is invalid") from error


class MutationLimiter:
    def __init__(self, limit: int, window_seconds: int = 60) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._events: dict[str, list[float]] = {}

    def allow(self, identity: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        recent = [value for value in self._events.get(identity, []) if value >= cutoff]
        if len(recent) >= self.limit:
            self._events[identity] = recent
            return False
        recent.append(now)
        self._events[identity] = recent
        return True
