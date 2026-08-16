from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from voice_ai.agent.api.auth import Auth0AccessTokenVerifier, AuthFailure
from voice_ai.shared.config import Settings


class StaticJwks:
    def __init__(self, key) -> None:
        self.key = key

    def get_signing_key_from_jwt(self, _token: str):
        return SimpleNamespace(key=self.key)


def _verifier_and_key(**overrides):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    settings = Settings(
        api_enabled=True,
        auth0_domain="tenant.example.auth0.com",
        auth0_audience="https://voice-api.example.com",
        **overrides,
    )
    verifier = Auth0AccessTokenVerifier(settings)
    verifier._jwks = StaticJwks(private_key.public_key())  # type: ignore[assignment]
    return settings, verifier, private_key


def _token(settings, private_key, **claims):
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": settings.auth0_issuer,
            "aud": settings.auth0_audience,
            "sub": "auth0|user_1",
            "iat": now,
            "exp": now + timedelta(minutes=5),
            **claims,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )


@pytest.mark.asyncio
async def test_auth0_verifier_checks_claims_and_combines_permissions() -> None:
    settings, verifier, private_key = _verifier_and_key()
    token = _token(
        settings,
        private_key,
        org_id="org_1",
        scope="agents:invoke responses:read",
        permissions=["conversations:write"],
    )

    context = await verifier.verify(token)

    assert context.tenant_id == "org_1"
    assert context.subject_id == "auth0|user_1"
    assert context.scopes == {
        "agents:invoke",
        "responses:read",
        "conversations:write",
    }


@pytest.mark.asyncio
async def test_auth0_verifier_accepts_user_without_external_domain_binding() -> None:
    settings, verifier, private_key = _verifier_and_key()

    context = await verifier.verify(_token(settings, private_key, org_id="org_1"))

    assert context.tenant_id == "org_1"
    assert context.subject_id == "auth0|user_1"


@pytest.mark.asyncio
async def test_auth0_verifier_rejects_wrong_audience() -> None:
    settings, verifier, private_key = _verifier_and_key()
    token = _token(settings, private_key, aud="https://wrong.example.com")

    with pytest.raises(AuthFailure) as error:
        await verifier.verify(token)
    assert error.value.status_code == 401
    assert error.value.code == "invalid_token"


@pytest.mark.asyncio
async def test_auth0_verifier_accepts_small_future_iat_but_rejects_larger_skew() -> None:
    settings, verifier, private_key = _verifier_and_key(auth0_iat_skew_seconds=30)
    now = datetime.now(UTC)
    accepted = _token(settings, private_key, iat=now + timedelta(seconds=10))

    assert (await verifier.verify(accepted)).subject_id == "auth0|user_1"

    rejected = _token(settings, private_key, iat=now + timedelta(seconds=60))
    with pytest.raises(AuthFailure) as error:
        await verifier.verify(rejected)
    assert error.value.status_code == 401
    assert error.value.code == "invalid_token"
