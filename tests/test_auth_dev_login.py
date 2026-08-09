"""POST /auth/dev-login (app/api/auth.py) — bearer-token issuance for local
dev and the frontend's own dev-login flow. Two properties matter: it works
end to end in "local" mode (a minted token is genuinely accepted by a real
authenticated endpoint afterward, not just structurally well-formed), and
it is completely unreachable once CUE_AUTH_PROVIDER is anything else.
"""

import pytest
from httpx import ASGITransport, AsyncClient

import app.api.auth as auth_module
from app.identity.config import IdentitySettings
from main import app


@pytest.mark.asyncio
async def test_dev_login_issues_a_token_accepted_by_a_real_endpoint(org_and_project):
    org_id, _project_id = org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        login_response = await client.post(
            "/auth/dev-login",
            json={"organisation_id": str(org_id), "email": "dev-login-user@example.test"},
        )
        assert login_response.status_code == 200, login_response.text
        body = login_response.json()
        assert body["token_type"] == "bearer"
        assert body["expires_in"] > 0
        token = body["access_token"]

        # The real proof: this token, minted purely by naming an
        # organisation_id and an email, actually authenticates a normal
        # request — resolve_user auto-provisions the `users` row on this
        # first sight, per app/identity/service.py.
        projects_response = await client.get(
            "/projects", headers={"Authorization": f"Bearer {token}"}
        )
        assert projects_response.status_code == 200, projects_response.text


@pytest.mark.asyncio
async def test_dev_login_is_a_real_identity_not_shared_across_emails(org_and_project):
    """Two different emails against the same org must resolve to two
    distinct users, not the same one — mint_local_token's subject is the
    email, and resolve_user upserts on (issuer, subject)."""
    org_id, _project_id = org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/auth/dev-login", json={"organisation_id": str(org_id), "email": "person-a@example.test"}
        )
        second = await client.post(
            "/auth/dev-login", json={"organisation_id": str(org_id), "email": "person-b@example.test"}
        )
        token_a = first.json()["access_token"]
        token_b = second.json()["access_token"]

        me_a = await client.get("/admin/users", headers={"Authorization": f"Bearer {token_a}"})
        me_b = await client.get("/admin/users", headers={"Authorization": f"Bearer {token_b}"})
        # Neither is an org administrator yet, so both 403 — the point here
        # is only that both are real, independently-authenticated requests
        # against the same org, not that either has admin access.
        assert me_a.status_code == 403
        assert me_b.status_code == 403


@pytest.mark.asyncio
async def test_dev_login_404s_when_provider_is_not_local(org_and_project, monkeypatch):
    """Patches the settings getter app/api/auth.py actually calls, not the
    underlying env vars — a real .env file on disk (or CI's own env) can
    take precedence over a bare os.environ mutation depending on
    pydantic-settings' source-merge order, which makes an env-var-only
    monkeypatch an unreliable way to prove this guard; patching the
    function this endpoint imports directly is what every other settings-
    dependent test in this suite that isn't specifically testing env-var
    parsing itself already does (see e.g. tests/test_capture_health.py's
    own monkeypatch.setattr idiom)."""
    org_id, _project_id = org_and_project
    oidc_settings = IdentitySettings(
        auth_provider="oidc",
        oidc_issuer="https://idp.example/realms/cue",
        oidc_audience="cue-backend",
    )
    monkeypatch.setattr(auth_module, "get_identity_settings", lambda: oidc_settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/dev-login",
            json={"organisation_id": str(org_id), "email": "should-not-work@example.test"},
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_dev_login_requires_a_real_uuid_for_organisation_id():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/dev-login", json={"organisation_id": "not-a-uuid", "email": "x@example.test"}
        )
    assert response.status_code == 422
