"""Living WIP report (PRD §6.10 FR-RPT, Prompt 8): /projects/{id}/report —
current · export(pptx|pdf) · snapshots · schedule. Through the real ASGI app
and the real composer/service functions against the real test database, same
"against real infrastructure, not mocks" posture every other test module in
this suite follows (tests/conftest.py's own docstring).

Covers this session's own testing expectation, item by item: budget-summary
math against a known set of commitment states (the exact four-field
grounding rule, not an approximation of it), export-blocked-until-verified
as an explicit 409 naming the right commitment, snapshot immutability
(exporting twice produces two distinct, independently-readable, and
DB-enforced-immutable snapshots), and RLS + role-gating as two independent
properties.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.identity.config import get_identity_settings
from app.identity.models import Membership, User
from app.ledger.extractor import _get_commitment_act_term
from app.models import AuditLog, Budget, Commitment, Evidence, Project
from app.reports.composer import compute_budget_summary
from app.reports.models import ReportSnapshot
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


async def _get_project(app_session, project_id) -> Project:
    return (
        await app_session.execute(select(Project).where(Project.id == project_id))
    ).scalar_one()


async def _make_budget(app_session, project_id, approved_by, *, approved_amount=100_000, currency="SGD") -> Budget:
    budget = Budget(
        project_id=project_id,
        approved_amount=approved_amount,
        currency=currency,
        approved_by=approved_by,
        approved_at=datetime.now(timezone.utc),
        is_current=True,
    )
    app_session.add(budget)
    await app_session.flush()
    app_session.add(
        Evidence(
            budget_id=budget.id,
            channel="manual",
            sent_at=datetime.now(timezone.utc),
            language="en",
            original_text="Budget baseline recorded for a report test.",
        )
    )
    await app_session.flush()
    return budget


async def _make_commitment(
    app_session, project_id, vendor, internal, *,
    state="committed", amount=None, payment_status=None, verification_state="human_verified",
    deliverable_en="LED screen install",
) -> Commitment:
    act_term = await _get_commitment_act_term(app_session, "confirm")
    commitment = Commitment(
        project_id=project_id,
        party_id=vendor.id,
        counterparty_id=internal.id,
        act_type_id=act_term.id,
        state=state,
        deliverable_en=deliverable_en,
        deliverable_original=deliverable_en,
        amount=amount,
        currency="SGD" if amount is not None else None,
        payment_status=payment_status,
        confidence=0.9,
        field_confidence={},
        verification_state=verification_state,
    )
    app_session.add(commitment)
    await app_session.flush()
    app_session.add(
        Evidence(
            commitment_id=commitment.id,
            channel="whatsapp",
            sent_at=datetime.now(timezone.utc),
            language="en",
            original_text=f"{deliverable_en} — evidence for a report test.",
            span_start=0,
            span_end=10,
        )
    )
    await app_session.flush()
    return commitment


# --- budget-summary math ---------------------------------------------------


@pytest.mark.asyncio
async def test_budget_summary_math_against_known_commitment_states(
    app_session, authed_org_and_project, parties
):
    """§6.10's own paragraph, verbatim: approved budget from the Budget
    baseline; committed spend = sum(amount) over committed/at_risk/delivered;
    outstanding payments = sum(amount) over payment_status != 'paid' (no
    state filter — the PRD's own wording has none); variance = approved -
    committed. Six commitments, each exercising a different corner of that
    rule."""
    org_id, project_id, admin, _admin_token = authed_org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)

    await _make_budget(app_session, project_id, admin.id, approved_amount=100_000)

    # c1: committed, paid — counts toward committed_spend, excluded from
    # outstanding_payments (the one state payment_status literally excludes).
    c1 = await _make_commitment(
        app_session, project_id, vendor, internal,
        state="committed", amount=20_000, payment_status="paid", verification_state="human_verified",
    )
    # c2: at_risk, invoiced — counts toward both.
    await _make_commitment(
        app_session, project_id, vendor, internal,
        state="at_risk", amount=15_000, payment_status="invoiced", verification_state="human_verified",
    )
    # c3: delivered, payment_status never set (NULL) — counts toward both;
    # outstanding_payments' "not paid" includes NULL (IS DISTINCT FROM).
    await _make_commitment(
        app_session, project_id, vendor, internal,
        state="delivered", amount=5_000, payment_status=None, verification_state="auto",
    )
    # c4: proposed (not a committed-spend state), payment_status NULL —
    # excluded from committed_spend, included in outstanding_payments;
    # pending_verification, so it's expected to block export.
    c4 = await _make_commitment(
        app_session, project_id, vendor, internal,
        state="proposed", amount=3_000, payment_status=None, verification_state="pending_verification",
    )
    # c5: withdrawn, unpaid — excluded from committed_spend (not a
    # committed-spend state), included in outstanding_payments (the PRD's
    # own wording has no state carve-out for that field).
    await _make_commitment(
        app_session, project_id, vendor, internal,
        state="withdrawn", amount=1_000, payment_status="unpaid", verification_state="human_verified",
    )
    # c6: committed, payment_status NULL, pending_verification — counts
    # toward both sums, and is a second export-blocking commitment.
    c6 = await _make_commitment(
        app_session, project_id, vendor, internal,
        state="committed", amount=2_000, payment_status=None, verification_state="pending_verification",
    )
    await app_session.commit()

    project = await _get_project(app_session, project_id)
    section, blocking = await compute_budget_summary(app_session, project)

    assert section.approved_budget.available is True
    assert section.approved_budget.value == 100_000.0
    assert section.approved_budget.provenance[0].source_type == "budget"

    # committed_spend: c1 + c2 + c3 + c6 = 20000+15000+5000+2000
    assert section.committed_spend.value == 42_000.0
    committed_ids = {p.source_id for p in section.committed_spend.provenance}
    assert c1.id in committed_ids and c6.id in committed_ids

    # outstanding_payments: c2 + c3 + c4 + c5 + c6 = 15000+5000+3000+1000+2000
    assert section.outstanding_payments.value == 26_000.0
    outstanding_ids = {p.source_id for p in section.outstanding_payments.provenance}
    assert c1.id not in outstanding_ids  # paid — the one commitment excluded
    assert c4.id in outstanding_ids and c6.id in outstanding_ids

    # variance = approved_budget - committed_spend = 100000 - 42000
    assert section.variance.available is True
    assert section.variance.value == 58_000.0

    # blocking: every pending_verification commitment feeding either sum.
    assert {c.id for c in blocking} == {c4.id, c6.id}

    # An aggregate touched by a pending_verification commitment is flagged
    # at the field level too (FR-RPT-06's "visually distinct").
    assert section.committed_spend.verification_state == "pending_verification"
    assert section.outstanding_payments.verification_state == "pending_verification"


@pytest.mark.asyncio
async def test_budget_summary_unavailable_without_a_baseline(app_session, org_and_project):
    """P2 / §8.4: no budget row exists yet — approved_budget and variance
    are reported structurally unavailable, not zero or omitted."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = await _get_project(app_session, project_id)

    section, blocking = await compute_budget_summary(app_session, project)

    assert section.approved_budget.available is False
    assert section.approved_budget.value is None
    assert section.approved_budget.unavailable_reason
    assert section.variance.available is False
    assert blocking == []
    # committed/outstanding sums are still resolvable (0 over an empty set).
    assert section.committed_spend.available is True
    assert section.committed_spend.value == 0.0


# --- export verification gate (FR-RPT-06) -----------------------------


@pytest.mark.asyncio
async def test_export_blocked_names_the_pending_commitment(
    app_session, authed_org_and_project, parties
):
    org_id, project_id, admin, admin_token = authed_org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)
    await _make_budget(app_session, project_id, admin.id)
    blocker = await _make_commitment(
        app_session, project_id, vendor, internal,
        state="committed", amount=5_000, payment_status=None, verification_state="pending_verification",
    )
    await app_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/report/export",
            headers=_headers(admin_token),
            params={"format": "pptx"},
        )

    assert response.status_code == 409, response.text
    body = response.json()["detail"]
    blocking_ids = {c["commitment_id"] for c in body["blocking_commitments"]}
    assert str(blocker.id) in blocking_ids
    assert body["blocking_commitments"][0]["reason"] == "pending_verification"


# --- snapshot immutability (FR-RPT-08) ---------------------------------


@pytest.mark.asyncio
async def test_export_creates_two_distinct_immutable_snapshots(
    app_session, authed_org_and_project, parties
):
    org_id, project_id, admin, admin_token = authed_org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)
    await _make_budget(app_session, project_id, admin.id)
    await _make_commitment(
        app_session, project_id, vendor, internal,
        state="committed", amount=5_000, payment_status="paid", verification_state="human_verified",
    )
    await app_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            f"/projects/{project_id}/report/export",
            headers=_headers(admin_token), params={"format": "pptx"},
        )
        second = await client.post(
            f"/projects/{project_id}/report/export",
            headers=_headers(admin_token), params={"format": "pptx"},
        )
        listed = await client.get(
            f"/projects/{project_id}/report/snapshots", headers=_headers(admin_token)
        )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    first_body, second_body = first.json(), second.json()
    assert first_body["id"] != second_body["id"]
    assert first_body["download_url"] != second_body["download_url"]  # distinct storage keys

    snapshots = listed.json()
    assert {s["id"] for s in snapshots} >= {first_body["id"], second_body["id"]}

    # Independently readable: each snapshot's own detail endpoint returns
    # its own frozen report_json.
    transport2 = ASGITransport(app=app)
    async with AsyncClient(transport=transport2, base_url="http://test") as client:
        detail = await client.get(
            f"/projects/{project_id}/report/snapshots/{first_body['id']}",
            headers=_headers(admin_token),
        )
    assert detail.status_code == 200
    assert detail.json()["report_json"]["project_id"] == str(project_id)

    # DB-enforced immutability — not just absent application code. A direct
    # UPDATE against the RLS-enforced app role must still be rejected by the
    # migration's own trigger, firing immediately on execute (same shape
    # tests/test_audit_log.py's own append-only trigger tests use).
    with pytest.raises(DBAPIError, match="immutable"):
        await app_session.execute(
            text("UPDATE report_snapshots SET template_code = 'tampered' WHERE id = :id"),
            {"id": uuid.UUID(first_body["id"])},
        )
    await app_session.rollback()


@pytest.mark.asyncio
async def test_pdf_export_also_supported(app_session, authed_org_and_project, parties):
    """FR-RPT-07: both PPTX and PDF, over the same one composition path."""
    org_id, project_id, admin, admin_token = authed_org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)
    await _make_budget(app_session, project_id, admin.id)
    await _make_commitment(
        app_session, project_id, vendor, internal,
        state="committed", amount=1_000, payment_status="paid", verification_state="human_verified",
    )
    await app_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/report/export",
            headers=_headers(admin_token), params={"format": "pdf"},
        )

    assert response.status_code == 201, response.text
    assert response.json()["format"] == "pdf"


# --- role-gating and RLS, as two independent properties ------------------


@pytest.mark.asyncio
async def test_project_manager_can_view_but_not_export(app_session, authed_org_and_project, parties):
    """§12.2's "Freeze & Export" is a Producer-owned control — an ordinary
    project_manager can read the current report but not export it."""
    org_id, project_id, admin, _admin_token = authed_org_and_project
    _pm, pm_token = await _member(app_session, org_id, project_id, "project_manager", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        current = await client.get(
            f"/projects/{project_id}/report/current", headers=_headers(pm_token)
        )
        export = await client.post(
            f"/projects/{project_id}/report/export",
            headers=_headers(pm_token), params={"format": "pptx"},
        )

    assert current.status_code == 200, current.text
    assert export.status_code == 403


@pytest.mark.asyncio
async def test_producer_role_can_export(app_session, authed_org_and_project, parties):
    """ADMIN_ROLES = {"administrator", "producer"} — a Producer, not only an
    Administrator, can export."""
    org_id, project_id, admin, _admin_token = authed_org_and_project
    _producer, producer_token = await _member(app_session, org_id, project_id, "producer", admin.id)
    vendor, internal = parties
    await set_org_context(app_session, org_id)
    await _make_budget(app_session, project_id, admin.id)
    await _make_commitment(
        app_session, project_id, vendor, internal,
        state="committed", amount=1_000, payment_status="paid", verification_state="human_verified",
    )
    await app_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/report/export",
            headers=_headers(producer_token), params={"format": "pptx"},
        )
    assert response.status_code == 201, response.text


@pytest.mark.asyncio
async def test_report_snapshots_isolated_via_project_join_rls(app_session, org_and_project, parties):
    """report_snapshots has no organisation_id column of its own — proves
    its policy isolates via the project join, same property
    tests/test_rls.py's test_commitments_isolated_via_project_join already
    establishes for commitments, independent of role-gating."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    snapshot = ReportSnapshot(
        project_id=project_id,
        format="pptx",
        template_code="cue_placeholder",
        storage_ref="reports/isolation-test/report.pptx",
        report_json={"project_id": str(project_id)},
        trigger="manual",
    )
    app_session.add(snapshot)
    await app_session.commit()

    await set_org_context(app_session, uuid.uuid4())  # a different, unrelated org
    result = await app_session.execute(
        select(ReportSnapshot).where(ReportSnapshot.id == snapshot.id)
    )
    assert result.scalar_one_or_none() is None


# --- FR-VRG-04-adjacent: vendor status resolves real reliability data ----


@pytest.mark.asyncio
async def test_vendor_status_reliability_resolves_once_vrg_has_a_metric(
    app_session, authed_org_and_project, parties
):
    """M5's own note (this file's module-level context, backend/PROGRESS.md's
    M5 section) promised vendor_status's reliability field "starts resolving
    real metrics automatically the day M7 adds that module" — this proves
    that's actually true against the real composer code, not just the
    import succeeding. A vendor with a real on-time-rate metric computed
    shows up `available=True` with the real value; reliability_data_available
    flips to True for the whole section."""
    org_id, project_id, admin, admin_token = authed_org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)

    due = datetime.now(timezone.utc) + timedelta(days=1)
    commitment = await _make_commitment(
        app_session, project_id, vendor, internal, state="delivered"
    )
    commitment.due_at = due
    await app_session.flush()
    app_session.add(
        AuditLog(
            project_id=project_id, commitment_id=commitment.id, action="state_transition",
            actor_id=admin.id, from_state="committed", to_state="delivered", occurred_at=due,
        )
    )
    await app_session.commit()

    from app.parties.service import recompute_vendor_metrics

    await recompute_vendor_metrics(app_session, vendor.id)
    await app_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        current = await client.get(
            f"/projects/{project_id}/report/current", headers=_headers(admin_token)
        )

    assert current.status_code == 200, current.text
    section = current.json()["vendor_status"]
    assert section["reliability_data_available"] is True
    vendor_row = next(r for r in section["vendors"] if r["party_id"] == str(vendor.id))
    assert vendor_row["reliability"]["available"] is True
    assert vendor_row["reliability"]["value"] == pytest.approx(1.0)


# --- FR-DEV-05: deviations roll into the risk and issues section --------


@pytest.mark.asyncio
async def test_current_report_risk_and_issues_includes_risks_and_deviations(
    app_session, authed_org_and_project
):
    org_id, project_id, admin, admin_token = authed_org_and_project
    from app.foresight.models import Risk

    await set_org_context(app_session, org_id)
    risk = Risk(
        project_id=project_id,
        source="forecast",
        finding_key=f"forecast:{uuid.uuid4()}",
        severity="high",
        status="open",
        downstream_consequence="Load-in likely delayed by two days.",
    )
    app_session.add(risk)
    await app_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            f"/projects/{project_id}/deviations",
            headers=_headers(admin_token),
            json={
                "class_code": "delay",
                "description_en": "AV vendor slipped two days",
                "original_text": "AV vendor slipped two days",
            },
        )
        current = await client.get(
            f"/projects/{project_id}/report/current", headers=_headers(admin_token)
        )

    assert current.status_code == 200, current.text
    body = current.json()
    section = body["risk_and_issues"]
    assert any(r["risk_id"] == str(risk.id) for r in section["risks"])
    assert len(section["deviations"]) == 1
    assert section["deviations"][0]["status"] == "auto_drafted" or section["deviations"][0]["status"] == "confirmed"

    # All seven sections are present, per FR-RPT-02's fixed list.
    for key in (
        "project_overview", "milestone_tracker", "vendor_status", "budget_summary",
        "risk_and_issues", "decision_and_approval_log", "next_steps",
    ):
        assert key in body


@pytest.mark.asyncio
async def test_decision_log_includes_the_real_correction_detail(
    app_session, authed_org_and_project, parties
):
    """Blind Spots item 7: a commitment correction writes a real before/
    after `changes` diff to AuditLog.detail (app/api/commitments.py's
    verify_commitment) — DecisionLogRow never had anywhere to put it. Same
    detail must show up in the Decision Log report section."""
    org_id, project_id, _admin, admin_token = authed_org_and_project
    vendor, internal = parties

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            f"/projects/{project_id}/commitments",
            headers=_headers(admin_token),
            json={
                "party_id": str(vendor.id),
                "counterparty_id": str(internal.id),
                "act_type": "commit",
                "deliverable_en": "LED screen install",
            },
        )
        assert created.status_code == 201, created.text
        commitment_id = created.json()["id"]

        verified = await client.post(
            f"/projects/{project_id}/commitments/{commitment_id}/verify",
            headers=_headers(admin_token),
            json={"corrections": {"deliverable_en": "LED wall install"}},
        )
        assert verified.status_code == 200, verified.text

        current = await client.get(
            f"/projects/{project_id}/report/current", headers=_headers(admin_token)
        )

    assert current.status_code == 200, current.text
    decisions = current.json()["decision_and_approval_log"]["decisions"]
    corrected = next(d for d in decisions if d["commitment_id"] == commitment_id)
    assert corrected["action"] == "corrected"
    assert corrected["detail"] == {
        "changes": {"deliverable_en": {"before": "LED screen install", "after": "LED wall install"}}
    }


# --- FR-RPT-09/10: schedule config surface --------------------------------


@pytest.mark.asyncio
async def test_schedule_config_crud_role_gated(app_session, authed_org_and_project):
    org_id, project_id, admin, admin_token = authed_org_and_project
    _pm, pm_token = await _member(app_session, org_id, project_id, "project_manager", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.post(
            f"/projects/{project_id}/report/schedule",
            headers=_headers(pm_token),
            json={"day_of_week": 0, "hour_local": 9, "format": "pptx"},
        )
        created = await client.post(
            f"/projects/{project_id}/report/schedule",
            headers=_headers(admin_token),
            json={"day_of_week": 0, "hour_local": 9, "format": "pptx", "template_code": "cue_placeholder"},
        )
        listed = await client.get(
            f"/projects/{project_id}/report/schedule", headers=_headers(pm_token)
        )
        updated = await client.patch(
            f"/projects/{project_id}/report/schedule/{created.json()['id']}",
            headers=_headers(admin_token),
            json={"hour_local": 14},
        )
        deleted = await client.delete(
            f"/projects/{project_id}/report/schedule/{created.json()['id']}",
            headers=_headers(admin_token),
        )

    assert denied.status_code == 403
    assert created.status_code == 201, created.text
    assert created.json()["day_of_week"] == 0
    assert listed.status_code == 200 and len(listed.json()) == 1  # read access for a non-producer role
    assert updated.status_code == 200 and updated.json()["hour_local"] == 14
    assert deleted.status_code == 204


@pytest.mark.asyncio
async def test_scheduled_runner_generates_a_scheduled_snapshot(
    app_session, authed_org_and_project, parties
):
    """FR-RPT-09: a due schedule produces a real, immutable snapshot with
    `trigger='scheduled'` — the same export_report path a manual export
    uses, just called from app/reports/schedule.py's runner instead of the
    API layer."""
    from zoneinfo import ZoneInfo

    from app.reports.schedule import run_due_report_schedules
    from app.reports.models import ReportScheduleConfig

    org_id, project_id, admin, admin_token = authed_org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)
    await _make_budget(app_session, project_id, admin.id)
    await _make_commitment(
        app_session, project_id, vendor, internal,
        state="committed", amount=1_000, payment_status="paid", verification_state="human_verified",
    )

    project = await _get_project(app_session, project_id)
    now_local = datetime.now(timezone.utc).astimezone(ZoneInfo(project.timezone))
    schedule = ReportScheduleConfig(
        project_id=project_id,
        day_of_week=now_local.weekday(),
        hour_local=now_local.hour,
        minute_local=0,
        format="pptx",
        template_code="cue_placeholder",
        active=True,
        created_by=admin.id,
    )
    app_session.add(schedule)
    await app_session.commit()

    generated = await run_due_report_schedules()
    assert generated >= 1

    await set_org_context(app_session, org_id)
    snapshots = (
        await app_session.execute(
            select(ReportSnapshot).where(
                ReportSnapshot.project_id == project_id, ReportSnapshot.trigger == "scheduled"
            )
        )
    ).scalars().all()
    assert len(snapshots) == 1

    refreshed = (
        await app_session.execute(
            select(ReportScheduleConfig).where(ReportScheduleConfig.id == schedule.id)
        )
    ).scalar_one()
    assert refreshed.last_run_at is not None

    # Running again immediately must not double-fire within the same hour.
    generated_again = await run_due_report_schedules()
    assert generated_again == 0


# --- the review queue is reachable end-to-end ------------------------------


@pytest.mark.asyncio
async def test_linked_price_claim_reaches_the_review_queue_and_names_its_reason(
    app_session, authed_org_and_project
):
    """The trust surface, proven end to end rather than by inspection.

    A vendor message that links to an already-logged commitment and asserts a
    new price is never auto-applied (app/ledger/extractor.py's
    `_attach_evidence_to_existing`). This proves the other half: that
    refusing to apply it does not mean burying it. The commitment has to
    surface in the Living WIP report's outstanding_approvals — the
    "Outstanding Approvals" list a PM actually reads — and the claim itself
    has to be legible in the decision log, not merely present in a table
    nothing renders.

    `outstanding_approvals` had no coverage at all before this: the review
    queue is the one surface the product's "every row is either trustworthy
    or visibly marked" claim rests on.
    """
    from app.ledger.context import load_open_commitment_context
    from app.ledger.extractor import extract_case
    from tests.test_extractor import CONTEXT, FakeModelClient, _item, make_case

    org_id, project_id, admin, admin_token = authed_org_and_project
    await set_org_context(app_session, org_id)

    first = await extract_case(
        app_session, project_id=project_id, organisation_id=org_id, context=CONTEXT,
        case=make_case("screen install confirmed"),
        client=FakeModelClient({"commitments": [_item(evidence_span="screen install")]}),
    )
    await app_session.flush()
    existing = first.created[0]
    assert existing.verification_state == "auto"  # not in the queue yet

    ledger_context = await load_open_commitment_context(app_session, project_id=project_id)
    await extract_case(
        app_session, project_id=project_id, organisation_id=org_id, context=CONTEXT,
        case=make_case("the screen install is now 6200 not 5000"),
        client=FakeModelClient({"commitments": [_item(
            act_type="renegotiate", evidence_span="screen install",
            relates_to="C1", amount=6200, currency="SGD",
        )]}),
        ledger_context=ledger_context,
    )
    await app_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        report = await client.get(
            f"/projects/{project_id}/report/current", headers=_headers(admin_token)
        )
        queue = await client.get(
            f"/projects/{project_id}/commitments?verification_state=pending_verification",
            headers=_headers(admin_token),
        )

    assert report.status_code == 200, report.text
    log = report.json()["decision_and_approval_log"]

    # 1. It is in the queue a PM reads.
    approvals = log["outstanding_approvals"]
    assert str(existing.id) in [a["commitment_id"] for a in approvals]

    # 2. The queue says *why*, rather than just listing the row.
    linked = next(
        d for d in log["decisions"]
        if d["commitment_id"] == str(existing.id) and d["action"] == "evidence_added"
    )
    assert linked["detail"]["unapplied_claims"] == {
        "amount": 6200, "currency": "SGD", "act_type": "renegotiate",
    }
    assert linked["detail"]["flagged_reason"] == "unapplied_claims"

    # 3. The same row is reachable through the list endpoint's own filter.
    assert str(existing.id) in [c["id"] for c in queue.json()]

    # 4. And the price was still never applied.
    assert [c["amount"] for c in queue.json() if c["id"] == str(existing.id)] == [None]
