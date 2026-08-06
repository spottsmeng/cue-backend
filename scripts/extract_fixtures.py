"""Run cue-eval's fixture cases through the real extraction pipeline, writing
into the actual schema (not hand-written test SQL) — the thing this whole
backend scaffolding pass was building toward.

Usage: uv run python scripts/extract_fixtures.py
"""

import asyncio
import uuid

from sqlalchemy import text

from app.capture.fixtures import load_fixture_cases
from app.core.db import async_session_factory
from app.ledger.extractor import RejectedExtraction, extract_case
from app.models import Organisation, Project, Vertical


async def main() -> None:
    context, cases = load_fixture_cases()

    org_id = uuid.uuid4()
    vertical_id = uuid.uuid4()
    project_id = uuid.uuid4()

    async with async_session_factory() as session:
        # set_config(), not a literal `SET ... = :param` — Postgres's SET
        # statement doesn't accept bind parameters at all (only literals),
        # so a dynamic value has to go through the function form instead.
        # is_local=false (session-wide), since this script holds one session
        # across many sequential commits and wants the org context to persist
        # through all of them. Contrast with app/core/db.py's documented
        # production pattern — per-request RBAC middleware should pass
        # is_local=true, scoped to one transaction, so the setting can't leak
        # across a pooled connection into a different request.
        await session.execute(
            text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)}
        )

        # Staged, explicit flushes rather than one batched flush of all three —
        # the FK is set by raw UUID value, not an ORM relationship() object
        # reference, so SQLAlchemy's automatic insert-dependency sorting isn't
        # guaranteed to order Vertical/Organisation before Project.
        session.add(Vertical(id=vertical_id, code="event-production", name="Event Production"))
        session.add(Organisation(id=org_id, name="Fixture Test Org"))
        await session.flush()
        session.add(
            Project(
                id=project_id,
                organisation_id=org_id,
                vertical_id=vertical_id,
                name=context["project"],
                client_name=context["client"],
                timezone=context["timezone"],
            )
        )
        await session.flush()
        await session.commit()

        print(f"\n  org {org_id}   project {project_id}\n")
        print("  {:<5} {:<16} {}".format("id", "band", "result"))
        print("  " + "-" * 60)

        created_total = 0
        rejected_total = 0
        for case in cases:
            try:
                created = await extract_case(
                    session,
                    project_id=project_id,
                    organisation_id=org_id,
                    context=context,
                    case=case,
                )
                await session.commit()
                created_total += len(created)
                print(f"  {case['id']:<5} {case['band']:<16} {len(created)} commitment(s)")
            except RejectedExtraction as e:
                await session.rollback()
                rejected_total += 1
                print(f"  {case['id']:<5} {case['band']:<16} REJECTED: {e}")

        print("  " + "-" * 60)
        print(f"  total commitments created: {created_total}")
        print(f"  total cases rejected:      {rejected_total}\n")


if __name__ == "__main__":
    asyncio.run(main())
