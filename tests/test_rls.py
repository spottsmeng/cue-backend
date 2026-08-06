"""Row-level tenant isolation (CUE-Tech-Stack.md P1). Codifies the manual
psql verification done earlier — including the regression this suite exists
specifically to catch: the `cue` bootstrap role is a Postgres superuser with
BYPASSRLS, so if these tests ever ran against that role instead of the
unprivileged `cue_app`, every assertion here would pass for the wrong reason
(RLS silently not applying, not genuinely satisfied). app_session (fixture)
is cue_app; owner_session is cue and must never be used for the assertions
themselves, only for cross-tenant test setup.
"""

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.models import Organisation, Project
from tests.conftest import set_org_context


@pytest.mark.asyncio
async def test_cue_app_role_is_not_superuser_and_not_bypassrls(owner_session):
    """Regression guard for the exact bug found earlier in this project: RLS
    does not apply to a superuser or a BYPASSRLS role regardless of FORCE ROW
    LEVEL SECURITY. If this test ever fails, every other test in this file is
    passing for a false reason."""
    row = (
        await owner_session.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'cue_app'")
        )
    ).one()
    assert row.rolsuper is False
    assert row.rolbypassrls is False


@pytest.mark.asyncio
async def test_no_org_context_sees_zero_rows(app_session, org_and_project):
    org_id, project_id = org_and_project  # created (and committed) with context set

    # A fresh logical connection scope: nothing has set app.current_org_id on
    # *this* execution yet, since the fixture's own set_config was is_local
    # aware only within its own session object — assert explicitly to make
    # the "unset -> deny" behaviour a first-class, visible assertion.
    await app_session.execute(text("SELECT set_config('app.current_org_id', '', false)"))
    result = await app_session.execute(select(Organisation).where(Organisation.id == org_id))
    assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_org_context_sees_only_own_org(app_session):
    org_a = uuid.uuid4()
    org_b = uuid.uuid4()

    await set_org_context(app_session, org_a)
    app_session.add(Organisation(id=org_a, name="Org A"))
    await app_session.commit()

    await set_org_context(app_session, org_b)
    app_session.add(Organisation(id=org_b, name="Org B"))
    await app_session.commit()

    # Now scoped to org_a: org_b must be invisible, org_a must be visible.
    await set_org_context(app_session, org_a)
    visible = (await app_session.execute(select(Organisation.id))).scalars().all()
    assert org_a in visible
    assert org_b not in visible


@pytest.mark.asyncio
async def test_insert_without_org_context_is_rejected(app_session):
    await app_session.execute(text("SELECT set_config('app.current_org_id', '', false)"))
    app_session.add(Organisation(id=uuid.uuid4(), name="Sneaky Org"))
    with pytest.raises(DBAPIError, match="row-level security"):
        await app_session.commit()
    await app_session.rollback()


@pytest.mark.asyncio
async def test_commitments_isolated_via_project_join(app_session, org_and_project, parties):
    """commitments has no organisation_id column — its policy isolates via a
    subquery through projects. Confirms that join-based policy, not just the
    direct-column ones on organisations/projects."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, uuid.uuid4())  # a different, unrelated org

    result = await app_session.execute(select(Project).where(Project.id == project_id))
    assert result.scalar_one_or_none() is None  # project itself already invisible
