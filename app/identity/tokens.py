"""JWT verification, provider-pluggable — same env-driven-provider pattern as
app/llm/factory.py's Ollama/Anthropic switch: callers ask for `get_token_verifier()`
and never see which provider answered.

"local" (default): HS256 against a shared secret this deployment controls.
`mint_local_token` issues tokens for it — that's what tests/conftest.py and local
dev use in place of a real IdP login. Never accepted by the "oidc" verifier.

"oidc": RS256 verified against a real OIDC issuer's published JWKS — the shape
Keycloak (or Zitadel) presents, per CUE-Tech-Stack.md §2.6, and what Entra ID
would present once federated through one of those brokers. Wiring an actual
Keycloak/Entra ID tenant is a deployment step this module deliberately does not
take (CUE-PRD.md's explicit out-of-scope note) — this class is the pluggable
slot that makes doing so later a config change, not a code change.

Every provider must resolve the same claim shape: `sub` (stable per-issuer
subject), `org_id` (which CUE organisation this session is scoped to — a
custom claim; the IdP broker's claim-mapping config, not this code, is
responsible for populating it correctly), and optionally `email`/`name`.
"""

import time
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

import httpx
from authlib.jose import JoseError, jwt

from app.identity.config import get_identity_settings


class TokenVerificationError(Exception):
    """Raised for any invalid bearer token — bad signature, expired, wrong
    issuer/audience, or missing a claim CUE requires. Deliberately flat (no
    subclasses to distinguish reasons): the API layer turns any of these into
    a 401 without needing to branch on why, and the underlying library
    exception text is not something a caller should see."""


@dataclass(frozen=True)
class TokenClaims:
    subject: str
    issuer: str
    org_id: uuid.UUID
    email: str | None
    name: str | None


def _to_token_claims(claims: dict) -> TokenClaims:
    sub = claims.get("sub")
    if not sub:
        raise TokenVerificationError("token missing required 'sub' claim")
    org_id_raw = claims.get("org_id")
    if not org_id_raw:
        raise TokenVerificationError("token missing required 'org_id' claim")
    try:
        org_id = uuid.UUID(str(org_id_raw))
    except ValueError as e:
        raise TokenVerificationError("token 'org_id' claim is not a UUID") from e
    return TokenClaims(
        subject=str(sub),
        issuer=str(claims.get("iss") or ""),
        org_id=org_id,
        email=claims.get("email"),
        name=claims.get("name"),
    )


class TokenVerifier(Protocol):
    def verify(self, token: str) -> TokenClaims: ...


class LocalJWTVerifier:
    """HS256, shared secret. Dev/test only — see module docstring."""

    def __init__(self, secret: str, issuer: str) -> None:
        self._secret = secret
        self._issuer = issuer

    def verify(self, token: str) -> TokenClaims:
        try:
            claims = jwt.decode(
                token,
                self._secret,
                claims_options={
                    "iss": {"essential": True, "value": self._issuer},
                    "exp": {"essential": True},
                },
            )
            claims.validate()
        except JoseError as e:
            raise TokenVerificationError(f"invalid token: {e}") from e
        return _to_token_claims(claims)


def mint_local_token(
    *,
    subject: str,
    org_id: uuid.UUID,
    secret: str,
    issuer: str = "cue-local",
    email: str | None = None,
    name: str | None = None,
    ttl_seconds: int = 3600,
) -> str:
    """Self-issued HS256 token for the "local" provider. Used by
    tests/conftest.py (every RBAC test needs a real, verifiable bearer token,
    not a header the app trusts blindly) and available for local dev the same
    way. Not a general-purpose token minter — it only ever produces tokens the
    LocalJWTVerifier accepts."""
    now = int(time.time())
    payload = {
        "sub": subject,
        "iss": issuer,
        "org_id": str(org_id),
        "iat": now,
        "exp": now + ttl_seconds,
    }
    if email is not None:
        payload["email"] = email
    if name is not None:
        payload["name"] = name
    token = jwt.encode({"alg": "HS256"}, payload, secret)
    return token.decode("utf-8")


class OIDCJWTVerifier:
    """RS256 against a real OIDC issuer's JWKS. Untested against a live IdP in
    this build slice (no Keycloak/Entra tenant to point at — see module
    docstring); the shape follows the OIDC discovery spec so it is exercised
    for real the moment CUE_AUTH_PROVIDER=oidc points at one."""

    def __init__(
        self,
        issuer: str,
        audience: str,
        jwks_url: str | None = None,
        *,
        cache_ttl_seconds: float = 3600.0,
    ) -> None:
        self._issuer = issuer
        self._audience = audience
        self._jwks_url = jwks_url or self._discover_jwks_url(issuer)
        self._cache_ttl_seconds = cache_ttl_seconds
        self._jwks_cache: dict | None = None
        self._jwks_fetched_at: float = 0.0

    @staticmethod
    def _discover_jwks_url(issuer: str) -> str:
        well_known = issuer.rstrip("/") + "/.well-known/openid-configuration"
        response = httpx.get(well_known, timeout=5.0)
        response.raise_for_status()
        return response.json()["jwks_uri"]

    def _jwks(self) -> dict:
        now = time.monotonic()
        if self._jwks_cache is None or (now - self._jwks_fetched_at) > self._cache_ttl_seconds:
            response = httpx.get(self._jwks_url, timeout=5.0)
            response.raise_for_status()
            self._jwks_cache = response.json()
            self._jwks_fetched_at = now
        return self._jwks_cache

    def verify(self, token: str) -> TokenClaims:
        try:
            claims = jwt.decode(
                token,
                self._jwks(),
                claims_options={
                    "iss": {"essential": True, "value": self._issuer},
                    "aud": {"essential": True, "value": self._audience},
                    "exp": {"essential": True},
                },
            )
            claims.validate()
        except JoseError as e:
            raise TokenVerificationError(f"invalid token: {e}") from e
        return _to_token_claims(claims)


@lru_cache
def get_token_verifier() -> TokenVerifier:
    """Cached per process (same rationale as get_settings/get_llm_settings):
    the OIDC provider's JWKS cache is only useful if the verifier instance
    itself survives across requests."""
    settings = get_identity_settings()
    if settings.auth_provider == "local":
        return LocalJWTVerifier(settings.local_jwt_secret, settings.local_issuer)
    if settings.auth_provider == "oidc":
        if not settings.oidc_issuer or not settings.oidc_audience:
            raise ValueError(
                "CUE_AUTH_OIDC_ISSUER and CUE_AUTH_OIDC_AUDIENCE are required "
                "when CUE_AUTH_PROVIDER=oidc"
            )
        return OIDCJWTVerifier(settings.oidc_issuer, settings.oidc_audience, settings.oidc_jwks_url)
    raise ValueError(f"unknown auth provider: {settings.auth_provider!r}")
