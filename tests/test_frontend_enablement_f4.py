"""One small, scoped backend addition made while extending `Prompt F4 —
Documents.txt` (frontend), same "close the gap you find, document it"
pattern as the additions `backend/PROGRESS.md`'s rounds 1–3 already list:

`GET /projects/{project_id}/documents/spec-claims/{spec_claim_id}` —
`SpecClaim.contradicts` (rendered by F4's spec-claims view) can point at a
claim on a *different* document version than the one being viewed:
app/foresight/contradiction.py's own detector compares claims project-wide
by shared deliverable_id/location_code, never restricted to one version.
`GET .../versions/{id}/spec-claims` only ever returns claims for a single
version, so a `contradicts` target outside that list was otherwise an
unresolvable UUID with no way to reach its own document — the same "an id
surfaced with no paired way to resolve it" gap shape frontend/CLAUDE.md's
own gap-audit section names (Class A). `SpecClaimResolvedOut` adds just
enough document identity (id, name, the version's own number) to render and
link to it, without touching SpecClaim's own field set (CUE-PRD.md §4.3
already fixed that schema).
"""

import uuid
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from app.documents.models import Document, DocumentVersion, SpecClaim
from app.models import Evidence
from main import app
from tests.conftest import set_org_context


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _make_claim_with_evidence(
    app_session, project_id, *, location_code, attribute, value, document_name
) -> SpecClaim:
    """Mirrors tests/test_foresight_contradiction.py's own _make_claim — a
    SpecClaim needs a real Evidence row (NOT NULL, DB-trigger-enforced),
    same as its parent DocumentVersion."""
    document = Document(project_id=project_id, name=document_name)
    app_session.add(document)
    await app_session.flush()
    version = DocumentVersion(
        document_id=document.id, version_no=1, storage_ref=f"documents/{document.id}/v1/x",
        extracted_text=f"{location_code}: {value}",
    )
    app_session.add(version)
    await app_session.flush()
    app_session.add(
        Evidence(
            document_version_id=version.id, channel="manual",
            sent_at=datetime.now(timezone.utc), language="en", original_text="uploaded",
        )
    )
    document.current_version_id = version.id
    claim = SpecClaim(
        document_version_id=version.id, location_code=location_code, attribute=attribute, value=value,
    )
    app_session.add(claim)
    await app_session.flush()
    app_session.add(
        Evidence(
            spec_claim_id=claim.id, channel="manual",
            sent_at=datetime.now(timezone.utc), language="en", original_text=value,
        )
    )
    await app_session.commit()
    return claim


@pytest.mark.asyncio
async def test_read_spec_claim_resolves_cross_document_contradiction(app_session, authed_org_and_project):
    org_id, project_id, user, token = authed_org_and_project
    await set_org_context(app_session, org_id)

    claim_a = await _make_claim_with_evidence(
        app_session, project_id, location_code="H", attribute="dimension", value="2040mm x 1040mm",
        document_name="quotation.pdf",
    )
    claim_b = await _make_claim_with_evidence(
        app_session, project_id, location_code="H", attribute="dimension", value="2000mm x 1040mm",
        document_name="shop-drawing.pdf",
    )
    claim_b.contradicts = claim_a.id
    await app_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/projects/{project_id}/documents/spec-claims/{claim_a.id}", headers=_headers(token)
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == str(claim_a.id)
    assert body["document_name"] == "quotation.pdf"
    assert body["document_version_no"] == 1
    assert body["value"] == "2040mm x 1040mm"


@pytest.mark.asyncio
async def test_read_spec_claim_404_for_other_project(app_session, authed_org_and_project):
    org_id, project_id, user, token = authed_org_and_project
    await set_org_context(app_session, org_id)

    claim = await _make_claim_with_evidence(
        app_session, project_id, location_code="H", attribute="finish", value="matte", document_name="a.pdf",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/projects/{uuid.uuid4()}/documents/spec-claims/{claim.id}", headers=_headers(token)
        )

    assert response.status_code == 404
