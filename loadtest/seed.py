#!/usr/bin/env python3
"""Seeds one org/project/party/user + mints a bearer token for the k6
scripts in this directory to hit — there's no public REST endpoint for org
creation (a real deployment provisions tenants out of band, not something
this backend exposes to itself), so this uses the same direct-ORM approach
tests/conftest.py's own org_and_project/authed_org_and_project/parties
fixtures already establish, run once as a standalone script instead of a
pytest fixture.

Run from backend/ with the app's own venv active (real Postgres required —
docker-compose's `postgres` service, same as `uv run pytest`):

    uv run python3 loadtest/seed.py

Prints BASE_URL/TOKEN/PROJECT_ID as `export` lines — eval them directly:

    eval "$(uv run python3 loadtest/seed.py)"
    k6 run loadtest/commitments.js
"""

import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.core.db import async_session_factory
from app.identity.config import get_identity_settings
from app.identity.models import Membership, User
from app.identity.tokens import mint_local_token
from app.models import Organisation, Party, Project


async def _set_org_context(session, org_id: uuid.UUID) -> None:
    from sqlalchemy import text

    await session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})


async def main() -> None:
    async with async_session_factory() as session:
        from app.models.vertical import Vertical

        vertical_id = (
            await session.execute(select(Vertical.id).where(Vertical.code == "event-production"))
        ).scalar_one()

        org_id, project_id = uuid.uuid4(), uuid.uuid4()
        await _set_org_context(session, org_id)

        session.add(Organisation(id=org_id, name="Load Test Org"))
        await session.flush()
        session.add(
            Project(
                id=project_id, organisation_id=org_id, vertical_id=vertical_id,
                name="Load Test Project", timezone="Asia/Singapore",
            )
        )
        await session.flush()

        subject = f"loadtest-{uuid.uuid4()}"
        settings = get_identity_settings()
        user = User(
            organisation_id=org_id, issuer=settings.local_issuer,
            external_subject=subject, email=f"{subject}@example.test",
        )
        session.add(user)
        await session.flush()
        session.add(Membership(user_id=user.id, project_id=project_id, role="administrator", granted_by=user.id))

        vendor = Party(organisation_id=org_id, display_name="Load Test Vendor", type="vendor_org")
        internal = Party(organisation_id=org_id, display_name="Load Test Internal", type="internal_staff")
        session.add_all([vendor, internal])
        await session.flush()

        await session.commit()

        token = mint_local_token(
            subject=subject, org_id=org_id, secret=settings.local_jwt_secret,
            issuer=settings.local_issuer, email=user.email,
        )

        print("export CUE_LOADTEST_BASE_URL=http://localhost:8000")
        print(f"export CUE_LOADTEST_TOKEN={token}")
        print(f"export CUE_LOADTEST_PROJECT_ID={project_id}")
        print(f"export CUE_LOADTEST_PARTY_ID={vendor.id}")
        print(f"export CUE_LOADTEST_COUNTERPARTY_ID={internal.id}")


if __name__ == "__main__":
    asyncio.run(main())
