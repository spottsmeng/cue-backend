#!/usr/bin/env python3
"""Seeds one organisation, one `event-production` project (so F2's Twin work
has a real graph to render), and one User+Membership pair per FR-ADM-01 role
— everything a fresh frontend dev-login flow needs to actually see something,
across every role the UI will eventually gate on.

Why a sibling script here and not an extension of `loadtest/seed.py`: that
script seeds exactly one administrator identity for k6 to hit as fast as
possible — adding an eight-role fan-out to it would bloat a load-test
bootstrap that deliberately stays minimal. This script has a different job
(give a human every role to click through as), so it gets its own file,
following the same direct-ORM approach for the same reason: there is no
public org-creation REST endpoint (a real deployment provisions tenants out
of band — loadtest/seed.py's own module docstring).

Run from backend/ with the app's own venv active and a real Postgres
(docker-compose's `postgres` service) reachable:

    uv run python3 scripts/seed_dev_data.py

Prints the organisation_id and each seeded email to stdout, in a form a
human copies straight into the frontend's `/login` form (which calls the
already-built `POST /auth/dev-login` — see that endpoint's own docstring for
why it trusts organisation_id/email with no credential check: it is gated
hard on CUE_AUTH_PROVIDER=local and 404s otherwise).
"""

import asyncio
import sys
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, text

from app.capture.models import Message
from app.core.db import async_session_factory
from app.documents.models import Document, DocumentVersion, SpecClaim
from app.foresight.deviation import create_manual_deviation, draft_deviation
from app.foresight.models import Notification, Risk
from app.identity.config import get_identity_settings
from app.identity.models import Membership, User
from app.models import (
    Budget,
    Channel,
    ChannelIdentity,
    Commitment,
    Evidence,
    Milestone,
    OntologyTerm,
    Organisation,
    Party,
    Project,
)
from app.models.vertical import Vertical
from app.twin.models import Dependency
from app.twin.service import get_milestone_type_term, materialize_archetype

# FR-ADM-01's full role enum (app/identity/models.py's MembershipRole) — one
# user per role, so every later milestone's prompt can log in as whichever
# role its own surface gates on without extending this script first.
ROLES: list[str] = [
    "project_manager",
    "producer",
    "finance",
    "account_manager",
    "designer",
    "administrator",
    "delegate",
    "read_only",
]


async def _set_org_context(session, org_id: uuid.UUID) -> None:
    # is_local=false (session-wide): this script holds one session for the
    # whole seed, same reasoning scripts/extract_fixtures.py's own call gives.
    await session.execute(
        text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)}
    )


async def main() -> None:
    settings = get_identity_settings()

    async with async_session_factory() as session:
        vertical_id = (
            await session.execute(select(Vertical.id).where(Vertical.code == "event-production"))
        ).scalar_one()

        org_id, project_id = uuid.uuid4(), uuid.uuid4()
        await _set_org_context(session, org_id)

        session.add(Organisation(id=org_id, name="CUE Dev Org"))
        await session.flush()

        now = datetime.now(dt_timezone.utc)

        project = Project(
            id=project_id,
            organisation_id=org_id,
            vertical_id=vertical_id,
            name="CUE Dev Project",
            client_name="Dev Client",
            venue="Dev Venue",
            timezone="Asia/Singapore",
            # F2 enablement: materialize_archetype below only stamps a real
            # `planned_at` on every seeded Milestone when event_start is set
            # (its own docstring: "If project.event_start isn't set yet,
            # milestones are still created... with planned_at=None"). Without
            # this, every node's earliest/latest/slack in TwinCurrentOut
            # would be None and the Twin surface would have nothing to
            # render — this was a real gap this session found and closed
            # (event_start was never set here before F2), not a pre-existing
            # deliberate choice. ~90 days out covers the archetype's own
            # earliest anchor (fnb_confirmation, day_offset -86).
            event_start=now + timedelta(days=90),
        )
        session.add(project)
        await session.flush()

        # FR-TWN-02: give F2's Twin work a real graph, not an empty project —
        # same call app/api/projects.py's create_project makes inline.
        # "event-production-default" (seed_data/event_production_archetype.py's
        # ARCHETYPE_CODE) is the actual archetype row's code; "event-production"
        # alone is the *vertical* code and 422s here as an unknown archetype.
        archetype_milestones = await materialize_archetype(session, project, "event-production-default")

        # F2 enablement: the archetype itself is honestly a linear chain
        # (seed_data/event_production_archetype.py's own docstring: "Annex A
        # gives us an ordered schedule, not a branching dependency graph").
        # F2's own TESTING EXPECTATION asks for "a couple of parallel
        # branches, at least one fixed node" to make critical-path/slack
        # rendering meaningfully testable — `doors` already covers the fixed
        # node, so this adds one real fork/join around the existing
        # load-in -> rigging -> install -> exhibitor-check-in run: a second,
        # slower path (a generator delivery, 5 days versus the existing
        # path's 1) that becomes the actual critical path, pushing rigging/
        # install/exhibitor-check-in onto real, non-zero slack. Added here
        # (a project-level Dependency, per app/twin/models.py's Dependency
        # docstring) rather than edited into the shared archetype template
        # itself — this is one dev project's own fixture, not a change to
        # what every future project seeds from.
        milestones_by_name = {m.name: m for m in archetype_milestones}
        load_in = milestones_by_name["Exhibits move in"]
        content_load = milestones_by_name["Content load into screens"]
        generator_type_term = await get_milestone_type_term(session, project, "rigging")
        generator_delivery = Milestone(
            project_id=project.id,
            type_term_id=generator_type_term.id,
            name="Backup generator delivery",
            planned_at=load_in.planned_at + timedelta(days=5) if load_in.planned_at else None,
            is_fixed=False,
        )
        session.add(generator_delivery)
        await session.flush()
        session.add_all(
            [
                Dependency(
                    project_id=project.id,
                    upstream_milestone_id=load_in.id,
                    downstream_milestone_id=generator_delivery.id,
                    lag_days=5,
                ),
                Dependency(
                    project_id=project.id,
                    upstream_milestone_id=generator_delivery.id,
                    downstream_milestone_id=content_load.id,
                    lag_days=0,
                ),
            ]
        )
        await session.flush()

        # `POST /auth/dev-login` always mints `subject=body.email` (app/api/
        # auth.py) — never a value this script chooses — and resolve_user
        # (app/identity/service.py) looks an existing user up by
        # `(issuer, external_subject)`, not by email. So a seeded row is
        # only ever *found* by a later dev-login (rather than colliding
        # with it on `users_org_email_key` while trying to insert a
        # "new" user) if `external_subject == email` here too. That in
        # turn means email itself must be globally unique per run, since
        # `(issuer, external_subject)` is a global constraint, not
        # per-organisation — a short suffix derived from this run's own
        # organisation_id keeps every run's emails distinct while staying
        # human-typeable and visibly tied to the organisation_id printed
        # right above them.
        org_suffix = org_id.hex[:8]
        seeded: list[tuple[str, str]] = []
        users_by_role: dict[str, User] = {}
        for role in ROLES:
            email = f"{role}+{org_suffix}@cue.dev"
            user = User(
                organisation_id=org_id,
                issuer=settings.local_issuer,
                external_subject=email,
                email=email,
                display_name=role.replace("_", " ").title(),
            )
            session.add(user)
            await session.flush()
            session.add(
                Membership(user_id=user.id, project_id=project_id, role=role, granted_by=user.id)
            )
            seeded.append((role, email))
            users_by_role[role] = user

        # --- F1 enablement: Living WIP needs real ledger content to render,
        # not an empty-project shell — a pending-verification, monetary,
        # real-capture-backed commitment is what F1's own TESTING
        # EXPECTATION names ("a commitment already sitting in
        # pending_verification to test against... prefer extending the seed
        # over hand-crafting one-off fixtures"). This also gives write-back
        # something real to draft against (FR-WBK-01 needs a captured
        # message's channel/party, not a manually-entered commitment — see
        # app/writeback/service.py's _resolve_writeback_target) and the
        # budget/export-block flow something to actually block on.
        vendor = Party(organisation_id=org_id, display_name="Golden Sound & Light Pte Ltd", type="vendor_org")
        internal = Party(organisation_id=org_id, display_name="Pico Production Team", type="internal_staff")
        session.add_all([vendor, internal])
        await session.flush()

        channel = Channel(project_id=project_id, type="whatsapp", external_ref=f"dev-seed-vendor-group-{org_suffix}")
        session.add(channel)
        await session.flush()
        # `channel_identities` has a UNIQUE(channel_type, external_id) that is
        # global, not per-organisation (app/models/party.py) — a fixed phone
        # number here collides across separate seed runs the same way a fixed
        # user email would, so it gets the same org_suffix treatment.
        vendor_phone = f"+65-6555-{org_suffix[:4]}"
        session.add(
            ChannelIdentity(party_id=vendor.id, channel_type="whatsapp", external_id=vendor_phone)
        )

        act_term = (
            await session.execute(
                select(OntologyTerm).where(
                    OntologyTerm.category == "commitment_act",
                    OntologyTerm.code == "commit",
                    OntologyTerm.vertical_id.is_(None),
                    OntologyTerm.organisation_id.is_(None),
                )
            )
        ).scalar_one()

        # `now` was already computed above, right after the org context was
        # set — reused here rather than a second call, same value either way.

        # Commitment 1: pending_verification, monetary, real-capture evidence
        # — the export-block / verify-end-to-end / write-back-draft case.
        pending_commitment = Commitment(
            project_id=project_id, party_id=vendor.id, counterparty_id=internal.id,
            act_type_id=act_term.id, state="committed",
            deliverable_en="LED wall rental — main stage",
            deliverable_original="LED屏幕租赁 —主舞台",
            due_at=now + timedelta(days=10), amount=18500.00, currency="SGD",
            confidence=0.91, field_confidence={"amount": 0.91, "due_at": 0.88},
            verification_state="pending_verification",
        )
        session.add(pending_commitment)
        await session.flush()

        pending_message_text = (
            "确认了,主舞台LED屏幕租赁总共18500新元,含运输安装,十天后到场"
        )
        pending_message = Message(
            project_id=project_id, channel_id=channel.id,
            external_id=f"dev-seed-msg-{uuid.uuid4()}",
            sender_external_id=vendor_phone, author_party_id=vendor.id,
            sent_at=now, language="zh-Hans", text=pending_message_text,
            payload_hash=f"dev-seed-hash-{uuid.uuid4()}",
        )
        session.add(pending_message)
        await session.flush()
        session.add(
            Evidence(
                commitment_id=pending_commitment.id, message_id=pending_message.id,
                channel="whatsapp", sent_at=now, language="zh-Hans",
                original_text=pending_message_text,
                translation=(
                    "Confirmed — LED wall rental for the main stage, total SGD 18,500 including "
                    "delivery and installation, arriving in ten days."
                ),
                span_start=0, span_end=len(pending_message_text),
            )
        )

        # Commitment 2: already human_verified and on-plan, so vendor status/
        # next-steps sections show more than one row.
        verified_commitment = Commitment(
            project_id=project_id, party_id=vendor.id, counterparty_id=internal.id,
            act_type_id=act_term.id, state="committed",
            deliverable_en="Stage rigging safety certification",
            deliverable_original="Stage rigging safety certification",
            due_at=now + timedelta(days=3), confidence=0.97, field_confidence={},
            verification_state="human_verified",
            verified_by=users_by_role["project_manager"].id, verified_at=now,
        )
        session.add(verified_commitment)
        await session.flush()
        session.add(
            Evidence(
                commitment_id=verified_commitment.id, channel="manual", sent_at=now,
                language="en",
                original_text="Rigging safety cert confirmed on site walkthrough, 3 days out.",
            )
        )

        # Budget baseline — makes the budget-summary section (and the
        # export-block gate, since pending_commitment's amount feeds
        # outstanding_payments) actually resolvable rather than "no budget
        # baseline recorded".
        budget = Budget(
            project_id=project_id, approved_amount=250_000.00, currency="SGD",
            approved_by=users_by_role["producer"].id, approved_at=now,
            revision_of=None, is_current=True,
        )
        session.add(budget)
        await session.flush()
        session.add(
            Evidence(
                budget_id=budget.id, channel="manual", sent_at=now, language="en",
                original_text="Budget baseline recorded via scripts/seed_dev_data.py (FR-ADM-11).",
            )
        )
        await session.flush()

        # Auto-drafted deviation off the pending commitment, so the
        # risk-and-issues section and the F1 deviation-confirm action both
        # have a real row to act on.
        await draft_deviation(
            session, project=project, class_code="spec_drift",
            description_en="Vendor's quoted LED wall spec drifted from the approved render — confirm before sign-off.",
            commitment_id=pending_commitment.id,
            evidence_text="Auto-drafted from a forecast/spec-drift check (scripts/seed_dev_data.py fixture).",
        )

        # F3 enablement (Foresight): app/foresight/risk.py's own detectors
        # (silence.py/contradiction.py/forecast.py) only ever fire from the
        # arq worker's periodic sweeps, on real elapsed time — not something
        # a Playwright run can wait on. Same ORM-direct pattern
        # loadtest/seed.py already uses: insert real Risk/Notification rows
        # for the frontend's own F3 TESTING EXPECTATION to act against
        # (filtering, acknowledge, the 409 race, and a real collapsed
        # notification), rather than a real sweep's timing.
        #
        # `base_rate=0.8` on risk_silence is fixture data for this dev
        # script only, not a claim CLAUDE.md's Models table would forbid —
        # it exists purely so the frontend has something real to render for
        # the "base rate present" case; risk_forecast's own base_rate stays
        # None (forecast-sourced Risks never get one, per Risk's own
        # docstring), so both the present and honestly-absent cases are
        # covered.
        risk_silence = Risk(
            project_id=project_id, source="silence", severity="medium", status="open",
            finding_key=f"silence:{pending_commitment.id}",
            commitment_id=pending_commitment.id,
            downstream_consequence=(
                "Golden Sound & Light has gone quiet past their own median WhatsApp reply "
                "window on the LED wall rental — main stage delivery is at risk of slipping "
                "past its 10-day-out commitment."
            ),
            base_rate=0.8,
            detail={"vendor": "Golden Sound & Light Pte Ltd", "expected_reply_by": (now + timedelta(days=2)).isoformat()},
        )
        risk_forecast = Risk(
            project_id=project_id, source="forecast", severity="critical", status="open",
            finding_key=f"forecast:{content_load.id}",
            milestone_id=content_load.id,
            downstream_consequence=(
                "Content load into screens has fallen to zero slack against the current "
                "critical path — any further delay upstream pushes doors-open directly."
            ),
            base_rate=None,
            detail={"slack_days": 0},
        )
        session.add_all([risk_silence, risk_forecast])
        await session.flush()

        # Collapsed notification (FR-NTF-03) to the seeded administrator —
        # the same role e2e/global-setup.ts logs Playwright in as, so
        # e2e/foresight.spec.ts can assert against a real, already-collapsed
        # row rather than re-deriving collapsing client-side.
        session.add(
            Notification(
                project_id=project_id, recipient_id=users_by_role["administrator"].id,
                risk_id=risk_forecast.id, collapsed_risk_ids=[risk_silence.id], collapsed_count=2,
                severity="critical", downstream_consequence=risk_forecast.downstream_consequence,
                deliverable_at=now,
            )
        )

        # A second, already-`confirmed` deviation (not auto-drafted) so F3's
        # own resolve-deviation flow has a row to act on distinct from the
        # auto_drafted spec_drift one above (which F3's own confirm test
        # uses) — create_manual_deviation immediately confirms, same
        # FR-DEV-01 "manually-entered-but-fully-real" posture as this
        # script's own commitments/budget above.
        await create_manual_deviation(
            session, project=project, actor_id=users_by_role["administrator"].id,
            class_code="delay", description_en="Rigging crew call time slipped two hours — resolved on site.",
            milestone_id=content_load.id,
            original_text="Rigging crew arrived two hours late; resolved by shifting the load-in window (scripts/seed_dev_data.py fixture).",
        )

        # F4 enablement (Documents): app/foresight/contradiction.py's own
        # detector only ever runs from scan_contradictions inside the arq
        # worker's periodic sweep — the same "not something Playwright can
        # wait on" gap F3's own risk_silence/risk_forecast fixtures above
        # already work around. Two documents, one spec claim each,
        # `contradicts` wired directly — same ORM-direct SpecClaim pattern
        # tests/test_foresight_contradiction.py's own _make_claim fixture
        # uses — so e2e/documents.spec.ts's own TESTING EXPECTATION has a
        # real cross-document contradiction to render and link between,
        # rather than one this script waits on a sweep to produce.
        # `storage_ref` points at no real MinIO object (this fixture never
        # exercises download/approve on these two rows — e2e's own upload
        # test uploads a fresh document through the real UI, hitting the
        # real StorageBackend, for that path instead); DocumentVersion.
        # evidence is still a real, NOT-NULL-enforced row either way.
        quotation_doc = Document(project_id=project_id, name="LED wall quotation.pdf")
        session.add(quotation_doc)
        await session.flush()
        quotation_version = DocumentVersion(
            document_id=quotation_doc.id, version_no=1,
            storage_ref=f"documents/{quotation_doc.id}/v1/seed",
            extracted_text="Location H: 2040mm x 1040mm LED wall panel, qty 1 set.",
        )
        session.add(quotation_version)
        await session.flush()
        session.add(
            Evidence(
                document_version_id=quotation_version.id, channel="manual", sent_at=now,
                language="en", original_text="Uploaded via scripts/seed_dev_data.py fixture.",
            )
        )
        quotation_doc.current_version_id = quotation_version.id
        quotation_claim = SpecClaim(
            document_version_id=quotation_version.id, location_code="H", attribute="dimension",
            value="2040mm x 1040mm",
        )
        session.add(quotation_claim)
        await session.flush()
        session.add(
            Evidence(
                spec_claim_id=quotation_claim.id, channel="manual", sent_at=now,
                language="en", original_text="2040mm x 1040mm",
            )
        )

        shop_drawing_doc = Document(project_id=project_id, name="LED wall shop drawing.pdf")
        session.add(shop_drawing_doc)
        await session.flush()
        shop_drawing_version = DocumentVersion(
            document_id=shop_drawing_doc.id, version_no=1,
            storage_ref=f"documents/{shop_drawing_doc.id}/v1/seed",
            extracted_text="Location H: 2000mm x 1040mm LED wall panel, qty 1 set.",
        )
        session.add(shop_drawing_version)
        await session.flush()
        session.add(
            Evidence(
                document_version_id=shop_drawing_version.id, channel="manual", sent_at=now,
                language="en", original_text="Uploaded via scripts/seed_dev_data.py fixture.",
            )
        )
        shop_drawing_doc.current_version_id = shop_drawing_version.id
        shop_drawing_claim = SpecClaim(
            document_version_id=shop_drawing_version.id, location_code="H", attribute="dimension",
            value="2000mm x 1040mm", contradicts=quotation_claim.id,
        )
        session.add(shop_drawing_claim)
        await session.flush()
        session.add(
            Evidence(
                spec_claim_id=shop_drawing_claim.id, channel="manual", sent_at=now,
                language="en", original_text="2000mm x 1040mm",
            )
        )

        await session.commit()

    print(f"organisation_id: {org_id}")
    print(f"project_id:      {project_id}")
    print()
    print("Paste organisation_id above into the /login form, then sign in as any of:")
    print()
    for role, email in seeded:
        print(f"  {role:<16} {email}")


if __name__ == "__main__":
    asyncio.run(main())
