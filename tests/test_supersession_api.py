"""FR-LED-05: app/ledger/supersession.py's propose/confirm/reject lifecycle,
plus its /projects/{id}/commitments/supersession-candidates API surface.
Real Postgres throughout (tests/conftest.py's app_session), a fake LLM
client injected at the service boundary (same shape tests/
test_writeback_api.py's FakeJSONClient establishes, patched at
app.ledger.supersession.get_client — the seam that module's own namespace
imports it into) so this suite needs no live Ollama/Anthropic.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.identity.config import get_identity_settings
from app.identity.models import Membership, User
from app.ledger.extractor import _get_commitment_act_term
from app.ledger.supersession import (
    confirm_supersession_candidate,
    find_candidate_priors,
    propose_supersession_candidates,
    reject_supersession_candidate,
)
from app.models import Commitment, CommitmentSupersessionCandidate, Evidence
from main import app
from tests.conftest import FAKE_LLM_USAGE, mint_token, set_org_context

NOW = datetime.now(timezone.utc)


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class FakeJSONClient:
    def __init__(self, response: dict | str):
        self.response = response
        self.calls = 0

    async def complete(self, prompt: str, schema: dict):
        self.calls += 1
        body = self.response if isinstance(self.response, str) else json.dumps(self.response)
        return body, FAKE_LLM_USAGE


async def _member(app_session, org_id, project_id, role, granted_by):
    await set_org_context(app_session, org_id)
    subject = f"{role}-{uuid.uuid4()}"
    user = User(
        organisation_id=org_id, issuer=get_identity_settings().local_issuer,
        external_subject=subject, email=f"{subject}@example.test",
    )
    app_session.add(user)
    await app_session.flush()
    app_session.add(Membership(user_id=user.id, project_id=project_id, role=role, granted_by=granted_by))
    await app_session.commit()
    return user, mint_token(org_id, subject=subject, email=user.email)


async def _make_commitment(
    app_session, org_id, project_id, party_id, counterparty_id, *,
    deliverable_en="LED wall rental", amount=None, currency=None, created_at=None,
) -> Commitment:
    # Re-asserted on every call, not just once at the top of each test —
    # app_session's own conftest.py docstring documents why: this fixture's
    # AsyncSession can silently check out a *different* physical pooled
    # connection after each commit(), and `app.current_org_id` is set
    # per-connection (session-scoped GUC), not per-AsyncSession. A helper
    # that commits internally, possibly several times per test, needs to
    # re-affirm this before every unit of work rather than relying on
    # whichever connection happened to be live when the test's own single
    # top-of-function call ran — a real, reproducible failure this file's
    # own development caught under full-suite load (many prior tests'
    # org_ids cycling through the pool), not a hypothetical.
    await set_org_context(app_session, org_id)
    act_term = await _get_commitment_act_term(app_session, "commit")
    kwargs = {} if created_at is None else {"created_at": created_at}
    commitment = Commitment(
        project_id=project_id, party_id=party_id, counterparty_id=counterparty_id,
        act_type_id=act_term.id, state="committed", deliverable_en=deliverable_en,
        amount=amount, currency=currency, confidence=1.0, verification_state="human_verified",
        # `created_at` is server_default=func.now() — passing it explicitly
        # here overrides that default for this one INSERT, the plain
        # SQLAlchemy way to control ordering deterministically for
        # find_candidate_priors's own "recorded strictly before" filter,
        # rather than a second UPDATE statement after the fact.
        **kwargs,
    )
    app_session.add(commitment)
    await app_session.flush()
    app_session.add(
        Evidence(
            commitment_id=commitment.id, channel="whatsapp", sent_at=NOW,
            language="en", original_text="ok, confirmed",
        )
    )
    await app_session.flush()
    await app_session.commit()
    return commitment


# --- find_candidate_priors ---------------------------------------------


@pytest.mark.asyncio
async def test_find_candidate_priors_matches_same_party_and_deliverable(
    app_session, org_and_project, parties
):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)

    older = await _make_commitment(
        app_session, org_id, project_id, vendor.id, internal.id,
        deliverable_en="LED wall rental", created_at=NOW - timedelta(days=5),
    )
    newer = await _make_commitment(
        app_session, org_id, project_id, vendor.id, internal.id,
        deliverable_en="LED wall rental", created_at=NOW,
    )
    # A different deliverable — must not match.
    await _make_commitment(
        app_session, org_id, project_id, vendor.id, internal.id,
        deliverable_en="Stage rigging", created_at=NOW - timedelta(days=3),
    )

    priors = await find_candidate_priors(app_session, newer)
    assert [p.id for p in priors] == [older.id]


@pytest.mark.asyncio
async def test_find_candidate_priors_excludes_a_different_party(app_session, org_and_project, parties):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)

    from app.models import Party

    other_vendor = Party(organisation_id=org_id, display_name="Other Vendor", type="vendor_org")
    app_session.add(other_vendor)
    await app_session.flush()
    await app_session.commit()

    await _make_commitment(
        app_session, org_id, project_id, other_vendor.id, internal.id,
        deliverable_en="LED wall rental", created_at=NOW - timedelta(days=5),
    )
    newer = await _make_commitment(
        app_session, org_id, project_id, vendor.id, internal.id,
        deliverable_en="LED wall rental", created_at=NOW,
    )

    priors = await find_candidate_priors(app_session, newer)
    assert priors == []


@pytest.mark.asyncio
async def test_find_candidate_priors_respects_the_limit(app_session, org_and_project, parties):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)

    for i in range(5):
        await _make_commitment(
        app_session, org_id, project_id, vendor.id, internal.id,
            deliverable_en="Staffing — day crew", created_at=NOW - timedelta(days=10 - i),
        )
    newer = await _make_commitment(
        app_session, org_id, project_id, vendor.id, internal.id,
        deliverable_en="Staffing — day crew", created_at=NOW,
    )

    priors = await find_candidate_priors(app_session, newer)
    assert len(priors) == 3  # _CANDIDATE_LIMIT


# --- propose_supersession_candidates ------------------------------------


@pytest.mark.asyncio
async def test_propose_creates_no_row_and_makes_no_llm_call_with_no_candidates(
    app_session, org_and_project, parties
):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)
    commitment = await _make_commitment(app_session, org_id, project_id, vendor.id, internal.id)

    fake = FakeJSONClient({"supersedes": True, "reasoning": "n/a"})
    created = await propose_supersession_candidates(app_session, commitment, client=fake)

    assert created == []
    assert fake.calls == 0  # the cheap SQL search short-circuits before ever reaching the model


@pytest.mark.asyncio
async def test_propose_writes_a_pending_candidate_on_a_yes_verdict(app_session, org_and_project, parties):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)

    older = await _make_commitment(
        app_session, org_id, project_id, vendor.id, internal.id, deliverable_en="LED wall rental",
        amount=18500, currency="SGD", created_at=NOW - timedelta(days=5),
    )
    newer = await _make_commitment(
        app_session, org_id, project_id, vendor.id, internal.id, deliverable_en="LED wall rental",
        amount=20000, currency="SGD", created_at=NOW,
    )

    fake = FakeJSONClient({"supersedes": True, "reasoning": "Same deliverable, price increased from 18500 to 20000."})
    created = await propose_supersession_candidates(app_session, newer, client=fake)
    await app_session.commit()

    assert fake.calls == 1
    assert len(created) == 1
    assert created[0].commitment_id == newer.id
    assert created[0].supersedes_commitment_id == older.id
    assert created[0].status == "pending"
    assert "18500" in created[0].reasoning

    row = (
        await app_session.execute(
            select(CommitmentSupersessionCandidate).where(CommitmentSupersessionCandidate.commitment_id == newer.id)
        )
    ).scalar_one()
    assert row.status == "pending"


@pytest.mark.asyncio
async def test_propose_writes_nothing_on_a_no_verdict(app_session, org_and_project, parties):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)

    await _make_commitment(
        app_session, org_id, project_id, vendor.id, internal.id, deliverable_en="Staffing — day crew",
        created_at=NOW - timedelta(days=5),
    )
    newer = await _make_commitment(
        app_session, org_id, project_id, vendor.id, internal.id, deliverable_en="Staffing — day crew", created_at=NOW,
    )

    fake = FakeJSONClient({"supersedes": False, "reasoning": "Two separate, unrelated staffing bookings."})
    created = await propose_supersession_candidates(app_session, newer, client=fake)

    assert created == []
    rows = (
        await app_session.execute(
            select(CommitmentSupersessionCandidate).where(CommitmentSupersessionCandidate.commitment_id == newer.id)
        )
    ).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_propose_skips_a_malformed_response_without_raising(app_session, org_and_project, parties):
    """A candidate proposal failing must never block commitment creation
    itself — this is called inline from extract_case/create_commitment."""
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)

    await _make_commitment(
        app_session, org_id, project_id, vendor.id, internal.id, deliverable_en="LED wall rental",
        created_at=NOW - timedelta(days=5),
    )
    newer = await _make_commitment(
        app_session, org_id, project_id, vendor.id, internal.id, deliverable_en="LED wall rental", created_at=NOW,
    )

    fake = FakeJSONClient("not valid json at all")
    created = await propose_supersession_candidates(app_session, newer, client=fake)
    assert created == []


# --- confirm / reject ----------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_sets_supersedes_and_recomputes_vendor_metrics(app_session, org_and_project, parties):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)

    older = await _make_commitment(
        app_session, org_id, project_id, vendor.id, internal.id, deliverable_en="LED wall rental",
        amount=18500, currency="SGD", created_at=NOW - timedelta(days=5),
    )
    newer = await _make_commitment(
        app_session, org_id, project_id, vendor.id, internal.id, deliverable_en="LED wall rental",
        amount=20000, currency="SGD", created_at=NOW,
    )
    fake = FakeJSONClient({"supersedes": True, "reasoning": "price revised"})
    [candidate] = await propose_supersession_candidates(app_session, newer, client=fake)
    await app_session.commit()

    admin_id = uuid.uuid4()  # reviewed_by is FK'd to users, but no user row check is enforced app-side
    user = User(
        organisation_id=org_id, issuer=get_identity_settings().local_issuer,
        external_subject=f"reviewer-{admin_id}", email=f"reviewer-{admin_id}@example.test",
    )
    app_session.add(user)
    await app_session.flush()
    await app_session.commit()

    confirmed = await confirm_supersession_candidate(app_session, candidate=candidate, actor_id=user.id)
    await app_session.commit()

    assert confirmed.status == "confirmed"
    assert confirmed.reviewed_by == user.id
    assert confirmed.reviewed_at is not None

    refreshed = (await app_session.execute(select(Commitment).where(Commitment.id == newer.id))).scalar_one()
    assert older.id in refreshed.supersedes

    from app.parties.reliability import get_reliability_metrics

    metrics = await get_reliability_metrics(app_session, vendor.id)
    assert metrics["revision_churn"].available is True
    assert metrics["price_drift_pct"].available is True


@pytest.mark.asyncio
async def test_reject_leaves_supersedes_untouched(app_session, org_and_project, parties):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)

    older = await _make_commitment(
        app_session, org_id, project_id, vendor.id, internal.id, deliverable_en="LED wall rental",
        created_at=NOW - timedelta(days=5),
    )
    newer = await _make_commitment(
        app_session, org_id, project_id, vendor.id, internal.id, deliverable_en="LED wall rental", created_at=NOW,
    )
    fake = FakeJSONClient({"supersedes": True, "reasoning": "looks related"})
    [candidate] = await propose_supersession_candidates(app_session, newer, client=fake)
    await app_session.commit()

    user = User(
        organisation_id=org_id, issuer=get_identity_settings().local_issuer,
        external_subject=f"reviewer-{uuid.uuid4()}", email=f"reviewer-{uuid.uuid4()}@example.test",
    )
    app_session.add(user)
    await app_session.flush()
    await app_session.commit()

    rejected = await reject_supersession_candidate(app_session, candidate=candidate, actor_id=user.id)
    await app_session.commit()

    assert rejected.status == "rejected"
    refreshed = (await app_session.execute(select(Commitment).where(Commitment.id == newer.id))).scalar_one()
    assert older.id not in refreshed.supersedes


# --- API surface ----------------------------------------------------------


@pytest.mark.asyncio
async def test_create_commitment_via_http_proposes_a_real_candidate(app_session, authed_org_and_project, parties):
    org_id, project_id, admin, admin_token = authed_org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)
    await _make_commitment(
        app_session, org_id, project_id, vendor.id, internal.id, deliverable_en="LED wall rental",
        amount=18500, currency="SGD", created_at=NOW - timedelta(days=5),
    )

    transport = ASGITransport(app=app)
    with patch(
        "app.ledger.supersession.get_client",
        return_value=FakeJSONClient({"supersedes": True, "reasoning": "price revised"}),
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            create = await client.post(
                f"/projects/{project_id}/commitments",
                headers=_headers(admin_token),
                json={
                    "party_id": str(vendor.id), "counterparty_id": str(internal.id),
                    "act_type": "renegotiate", "deliverable_en": "LED wall rental",
                    "amount": 20000, "currency": "SGD",
                },
            )
    assert create.status_code == 201, create.text
    new_commitment_id = create.json()["id"]

    candidates = (
        await app_session.execute(
            select(CommitmentSupersessionCandidate).where(
                CommitmentSupersessionCandidate.commitment_id == uuid.UUID(new_commitment_id)
            )
        )
    ).scalars().all()
    assert len(candidates) == 1
    assert candidates[0].status == "pending"


@pytest.mark.asyncio
async def test_list_confirm_reject_through_http(app_session, authed_org_and_project, parties):
    org_id, project_id, admin, admin_token = authed_org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)

    older = await _make_commitment(
        app_session, org_id, project_id, vendor.id, internal.id, deliverable_en="LED wall rental",
        created_at=NOW - timedelta(days=5),
    )
    newer = await _make_commitment(
        app_session, org_id, project_id, vendor.id, internal.id, deliverable_en="LED wall rental", created_at=NOW,
    )
    fake = FakeJSONClient({"supersedes": True, "reasoning": "looks like a revision"})
    [candidate] = await propose_supersession_candidates(app_session, newer, client=fake)
    await app_session.commit()

    _read_only, read_only_token = await _member(app_session, org_id, project_id, "read_only", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get(
            f"/projects/{project_id}/commitments/supersession-candidates",
            headers=_headers(read_only_token),
        )
        assert listed.status_code == 200, listed.text
        assert len(listed.json()) == 1
        assert listed.json()[0]["status"] == "pending"

        # A read-only member can see it, but not confirm it.
        denied = await client.post(
            f"/projects/{project_id}/commitments/supersession-candidates/{candidate.id}/confirm",
            headers=_headers(read_only_token),
        )
        assert denied.status_code == 403

        confirmed = await client.post(
            f"/projects/{project_id}/commitments/supersession-candidates/{candidate.id}/confirm",
            headers=_headers(admin_token),
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["status"] == "confirmed"

        # Already confirmed — confirming (or rejecting) again is a 409, not a silent no-op.
        double_confirm = await client.post(
            f"/projects/{project_id}/commitments/supersession-candidates/{candidate.id}/confirm",
            headers=_headers(admin_token),
        )
        assert double_confirm.status_code == 409


@pytest.mark.asyncio
async def test_supersession_candidates_are_isolated_via_project_join_rls(
    app_session, org_and_project, parties
):
    """Same direct-query RLS check test_writeback_api.py's own
    test_outbound_messages_are_isolated_via_project_join_rls already
    establishes for OutboundMessage — switch `app.current_org_id` to an
    unrelated org and confirm the row genuinely disappears, not just that a
    caller without the right token gets refused at the API layer."""
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)

    older = await _make_commitment(
        app_session, org_id, project_id, vendor.id, internal.id, deliverable_en="LED wall rental",
        created_at=NOW - timedelta(days=5),
    )
    newer = await _make_commitment(
        app_session, org_id, project_id, vendor.id, internal.id, deliverable_en="LED wall rental", created_at=NOW,
    )
    fake = FakeJSONClient({"supersedes": True, "reasoning": "x"})
    [candidate] = await propose_supersession_candidates(app_session, newer, client=fake)
    await app_session.commit()

    await set_org_context(app_session, uuid.uuid4())  # a different, unrelated org
    result = await app_session.execute(
        select(CommitmentSupersessionCandidate).where(CommitmentSupersessionCandidate.id == candidate.id)
    )
    assert result.scalar_one_or_none() is None
