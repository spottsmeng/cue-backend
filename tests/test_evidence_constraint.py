"""FR-LED-03 / CLAUDE.md: 'No commitment without evidence... enforced at the
DB layer, not trusted.' Codifies the manual psql verification done earlier
for commitments, and extends it to budgets (PRD §4.3 requires the same
NOT NULL evidence rule there), which was never actually tested by hand.

The trigger is DEFERRED to COMMIT, so these tests must genuinely commit (or
observe a commit failure) — a rollback-based test would never fire it.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import DBAPIError

from app.documents.models import Document, DocumentVersion, SpecClaim
from app.ledger.extractor import _get_commitment_act_term
from app.models import Budget, Commitment, Evidence
from tests.conftest import set_org_context


async def _commitment_kwargs(app_session, project_id, vendor, internal):
    act_term = await _get_commitment_act_term(app_session, "confirm")
    return dict(
        project_id=project_id,
        party_id=vendor.id,
        counterparty_id=internal.id,
        act_type_id=act_term.id,
        state="proposed",
        deliverable_en="LED screen install",
        deliverable_original="LED screen install",
        confidence=0.9,
        field_confidence={},
        verification_state="auto",
    )


@pytest.mark.asyncio
async def test_commitment_without_evidence_is_rejected_at_commit(
    app_session, org_and_project, parties
):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)

    commitment = Commitment(**await _commitment_kwargs(app_session, project_id, vendor, internal))
    app_session.add(commitment)
    await app_session.flush()

    with pytest.raises(DBAPIError, match="has no evidence"):
        await app_session.commit()
    await app_session.rollback()


@pytest.mark.asyncio
async def test_commitment_with_evidence_in_same_transaction_succeeds(
    app_session, org_and_project, parties
):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)

    commitment = Commitment(**await _commitment_kwargs(app_session, project_id, vendor, internal))
    app_session.add(commitment)
    await app_session.flush()

    app_session.add(
        Evidence(
            commitment_id=commitment.id,
            channel="whatsapp",
            sent_at=datetime.now(timezone.utc),
            language="en",
            original_text="screens will be up by 2pm",
            span_start=0,
            span_end=10,
        )
    )
    await app_session.commit()  # must not raise

    assert commitment.id is not None


@pytest.mark.asyncio
async def test_budget_without_evidence_is_rejected_at_commit(
    app_session, org_and_project, seeded_user
):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)

    budget = Budget(
        project_id=project_id,
        approved_amount=50000,
        currency="SGD",
        approved_by=seeded_user.id,
        approved_at=datetime.now(timezone.utc),
        is_current=True,
    )
    app_session.add(budget)
    await app_session.flush()

    with pytest.raises(DBAPIError, match="has no evidence"):
        await app_session.commit()
    await app_session.rollback()


@pytest.mark.asyncio
async def test_budget_with_evidence_succeeds(app_session, org_and_project, seeded_user):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)

    budget = Budget(
        project_id=project_id,
        approved_amount=50000,
        currency="SGD",
        approved_by=seeded_user.id,
        approved_at=datetime.now(timezone.utc),
        is_current=True,
    )
    app_session.add(budget)
    await app_session.flush()

    app_session.add(
        Evidence(
            budget_id=budget.id,
            channel="outlook",
            sent_at=datetime.now(timezone.utc),
            language="en",
            original_text="Approved: SGD 50,000 baseline for the Meridian activation.",
            span_start=0,
            span_end=8,
        )
    )
    await app_session.commit()  # must not raise

    assert budget.id is not None


async def _document_kwargs(project_id):
    return dict(project_id=project_id, name="Booth floor plan.pdf")


@pytest.mark.asyncio
async def test_document_version_without_evidence_is_rejected_at_commit(
    app_session, org_and_project
):
    """Prompt 6: DocumentVersion needs 'the same deferred-trigger treatment
    as Commitment/Budget' — DOCUMENT_VERSION's evidence is NOT NULL here,
    even though PRD §4.1's own ER diagram marks the link optional."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)

    document = Document(**await _document_kwargs(project_id))
    app_session.add(document)
    await app_session.flush()

    version = DocumentVersion(document_id=document.id, version_no=1, storage_ref="documents/x/v1/y")
    app_session.add(version)
    await app_session.flush()

    with pytest.raises(DBAPIError, match="has no evidence"):
        await app_session.commit()
    await app_session.rollback()


@pytest.mark.asyncio
async def test_document_version_with_evidence_in_same_transaction_succeeds(
    app_session, org_and_project
):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)

    document = Document(**await _document_kwargs(project_id))
    app_session.add(document)
    await app_session.flush()

    version = DocumentVersion(document_id=document.id, version_no=1, storage_ref="documents/x/v1/y")
    app_session.add(version)
    await app_session.flush()

    app_session.add(
        Evidence(
            document_version_id=version.id,
            channel="manual",
            sent_at=datetime.now(timezone.utc),
            language="en",
            original_text="Uploaded via the CUE API.",
        )
    )
    await app_session.commit()  # must not raise

    assert version.id is not None


@pytest.mark.asyncio
async def test_spec_claim_without_evidence_is_rejected_at_commit(app_session, org_and_project):
    """PRD §4.3: SpecClaim's evidence is NOT NULL — 'same rule as
    Commitment/Budget; sourced from the document version itself'."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)

    document = Document(**await _document_kwargs(project_id))
    app_session.add(document)
    await app_session.flush()
    version = DocumentVersion(
        document_id=document.id, version_no=1, storage_ref="documents/x/v1/y",
        extracted_text="Location H: 2040mm x 1040mm graphic panel, qty 1 set.",
    )
    app_session.add(version)
    await app_session.flush()
    app_session.add(
        Evidence(
            document_version_id=version.id, channel="manual",
            sent_at=datetime.now(timezone.utc), language="en", original_text="Uploaded via the CUE API.",
        )
    )
    await app_session.commit()

    await set_org_context(app_session, org_id)
    claim = SpecClaim(
        document_version_id=version.id, attribute="dimension", value="2040mm x 1040mm",
    )
    app_session.add(claim)
    await app_session.flush()

    with pytest.raises(DBAPIError, match="has no evidence"):
        await app_session.commit()
    await app_session.rollback()


@pytest.mark.asyncio
async def test_spec_claim_with_evidence_succeeds(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)

    document = Document(**await _document_kwargs(project_id))
    app_session.add(document)
    await app_session.flush()
    text = "Location H: 2040mm x 1040mm graphic panel, qty 1 set."
    version = DocumentVersion(
        document_id=document.id, version_no=1, storage_ref="documents/x/v1/y", extracted_text=text,
    )
    app_session.add(version)
    await app_session.flush()
    app_session.add(
        Evidence(
            document_version_id=version.id, channel="manual",
            sent_at=datetime.now(timezone.utc), language="en", original_text="Uploaded via the CUE API.",
        )
    )
    await app_session.commit()

    await set_org_context(app_session, org_id)
    claim = SpecClaim(document_version_id=version.id, attribute="dimension", value="2040mm x 1040mm")
    app_session.add(claim)
    await app_session.flush()

    span_start = text.index("2040mm x 1040mm")
    app_session.add(
        Evidence(
            spec_claim_id=claim.id, channel="manual", sent_at=datetime.now(timezone.utc),
            language="en", original_text=text, span_start=span_start, span_end=span_start + 16,
        )
    )
    await app_session.commit()  # must not raise

    assert claim.id is not None
