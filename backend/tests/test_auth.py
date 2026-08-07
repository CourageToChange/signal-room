from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from signal_room.auth import AccessTokenVerifier, AuthenticationError, MutationLimiter


def access_token(
    private_key,
    *,
    email: object,
    issuer: str,
    audience: str,
    expires: datetime,
    subject: object = "subject-123",
    algorithm: str = "RS256",
    kid: str | None = "test-key",
) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": subject,
            "email": email,
            "iss": issuer,
            "aud": audience,
            "iat": int(now.timestamp()),
            "exp": int(expires.timestamp()),
        },
        private_key,
        algorithm=algorithm,
        headers={} if kid is None else {"kid": kid},
    )


@pytest.fixture
def verifier_and_key():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    public_jwk["kid"] = "test-key"
    verifier = AccessTokenVerifier(
        "https://team.cloudflareaccess.com", "audience", {"owner@example.invalid"}
    )
    verifier._jwks = {"keys": [public_jwk]}
    verifier._jwks_expires_at = time.monotonic() + 600
    return verifier, private_key


async def test_access_token_verifies_signature_audience_and_email(verifier_and_key) -> None:
    verifier, private_key = verifier_and_key
    token = access_token(
        private_key,
        email="owner@example.invalid",
        issuer="https://team.cloudflareaccess.com",
        audience="audience",
        expires=datetime.now(UTC) + timedelta(minutes=5),
    )
    identity = await verifier.verify(token)
    assert identity.subject == "subject-123"
    assert identity.email == "owner@example.invalid"


async def test_access_token_rejects_non_allowlisted_identity(verifier_and_key) -> None:
    verifier, private_key = verifier_and_key
    token = access_token(
        private_key,
        email="intruder@example.invalid",
        issuer="https://team.cloudflareaccess.com",
        audience="audience",
        expires=datetime.now(UTC) + timedelta(minutes=5),
    )
    with pytest.raises(AuthenticationError):
        await verifier.verify(token)


async def test_access_token_rejects_expired_token(verifier_and_key) -> None:
    verifier, private_key = verifier_and_key
    token = access_token(
        private_key,
        email="owner@example.invalid",
        issuer="https://team.cloudflareaccess.com",
        audience="audience",
        expires=datetime.now(UTC) - timedelta(seconds=60),
    )
    with pytest.raises(AuthenticationError):
        await verifier.verify(token)


@pytest.mark.parametrize(
    ("issuer", "audience"),
    [
        ("https://other.cloudflareaccess.com", "audience"),
        ("https://team.cloudflareaccess.com", "wrong-audience"),
    ],
)
async def test_access_token_rejects_wrong_issuer_or_audience(
    verifier_and_key, issuer: str, audience: str
) -> None:
    verifier, private_key = verifier_and_key
    token = access_token(
        private_key,
        email="owner@example.invalid",
        issuer=issuer,
        audience=audience,
        expires=datetime.now(UTC) + timedelta(minutes=5),
    )
    with pytest.raises(AuthenticationError, match="invalid"):
        await verifier.verify(token)


@pytest.mark.parametrize("missing", ["sub", "email", "iss", "aud", "iat", "exp"])
async def test_access_token_rejects_missing_required_claim(verifier_and_key, missing: str) -> None:
    verifier, private_key = verifier_and_key
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "sub": "subject-123",
        "email": "owner@example.invalid",
        "iss": "https://team.cloudflareaccess.com",
        "aud": "audience",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }
    del claims[missing]
    token = jwt.encode(
        claims,
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )
    with pytest.raises(AuthenticationError, match="invalid"):
        await verifier.verify(token)


async def test_access_token_rejects_missing_algorithm_key_and_identity_claims(
    verifier_and_key,
) -> None:
    verifier, private_key = verifier_and_key
    with pytest.raises(AuthenticationError, match="required"):
        await verifier.verify("")
    hs_token = jwt.encode(
        {"sub": "subject", "email": "owner@example.invalid"},
        "test-secret-at-least-thirty-two-bytes",
        algorithm="HS256",
        headers={"kid": "test-key"},
    )
    with pytest.raises(AuthenticationError, match="algorithm"):
        await verifier.verify(hs_token)
    no_kid = access_token(
        private_key,
        email="owner@example.invalid",
        issuer="https://team.cloudflareaccess.com",
        audience="audience",
        expires=datetime.now(UTC) + timedelta(minutes=5),
        kid=None,
    )
    with pytest.raises(AuthenticationError, match="key id"):
        await verifier.verify(no_kid)
    for email, subject in [
        (123, "subject"),
        ("owner@example.invalid", 123),
        ("owner@example.invalid", "   "),
    ]:
        token = access_token(
            private_key,
            email=email,
            subject=subject,
            issuer="https://team.cloudflareaccess.com",
            audience="audience",
            expires=datetime.now(UTC) + timedelta(minutes=5),
        )
        with pytest.raises(AuthenticationError):
            await verifier.verify(token)
    with pytest.raises(AuthenticationError, match="invalid"):
        await verifier.verify("not-a-jwt")


async def test_jwks_rotation_refreshes_unknown_key(monkeypatch: pytest.MonkeyPatch) -> None:
    old_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    new_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    old_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(old_key.public_key(), as_dict=True)
    old_jwk["kid"] = "old-key"
    new_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(new_key.public_key(), as_dict=True)
    new_jwk["kid"] = "new-key"
    verifier = AccessTokenVerifier(
        "https://team.cloudflareaccess.com", "audience", {"owner@example.invalid"}
    )
    verifier._jwks = {"keys": [old_jwk]}
    verifier._jwks_expires_at = time.monotonic() + 600
    verifier._jwks_stale_until = time.monotonic() + 600
    calls = 0

    async def refresh() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"keys": [new_jwk]}

    monkeypatch.setattr(verifier, "_fetch_jwks", refresh)
    token = jwt.encode(
        {
            "sub": "rotated-subject",
            "email": "owner@example.invalid",
            "iss": "https://team.cloudflareaccess.com",
            "aud": "audience",
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
        },
        new_key,
        algorithm="RS256",
        headers={"kid": "new-key"},
    )
    assert (await verifier.verify(token)).subject == "rotated-subject"
    assert calls == 1
    assert await verifier._get_jwks() == {"keys": [new_jwk]}


async def test_jwks_last_good_fallback_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    verifier = AccessTokenVerifier("https://team.cloudflareaccess.com", "aud", {"owner@test"})
    verifier._jwks = {"keys": [{"kid": "old"}]}
    verifier._jwks_expires_at = 0
    verifier._jwks_stale_until = time.monotonic() + 60

    async def unavailable() -> dict[str, object]:
        raise AuthenticationError("outage")

    monkeypatch.setattr(verifier, "_fetch_jwks", unavailable)
    assert await verifier._get_jwks() == verifier._jwks
    verifier._jwks_stale_until = 0
    with pytest.raises(AuthenticationError, match="unavailable"):
        await verifier._get_jwks()


class JwksResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.payload


class JwksClient:
    payload: object = {}

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def get(self, url: str) -> JwksResponse:
        return JwksResponse(self.payload)


async def test_jwks_fetch_rejects_empty_and_malformed_sets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("signal_room.auth.httpx.AsyncClient", JwksClient)
    verifier = AccessTokenVerifier("https://team.cloudflareaccess.com/", "aud", {"owner@test"})
    for payload, message in [
        ({}, "unavailable"),
        ({"keys": []}, "unavailable"),
        ({"keys": ["bad"]}, "malformed"),
        ({"keys": [{}]}, "malformed"),
    ]:
        JwksClient.payload = payload
        with pytest.raises(AuthenticationError, match=message):
            await verifier._fetch_jwks()
    JwksClient.payload = {"keys": [{"kid": "valid"}]}
    assert (await verifier._fetch_jwks())["keys"]
    assert verifier.team_domain == "https://team.cloudflareaccess.com"


def test_mutation_limiter_is_per_identity_and_window(monkeypatch: pytest.MonkeyPatch) -> None:
    values = iter([100.0, 101.0, 102.0, 200.0, 201.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(values))
    limiter = MutationLimiter(2, window_seconds=10)
    assert limiter.allow("owner")
    assert limiter.allow("owner")
    assert not limiter.allow("owner")
    assert limiter.allow("owner")
    assert limiter.allow("other")
