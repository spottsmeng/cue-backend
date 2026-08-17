"""Real, concurrent proof for `app/core/db.py`'s `org_scoped_transaction`/
`set_local_org_context` — the fix for a genuine bug found live, not
hypothesised: a real `ingest_channel_job` run (app/capture/worker.py)
overlapping a scheduled cron sweep in the same arq worker process produced
an actual cross-tenant RLS rejection on the second message of a real
backlog, the first having already committed correctly under the right org.

No sequential test can reproduce this — the failure only exists when two
different sessions, each making several commits for a *different*
organisation, actually race for the same pooled connection. So this file
builds its own tiny (`pool_size=1`) engine against the real test database
and drives two sessions through their commits **round by round**, each
round dispatched via `asyncio.gather` so both are genuinely contending for
the pool's one connection at the same real point in time, every round —
not a free-running dual loop hoping the scheduler happens to interleave
them.

Writes `Project` rows specifically, not `Party` — confirmed directly
against `alembic/versions/a895ae03ec5c_initial_schema.py` that `projects`
carries a real `FORCE ROW LEVEL SECURITY` + `tenant_isolation` policy
keyed on `organisation_id`, while `parties` (this file's first draft) has
none at all and would silently accept a write under any org, proving
nothing.

Two tests: one demonstrates the *old* pattern (`is_local=false`, set once
at round 0) breaking deterministically by the very next round, so this
file doesn't just assert an abstract claim; the other proves `org_scoped_
transaction` is safe against the identical round-by-round contention. If
the first test ever stops failing on its own, that's a signal this file's
own reproduction has drifted, not that the bug stopped existing elsewhere.
"""

import asyncio
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.db import org_scoped_transaction
from app.models import Organisation, Project

_ROUNDS = 6


async def _make_org(owner_session, seeded_vertical_id) -> uuid.UUID:
    """Setup only, via the schema-owner session (bypasses RLS, same
    convention tests/conftest.py's own reseed helpers already use) — no
    `set_org_context` call needed, since RLS doesn't apply to the owner
    role at all. The org context under real test below is `app_session`/
    `small_pool_session_factory`'s own cue_app connections, never this
    one. `seeded_vertical_id` is only threaded through so the round
    functions below can build a valid `Project` row."""
    org_id = uuid.uuid4()
    owner_session.add(Organisation(id=org_id, name=f"Concurrency Test Org {org_id.hex[:8]}"))
    await owner_session.flush()
    await owner_session.commit()
    return org_id


@pytest_asyncio.fixture
async def small_pool_session_factory():
    """A dedicated engine, deliberately `pool_size=1, max_overflow=0` —
    two concurrent sessions can only ever share the *one* physical
    connection this pool has, which is exactly the condition that makes
    the bug (and the fix) deterministic here rather than a rare, flaky
    race. Disposed after the test so this doesn't leak connections into
    other tests' own pool."""
    engine = create_async_engine(get_settings().database_url, pool_size=1, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _unsafe_round(
    session: AsyncSession, org_id: uuid.UUID, vertical_id: uuid.UUID, label: str, i: int
) -> None:
    """The exact pattern this session's own fix replaces everywhere it was
    found: `is_local=false`, set once (round 0 only) — every later round
    trusts it to still be correct, which is exactly the assumption that
    doesn't hold once the connection has been handed to the other sweep
    and back.

    The leading `asyncio.sleep`: confirmed empirically (not assumed) that
    without it, asyncio's own scheduler happens to let whichever task
    `gather` started first run to full completion before the second task
    gets its own first turn at all under this driver/pool combination —
    which would make this reproduction silently pass for the wrong reason
    (no real contention ever happened, not "the fix works"). A short sleep
    is enough to force a genuine scheduler handoff between the two tasks
    every round; verified directly against a real connection's own
    `pg_backend_pid()`/`current_setting()` before writing this test.
    """
    await asyncio.sleep(0.01)
    if i == 0:
        await session.execute(
            text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)}
        )
    session.add(
        Project(
            organisation_id=org_id, vertical_id=vertical_id, name=f"{label}-{i}", timezone="Asia/Singapore"
        )
    )
    try:
        await session.commit()
    except Exception:
        # Roll back immediately, not on some later cleanup pass — with
        # pool_size=1, a failed commit leaves this session's connection
        # checked out but unusable until rolled back, and the *other*
        # concurrent task's own commit (already in flight in this same
        # `asyncio.gather` round) would otherwise block for SQLAlchemy's
        # full 30s default pool_timeout waiting for a connection this
        # session is silently still holding.
        await session.rollback()
        raise


async def _safe_round(
    session: AsyncSession, org_id: uuid.UUID, vertical_id: uuid.UUID, label: str, i: int
) -> None:
    await asyncio.sleep(0.01)  # same forced-handoff reasoning as _unsafe_round above
    async with org_scoped_transaction(session, org_id):
        session.add(
            Project(
                organisation_id=org_id, vertical_id=vertical_id, name=f"{label}-{i}", timezone="Asia/Singapore"
            )
        )


@pytest.mark.asyncio
async def test_is_local_false_genuinely_breaks_under_concurrent_multi_commit_sessions(
    small_pool_session_factory, owner_session, seeded_vertical_id
):
    """Reproduces the real bug this file's own module docstring describes,
    against the identical mechanism (not a mock of it): two sessions for
    two different orgs, each committing several times, sharing a
    one-connection pool, driven round by round so both are always
    genuinely contending. By round 1 at the latest, whichever session goes
    second in a round is working off whatever the *other* session's own
    last commit left the shared connection set to — a real RLS rejection,
    deterministically, not a rare flake."""
    org_a = await _make_org(owner_session, seeded_vertical_id)
    org_b = await _make_org(owner_session, seeded_vertical_id)

    failures: list[Exception] = []
    async with small_pool_session_factory() as session_a, small_pool_session_factory() as session_b:
        for i in range(_ROUNDS):
            results = await asyncio.gather(
                _unsafe_round(session_a, org_a, seeded_vertical_id, "unsafe-a", i),
                _unsafe_round(session_b, org_b, seeded_vertical_id, "unsafe-b", i),
                return_exceptions=True,
            )
            failures.extend(r for r in results if isinstance(r, Exception))
            if failures:
                break  # reproduced — no need to keep going

    assert failures, (
        "expected at least one round to fail with a real RLS rejection under this "
        "concurrency — if none did, this reproduction no longer demonstrates the bug "
        "org_scoped_transaction exists to fix, and this test itself needs "
        "re-examining before trusting the fix test below"
    )
    assert any(
        "row-level security" in str(f).lower() or "insufficientprivilege" in str(f).lower()
        for f in failures
    ), failures


@pytest.mark.asyncio
async def test_org_scoped_transaction_is_safe_under_the_identical_concurrency(
    small_pool_session_factory, owner_session, seeded_vertical_id
):
    """The fix, proven against the exact same adversarial setup as the
    reproduction above — same one-connection pool, same round-by-round
    concurrent commits for two different orgs. Every row must land under
    its own session's org, and no round may raise."""
    org_a = await _make_org(owner_session, seeded_vertical_id)
    org_b = await _make_org(owner_session, seeded_vertical_id)

    failures: list[Exception] = []
    async with small_pool_session_factory() as session_a, small_pool_session_factory() as session_b:
        for i in range(_ROUNDS):
            results = await asyncio.gather(
                _safe_round(session_a, org_a, seeded_vertical_id, "safe-a", i),
                _safe_round(session_b, org_b, seeded_vertical_id, "safe-b", i),
                return_exceptions=True,
            )
            failures.extend(r for r in results if isinstance(r, Exception))

    assert not failures, f"org_scoped_transaction should never raise here: {failures}"

    # Read back via the already-provided schema-owner session (bypasses
    # RLS, real proof each row landed under the *correct* org, not just
    # that nothing raised).
    rows_a = (
        await owner_session.execute(select(Project.name).where(Project.organisation_id == org_a))
    ).scalars().all()
    rows_b = (
        await owner_session.execute(select(Project.name).where(Project.organisation_id == org_b))
    ).scalars().all()

    assert sorted(rows_a) == sorted(f"safe-a-{i}" for i in range(_ROUNDS))
    assert sorted(rows_b) == sorted(f"safe-b-{i}" for i in range(_ROUNDS))
