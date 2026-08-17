"""REST endpoints for /projects/{id}/documents (PRD §11.2, CUE-PRD.md §6.6
FR-DOC): create (ingestion) · list · read · versions · lineage · approve ·
tag · search. Through the real ASGI app, same pattern as
tests/test_commitments_api.py / tests/test_budget_api.py. Storage goes
through the real MinioStorageBackend (docker-compose.yml's `minio` service)
— this codebase's own testing philosophy (tests/conftest.py's module
docstring: "against a real second database... not mocks") extends to
object storage the same way, rather than inventing a mocking pattern that
doesn't exist anywhere else in this suite.
"""

import uuid
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.documents.models import Document, DocumentVersion, SpecClaim
from app.identity.config import get_identity_settings
from app.identity.models import Membership, User
from app.models import Evidence
from main import app
from tests.conftest import mint_token, set_org_context


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _member(app_session, org_id, project_id, role, granted_by):
    await set_org_context(app_session, org_id)
    subject = f"{role}-{uuid.uuid4()}"
    user = User(
        organisation_id=org_id,
        issuer=get_identity_settings().local_issuer,
        external_subject=subject,
        email=f"{subject}@example.test",
    )
    app_session.add(user)
    await app_session.flush()
    app_session.add(Membership(user_id=user.id, project_id=project_id, role=role, granted_by=granted_by))
    await app_session.commit()
    return user, mint_token(org_id, subject=subject, email=user.email)


def _upload_files(filename="floor-plan.pdf", content=b"%PDF-1.4 fake bytes"):
    return {"file": (filename, content, "application/pdf")}


async def _upload_document(client, token, project_id, **form_overrides):
    data = {
        "name": "Booth floor plan",
        "extracted_text": (
            "Location H: 2040mm x 1040mm graphic panel, Graphic print on plywood, qty 1 set."
        ),
        **form_overrides,
    }
    return await client.post(
        f"/projects/{project_id}/documents",
        headers=_headers(token),
        data=data,
        files=_upload_files(),
    )


@pytest.mark.asyncio
async def test_create_document_manual_evidence_and_first_version(authed_org_and_project):
    """FR-DOC-01: ingestion — a Document plus its first DocumentVersion, in
    one call. FR-LED-10's manual-evidence posture is reused: the DB-layer
    evidence trigger on document_versions would have rejected the commit
    outright if evidence weren't populated."""
    org_id, project_id, user, token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await _upload_document(client, token, project_id)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "Booth floor plan"
    assert body["current_version_id"] is not None

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        versions = await client.get(
            f"/projects/{project_id}/documents/{body['id']}/versions", headers=_headers(token)
        )
    assert versions.status_code == 200
    version_list = versions.json()
    assert len(version_list) == 1
    assert version_list[0]["version_no"] == 1
    assert version_list[0]["is_current"] is True
    assert version_list[0]["id"] == body["current_version_id"]


@pytest.mark.asyncio
async def test_spec_claim_confidence_round_trips_through_the_api(app_session, authed_org_and_project):
    """Blind Spots item 4: SpecClaim.confidence — the extraction model's own
    per-claim confidence (app/documents/schema.py's ExtractedSpecClaim) —
    used to be dropped between the model response and the persisted row,
    and even once persisted, no response schema exposed it. Writes a claim
    directly (deterministic, not dependent on a live extraction model, the
    same reasoning tests/test_document_extractor.py's own FakeModelClient
    already applies to the extraction step itself) and asserts the real
    GET endpoint returns the same value, not silently dropping it a second
    time on the way out."""
    org_id, project_id, _user, token = authed_org_and_project
    await set_org_context(app_session, org_id)

    document = Document(project_id=project_id, name="Graphic list.pdf")
    app_session.add(document)
    await app_session.flush()
    version = DocumentVersion(
        document_id=document.id, version_no=1, storage_ref="documents/x/v1/y",
        extracted_text="Location H: 2040mm x 1040mm graphic panel.",
    )
    app_session.add(version)
    await app_session.flush()
    app_session.add(
        Evidence(
            document_version_id=version.id, channel="manual", sent_at=datetime.now(timezone.utc),
            language="en", original_text="Uploaded via the CUE API.",
        )
    )
    claim = SpecClaim(
        document_version_id=version.id, location_code="H", attribute="dimension",
        value="2040mm x 1040mm", confidence=0.77,
    )
    app_session.add(claim)
    await app_session.flush()
    app_session.add(
        Evidence(
            spec_claim_id=claim.id, channel="manual", sent_at=datetime.now(timezone.utc),
            language="en", original_text="Location H: 2040mm x 1040mm graphic panel.",
            span_start=0, span_end=10,
        )
    )
    await app_session.commit()
    document.current_version_id = version.id
    await app_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/projects/{project_id}/documents/{document.id}/versions/{version.id}/spec-claims",
            headers=_headers(token),
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body) == 1
    assert body[0]["confidence"] == 0.77


@pytest.mark.asyncio
async def test_read_only_member_can_read_but_not_upload(app_session, authed_org_and_project):
    """WRITE_ROLES-gated, same shape every other ledger-mutating endpoint
    uses — read_only is the role that exists specifically to grant
    visibility without write access (tests/test_budget_api.py's own
    read_only test asserts the same property for Budget)."""
    org_id, project_id, admin, _admin_token = authed_org_and_project
    _viewer, viewer_token = await _member(app_session, org_id, project_id, "read_only", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        write = await _upload_document(client, viewer_token, project_id)
        read = await client.get(f"/projects/{project_id}/documents", headers=_headers(viewer_token))

    assert write.status_code == 403
    assert read.status_code == 200


@pytest.mark.asyncio
async def test_add_version_supersedes_and_updates_current_pointer(authed_org_and_project):
    """FR-DOC-02: version lineage — a new version correctly supersedes the
    old one, current_version_id updates."""
    org_id, project_id, user, token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await _upload_document(client, token, project_id)
        document_id = first.json()["id"]
        first_version_id = first.json()["current_version_id"]

        second = await client.post(
            f"/projects/{project_id}/documents/{document_id}/versions",
            headers=_headers(token),
            data={"extracted_text": "Revised: 2100mm x 1040mm graphic panel."},
            files=_upload_files(filename="floor-plan-v2.pdf"),
        )
        document = await client.get(
            f"/projects/{project_id}/documents/{document_id}", headers=_headers(token)
        )
        lineage = await client.get(
            f"/projects/{project_id}/documents/{document_id}/lineage", headers=_headers(token)
        )

    assert second.status_code == 201, second.text
    second_body = second.json()
    assert second_body["version_no"] == 2
    assert second_body["id"] != first_version_id

    assert document.json()["current_version_id"] == second_body["id"]

    lineage_body = lineage.json()
    assert lineage_body["current_version_id"] == second_body["id"]
    assert [v["version_no"] for v in lineage_body["versions"]] == [1, 2]
    by_id = {v["id"]: v for v in lineage_body["versions"]}
    assert by_id[first_version_id]["is_current"] is False
    assert by_id[second_body["id"]]["is_current"] is True


@pytest.mark.asyncio
async def test_approve_version_records_approver_and_writes_back(authed_org_and_project):
    """FR-DOC-02's approval step, plus FR-DOC-07's write-back trigger (the
    no-op SharePointAdapter — real Graph-backed write-back is Prompt 11's
    job, see app/documents/sharepoint.py)."""
    org_id, project_id, user, token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await _upload_document(client, token, project_id)
        document_id = created.json()["id"]
        version_id = created.json()["current_version_id"]

        response = await client.post(
            f"/projects/{project_id}/documents/{document_id}/versions/{version_id}/approve",
            headers=_headers(token),
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["approved_by"] == str(user.id)
    assert body["approved_at"] is not None


@pytest.mark.asyncio
async def test_tag_document_resolves_ontology_codes(authed_org_and_project):
    """FR-DOC-03: auto-tag into deliverable_class/milestone_type/phase
    ontology terms."""
    org_id, project_id, user, token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await _upload_document(client, token, project_id)
        document_id = created.json()["id"]

        response = await client.post(
            f"/projects/{project_id}/documents/{document_id}/tag",
            headers=_headers(token),
            json={
                "class_code": "led_screen",
                "milestone_type_code": "install",
                "phase_code": "fabrication",
            },
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["class_term_id"] is not None
    assert body["milestone_type_term_id"] is not None
    assert body["phase_term_id"] is not None


@pytest.mark.asyncio
async def test_tag_document_unknown_code_is_422(authed_org_and_project):
    org_id, project_id, user, token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await _upload_document(client, token, project_id)
        document_id = created.json()["id"]

        response = await client.post(
            f"/projects/{project_id}/documents/{document_id}/tag",
            headers=_headers(token),
            json={"class_code": "not_a_real_deliverable_class"},
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_document_with_class_code_at_upload(authed_org_and_project):
    org_id, project_id, user, token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await _upload_document(client, token, project_id, class_code="graphic_panel")

    assert response.status_code == 201, response.text
    assert response.json()["class_term_id"] is not None


@pytest.mark.asyncio
async def test_document_audit_log_reflects_a_real_lifecycle_most_recent_first(authed_org_and_project):
    """The Documents page's own Activity tab surface — real DocumentAuditLog
    rows for one document, across upload, approval and tagging, most-recent
    first. Previously reachable only through /admin/export's whole-project
    bundle."""
    org_id, project_id, user, token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await _upload_document(client, token, project_id)
        document_id = created.json()["id"]
        version_id = created.json()["current_version_id"]

        await client.post(
            f"/projects/{project_id}/documents/{document_id}/versions/{version_id}/approve",
            headers=_headers(token),
        )
        await client.post(
            f"/projects/{project_id}/documents/{document_id}/tag",
            headers=_headers(token),
            json={"class_code": "led_screen"},
        )

        response = await client.get(
            f"/projects/{project_id}/documents/{document_id}/audit-log", headers=_headers(token)
        )

    assert response.status_code == 200, response.text
    rows = response.json()
    actions = [r["action"] for r in rows]
    # The most-recent-first two are unambiguous — approve and tag are two
    # genuinely separate requests, each its own transaction with its own
    # real `occurred_at`. `document_created`/`version_created` are written
    # in the *same* upload transaction, where Postgres's `now()` is fixed
    # for the whole transaction (not per-statement) — both rows land on
    # the identical timestamp, so their relative order is genuinely
    # undefined, not something this endpoint fabricates a false answer for.
    assert actions[:2] == ["auto_tagged", "version_approved"]
    assert set(actions[2:]) == {"document_created", "version_created"}
    assert all(r["document_id"] == document_id for r in rows)
    assert all(r["actor_id"] == str(user.id) for r in rows)

    version_created = next(r for r in rows if r["action"] == "version_created")
    assert version_created["detail"] == {"version_no": 1}
    approved = next(r for r in rows if r["action"] == "version_approved")
    assert approved["detail"] == {"sharepoint_write_back": "ok"}
    tagged = next(r for r in rows if r["action"] == "auto_tagged")
    assert "class_term_id" in tagged["detail"]["changes"]


@pytest.mark.asyncio
async def test_document_audit_log_empty_for_a_freshly_uploaded_but_untouched_document(
    authed_org_and_project,
):
    org_id, project_id, _user, token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await _upload_document(client, token, project_id)
        document_id = created.json()["id"]

        response = await client.get(
            f"/projects/{project_id}/documents/{document_id}/audit-log", headers=_headers(token)
        )

    assert response.status_code == 200, response.text
    # document_created + version_created from the upload itself — never
    # empty, since a document with no history at all can't exist.
    assert {r["action"] for r in response.json()} == {"document_created", "version_created"}


_REAL_MINIMAL_PDF = b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/Resources<</Font<</F1 4 0 R>>>>/MediaBox[0 0 200 200]/Contents 5 0 R>>endobj
4 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj
5 0 obj<</Length 58>>
stream
BT /F1 18 Tf 20 100 Td (Hello CUE PDF test) Tj ET
endstream
endobj
xref
0 6
trailer<</Size 6/Root 1 0 R>>
%%EOF"""


@pytest.mark.asyncio
async def test_upload_without_extracted_text_auto_derives_it_from_a_real_pdf(authed_org_and_project):
    """Prompt 11 item 6: real OCR/parsing, not a caller-supplied stand-in,
    now backs DocumentVersion.extracted_text when the caller doesn't supply
    it — app/documents/service.py's _derive_extracted_text, real pdftotext
    underneath (app/capture/media.py's extract_pdf_text)."""
    org_id, project_id, user, token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/documents",
            headers=_headers(token),
            data={"name": "Real PDF upload"},
            files={"file": ("real.pdf", _REAL_MINIMAL_PDF, "application/pdf")},
        )
    assert response.status_code == 201, response.text

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        search = await client.get(
            f"/projects/{project_id}/documents/search",
            headers=_headers(token),
            params={"q": "CUE"},
        )
    assert search.status_code == 200
    results = search.json()
    assert len(results) == 1
    assert results[0]["document"]["name"] == "Real PDF upload"


@pytest.mark.asyncio
async def test_search_documents_full_text(authed_org_and_project):
    """FR-DOC-05: Postgres full-text search over DocumentVersion.extracted_text."""
    org_id, project_id, user, token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _upload_document(
            client, token, project_id,
            name="Graphic list", extracted_text="LED screen mounted on plywood backing, qty 2 sets.",
        )
        await _upload_document(
            client, token, project_id,
            name="Catering menu", extracted_text="Buffet lunch for 150 guests, halal certified.",
        )

        found = await client.get(
            f"/projects/{project_id}/documents/search",
            headers=_headers(token),
            params={"q": "plywood"},
        )
        missed = await client.get(
            f"/projects/{project_id}/documents/search",
            headers=_headers(token),
            params={"q": "spaceship"},
        )

    assert found.status_code == 200, found.text
    results = found.json()
    assert len(results) == 1
    assert results[0]["document"]["name"] == "Graphic list"

    assert missed.status_code == 200
    assert missed.json() == []


@pytest.mark.asyncio
async def test_documents_isolated_via_project_join_rls(app_session, authed_org_and_project):
    """RLS: a document created under one org's project must be invisible
    once the session's org context switches away — mirrors
    tests/test_rls.py's test_commitments_isolated_via_project_join for the
    same join-based (not direct-column) policy shape."""
    org_id, project_id, user, token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await _upload_document(client, token, project_id)
    document_id = created.json()["id"]

    await set_org_context(app_session, uuid.uuid4())  # a different, unrelated org
    result = await app_session.execute(select(Document).where(Document.id == uuid.UUID(document_id)))
    assert result.scalar_one_or_none() is None
