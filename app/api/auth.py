"""POST /auth/dev-login — bearer-token issuance for local development and
the frontend's own dev-login flow (frontend/PROGRESS.md's F0 milestone).

app/identity/tokens.py's mint_local_token already existed, called only from
tests/conftest.py; this is that same function, reachable over HTTP for the
first time. Before this endpoint existed there was no way for a browser-
based client to authenticate against this API at all — see that module's
own docstring for the "local" (self-issued HS256) vs. "oidc" (real IdP,
untested against a live tenant in this build) provider split this endpoint
sits inside.

Deliberately narrow and deliberately dangerous if misconfigured: there is
no password, no credential check, no proof of identity at all beyond the
caller naming an organisation_id and an email — anyone who can reach this
endpoint can mint a token for any organisation in the database. The ONLY
guard is get_identity_settings().auth_provider — this 404s outright unless
it is exactly "local", so it can never be a live credential-bypass path in
a deployment actually federated to a real IdP (Keycloak/Zitadel, per
CUE-Tech-Stack.md §2.6) via CUE_AUTH_PROVIDER=oidc. Treat any change to
that guard with the seriousness NFR-SEC-03 gives real secrets — this is
the one endpoint in this codebase where a bug in an `if` statement is a
genuine tenant-isolation failure, not just a wrong response.
"""

import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.identity.config import get_identity_settings
from app.identity.tokens import mint_local_token

router = APIRouter(prefix="/auth", tags=["auth"])

_TOKEN_TTL_SECONDS = 3600


class DevLoginRequest(BaseModel):
    organisation_id: uuid.UUID
    email: str
    name: str | None = None


class DevLoginOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


@router.post("/dev-login", response_model=DevLoginOut)
async def dev_login(body: DevLoginRequest) -> DevLoginOut:
    """No database access at all — token minting is pure JWT signing
    (app/identity/tokens.py's mint_local_token). The token is only made
    real the first time it's used against an authenticated endpoint:
    app/identity/service.py's resolve_user upserts the `users` row then,
    keyed by (issuer, subject), with a real FK to `organisations` — a
    caller naming an organisation_id that doesn't exist gets a token that
    verifies fine but then 500s/fails on its first real request, not a
    failure at login time. That's an acceptable dev-tool failure mode
    (this endpoint has no way to check organisation existence without
    picking an org context to query under, which isn't its job); a real
    frontend dev-login flow should source organisation_id from a value a
    seed script already printed, not a value typed blind.
    """
    settings = get_identity_settings()
    if settings.auth_provider != "local":
        raise HTTPException(status_code=404, detail="not found")

    token = mint_local_token(
        subject=body.email,
        org_id=body.organisation_id,
        secret=settings.local_jwt_secret,
        issuer=settings.local_issuer,
        email=body.email,
        name=body.name,
        ttl_seconds=_TOKEN_TTL_SECONDS,
    )
    return DevLoginOut(access_token=token, expires_in=_TOKEN_TTL_SECONDS)
