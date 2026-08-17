import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

settings = get_settings()

# pool_pre_ping=True is deliberately NOT set here. It's a genuinely useful
# production feature (discards dead connections, e.g. after a DB restart, on
# next checkout) — removed only because it reproducibly caused
# `MissingGreenlet` errors under the test suite's harness (multiple engines —
# this one plus tests/conftest.py's owner_engine — sharing one pytest-asyncio
# session-scoped loop, a setup a real running FastAPI process doesn't have).
# Never confirmed whether it's actually unsafe in production or purely a test
# harness artifact; revisit with a proper investigation before re-adding it,
# rather than assuming either answer.
engine = create_async_engine(settings.database_url, echo=settings.sql_echo)

async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request, closed on exit.

    RBAC middleware sets `app.current_org_id` on this session per request via
    `SELECT set_config('app.current_org_id', :org_id, true)` — the function
    form, not a literal `SET LOCAL ... = :param` statement, since Postgres's
    SET does not accept bind parameters at all, only literals. `is_local=true`
    scopes it to the request's transaction so the value can't leak across a
    pooled connection into a different request — safe here because a request
    normally makes one commit. A caller that needs to do this across *more
    than one* commit on the same session (an arq worker sweep, not a
    request) must use `org_scoped_transaction`/`set_local_org_context` below
    instead of a single `is_local=false` call — see that function's own
    docstring for the real bug a session-scoped value causes under a shared
    connection pool. `scripts/extract_fixtures.py`/`scripts/seed_dev_data.py`
    still use a bare `is_local=false` call directly: both are one-shot CLI
    scripts holding their own dedicated engine/pool for the process's short
    lifetime, never running concurrently with another job against the same
    pool, so that specific risk doesn't apply to them — not a precedent to
    follow anywhere a session lives inside a long-running, multi-job worker
    process.
    """
    async with async_session_factory() as session:
        yield session


async def set_local_org_context(session: AsyncSession, org_id: uuid.UUID) -> None:
    """`SET LOCAL` (`is_local=true`) — scoped to whatever the *current*
    transaction turns out to be, never surviving past its own commit or
    rollback. This is the only safe way to set `app.current_org_id` for a
    session a worker holds across several commits; see `org_scoped_
    transaction`'s own docstring below for the full reasoning and the real
    bug this replaces.

    A bare primitive, not the context manager below, for the one shape that
    doesn't fit "set, do work, commit": a caller with its own try/except/
    rollback around the unit of work (e.g. app/reports/schedule.py's
    `run_due_report_schedules`, which must roll back on `ExportBlocked`
    without this helper then unconditionally committing anyway). Call this
    once, immediately before whatever block of statements ends in that
    caller's own explicit commit/rollback — never once for a whole
    multi-commit session.
    """
    await session.execute(
        text("SELECT set_config('app.current_org_id', :oid, true)"), {"oid": str(org_id)}
    )


@asynccontextmanager
async def org_scoped_transaction(session: AsyncSession, org_id: uuid.UUID) -> AsyncIterator[None]:
    """The one correct way to set `app.current_org_id` for a session that
    makes more than one commit — every worker sweep in this codebase that
    processes several sub-steps or several rows per organisation, each its
    own commit (app/foresight/worker.py's `run_project_sweep`,
    app/capture/pipeline.py's `ingest_channel_backlog`, and others).

    **Why `is_local=true`, called once per unit of work, not once per
    session.** SQLAlchemy's `AsyncSession` releases its DBAPI connection
    back to the engine's pool on every `commit()` (and reacquires one,
    possibly a *different* physical connection, on the next statement) —
    this is normal, documented "autobegin" behaviour, not a bug. A single
    `SELECT set_config('app.current_org_id', :oid, false)` (`is_local=
    false`, session-scoped) called once at the top of a multi-commit
    session looks correct and passes any sequential test, but is genuinely
    unsafe: `false` means the value is NOT cleared by commit, so it
    persists on the physical connection after release — and the *next*
    thing to check that exact connection out of the pool (a concurrent arq
    job in the same worker process; this codebase's own arq cron jobs and
    on-demand jobs all share one process and one pool) silently inherits
    it. Found for real, not hypothesised: a live `ingest_channel_job` run
    overlapping a scheduled cron sweep produced a genuine cross-tenant RLS
    rejection on the *second* message of a backlog, the first having
    already committed correctly.

    `is_local=true` (`SET LOCAL`) is the fix: Postgres resets it
    automatically at the end of *every* transaction (commit or rollback),
    so it can never survive onto a connection returned to the pool — the
    same guarantee `app/api/deps.py`'s `get_current_user` already relies on
    for the single-transaction, per-request case (see its own docstring).
    The only change a multi-commit caller needs is re-asserting it before
    *each* unit of work about to be committed, which is exactly what
    wrapping that unit of work in this context manager does. If the
    wrapped block raises, this does not commit — the exception propagates
    so the caller's own rollback/retry handling (already present at every
    call site) runs unchanged. A caller that needs its own rollback branch
    instead of always-commit-or-propagate should use `set_local_org_context`
    directly (above) rather than this context manager.
    """
    await set_local_org_context(session, org_id)
    yield
    await session.commit()
