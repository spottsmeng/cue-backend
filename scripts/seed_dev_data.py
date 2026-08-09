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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, text

from app.core.db import async_session_factory
from app.identity.config import get_identity_settings
from app.identity.models import Membership, User
from app.models import Organisation, Project
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
