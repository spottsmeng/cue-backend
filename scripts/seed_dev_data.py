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
from app.foresight.deviation import draft_deviation
from app.identity.config import get_identity_settings
from app.identity.models import Membership, User
from app.models import Budget, Channel, ChannelIdentity, Commitment, Evidence, OntologyTerm, Organisation, Party, Project
from app.models.vertical import Vertical
from app.twin.service import materialize_archetype

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

        project = Project(
            id=project_id,
            organisation_id=org_id,
            vertical_id=vertical_id,
            name="CUE Dev Project",
            client_name="Dev Client",
            venue="Dev Venue",
            timezone="Asia/Singapore",
        )
        session.add(project)
        await session.flush()

        # FR-TWN-02: give F2's Twin work a real graph, not an empty project —
        # same call app/api/projects.py's create_project makes inline.
        # "event-production-default" (seed_data/event_production_archetype.py's
        # ARCHETYPE_CODE) is the actual archetype row's code; "event-production"
        # alone is the *vertical* code and 422s here as an unknown archetype.
        await materialize_archetype(session, project, "event-production-default")

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

        now = datetime.now(dt_timezone.utc)

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
