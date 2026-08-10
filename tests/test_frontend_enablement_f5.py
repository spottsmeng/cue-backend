"""One small, scoped backend addition made while extending `Prompt F5 — Ask
and Successor Brief.txt` (frontend), same "close the gap you find, document
it" pattern as the additions `backend/PROGRESS.md`'s rounds 1–4 already list:

`GET /projects/{project_id}/documents/versions/{version_id}` — an Ask
`Citation` with `source_type == "document_version"` (app/ask/schema.py) only
ever carries the DocumentVersion's own id (app/ask/answer.py's
_resolve_citation), never its parent document_id — every other version
route in this router is nested under `/{document_id}/versions/{version_id}`
and needs both. `DocumentVersionOut` already carries `document_id` itself
(no new response shape needed, unlike round 4's `SpecClaimResolvedOut`),
so this is a plain lookup by version id alone — the same "an id surfaced
with no paired way to resolve it" gap shape frontend/CLAUDE.md's own
gap-audit section names (Class A), just for a different field.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from main import app


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _upload_document(client, token, project_id):
    return await client.post(
        f"/projects/{project_id}/documents",
        headers=_headers(token),
        data={"name": "Booth floor plan", "extracted_text": "Location H: 2040mm x 1040mm panel."},
        files={"file": ("floor-plan.pdf", b"%PDF-1.4 fake bytes", "application/pdf")},
    )


@pytest.mark.asyncio
async def test_read_version_resolves_by_version_id_alone(authed_org_and_project):
    org_id, project_id, user, token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await _upload_document(client, token, project_id)
        document_id = created.json()["id"]
        version_id = created.json()["current_version_id"]

        resolved = await client.get(
            f"/projects/{project_id}/documents/versions/{version_id}", headers=_headers(token)
        )
        assert resolved.status_code == 200, resolved.text
        body = resolved.json()
        assert body["id"] == version_id
        assert body["document_id"] == document_id
        assert body["version_no"] == 1


@pytest.mark.asyncio
async def test_read_version_404_for_unknown_id(authed_org_and_project):
    org_id, project_id, user, token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.get(
            f"/projects/{project_id}/documents/versions/{uuid.uuid4()}", headers=_headers(token)
        )
        assert missing.status_code == 404


@pytest.mark.asyncio
async def test_read_version_404_for_other_project(authed_org_and_project):
    """Same project-scoping shape round 4's spec-claim resolver test
    asserts — a version id that's real, just not in the caller's project,
    404s rather than leaking cross-tenant existence."""
    org_id, project_id, user, token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await _upload_document(client, token, project_id)
        version_id = created.json()["current_version_id"]

        cross_tenant = await client.get(
            f"/projects/{uuid.uuid4()}/documents/versions/{version_id}", headers=_headers(token)
        )
        assert cross_tenant.status_code == 404
