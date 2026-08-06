from collections.abc import AsyncIterator

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
    pooled connection into a different request. See
    scripts/extract_fixtures.py for a worked (session-scoped, is_local=false)
    example of the same mechanism.
    """
    async with async_session_factory() as session:
        yield session
