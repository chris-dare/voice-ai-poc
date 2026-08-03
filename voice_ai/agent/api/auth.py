from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import jwt
from jwt import PyJWKClient
from jwt.exceptions import ImmatureSignatureError, InvalidIssuedAtError, PyJWTError

from voice_ai.shared.config import Settings
from voice_ai.shared.observability import record_auth_event, record_auth_iat_offset


@dataclass(frozen=True, slots=True)
class AuthContext:
    tenant_id: str
    subject_id: str
    scopes: frozenset[str]
    claims: dict[str, Any]


class AuthFailure(RuntimeError):
    def __init__(self, status_code: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail


class AccessTokenVerifier(Protocol):
    async def verify(self, token: str) -> AuthContext: ...


class Auth0AccessTokenVerifier:
    """Validate Auth0 RS256 access tokens against the tenant's cached JWKS."""

    def __init__(self, settings: Settings) -> None:
        issuer = settings.auth0_issuer
        audience = settings.auth0_audience
        jwks_url = settings.auth0_jwks_url
        if not issuer or not audience or not jwks_url:
            raise ValueError("AUTH0_DOMAIN and AUTH0_AUDIENCE must be configured")
        self._settings = settings
        self._issuer = issuer
        self._audience = audience
        self._jwks = PyJWKClient(
            jwks_url,
            cache_jwk_set=True,
            cache_keys=True,
            lifespan=300,
            timeout=5,
        )

    async def verify(self, token: str) -> AuthContext:
        try:
            claims = await asyncio.to_thread(self._decode, token)
        except PyJWTError as exc:
            record_auth_event(outcome=type(exc).__name__, stage="verification")
            raise AuthFailure(401, "invalid_token", "The access token is invalid or expired.") from exc
        except Exception as exc:
            raise AuthFailure(
                503,
                "identity_provider_unavailable",
                "The token signing keys could not be retrieved.",
            ) from exc

        subject = str(claims.get("sub") or "").strip()
        if not subject:
            raise AuthFailure(401, "invalid_token", "The access token has no subject.")

        tenant_claim = self._settings.auth0_tenant_claim
        tenant = str(
            claims.get(tenant_claim)
            or self._settings.auth0_default_tenant_id
            or self._settings.auth0_domain
        ).strip()
        if not tenant:
            raise AuthFailure(403, "tenant_required", "No trusted tenant is associated with the token.")

        scope_claim = claims.get("scope")
        scopes = set(str(scope_claim).split()) if scope_claim else set()
        permissions = claims.get("permissions")
        if isinstance(permissions, list):
            scopes.update(str(permission) for permission in permissions)
        record_auth_event(outcome="accepted", stage="verification")
        return AuthContext(
            tenant_id=tenant,
            subject_id=subject,
            scopes=frozenset(scopes),
            claims=claims,
        )

    def _decode(self, token: str) -> dict[str, Any]:
        signing_key = self._jwks.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=self._audience,
            issuer=self._issuer,
            options={
                "require": ["exp", "iat", "iss", "sub", "aud"],
                "verify_signature": True,
                "verify_exp": True,
                # Auth0 and the API host can differ by a few seconds. Validate
                # iat separately so tolerance does not also extend exp/nbf.
                "verify_iat": False,
                "verify_iss": True,
                "verify_aud": True,
            },
        )
        issued_at = claims.get("iat")
        if isinstance(issued_at, bool) or not isinstance(issued_at, (int, float)):
            raise InvalidIssuedAtError("Issued At claim (iat) must be a number.")
        now = datetime.now(UTC).timestamp()
        offset = issued_at - now
        if offset > self._settings.auth0_iat_skew_seconds:
            record_auth_iat_offset(seconds=offset, outcome="rejected")
            raise ImmatureSignatureError("The token is not yet valid (iat).")
        if offset > 0:
            record_auth_iat_offset(seconds=offset, outcome="accepted")
        return claims


def require_scopes(context: AuthContext, *required: str) -> None:
    missing = [scope for scope in required if scope not in context.scopes]
    if missing:
        raise AuthFailure(
            403,
            "insufficient_scope",
            f"Missing required scope: {', '.join(missing)}.",
        )
