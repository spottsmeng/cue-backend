"""Test infrastructure against a real second database (cue_test), not mocks —
several of the things this suite codifies (the deferred evidence-constraint
triggers, RLS policies) only prove anything when actually exercised at COMMIT
time against real Postgres. A rollback-only or fully in-memory test strategy
would never fire the deferred trigger at all and would silently prove nothing.

pyproject.toml's [tool.pytest_env] points CUE_DATABASE_URL /
CUE_MIGRATION_DATABASE_URL at cue_test *before* collection, which matters
because app/core/db.py builds its engine as a module-level singleton at
import time.
"""

import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.identity.config import get_identity_settings
from app.identity.models import Membership, User
from app.identity.tokens import mint_local_token
from app.models import Base, Organisation, Party, Project
from seed_data.event_production_archetype import ARCHETYPE_CODE, ARCHETYPE_DEPENDENCIES, ARCHETYPE_ITEMS, ARCHETYPE_NAME
from seed_data.verticals import PLATFORM_VERTICALS

BACKEND_DIR = Path(__file__).resolve().parents[1]
TEST_DB_NAME = "cue_test"
ADMIN_URL = "postgresql+asyncpg://cue:cue@localhost:5432/cue"
OWNER_TEST_URL = f"postgresql+asyncpg://cue:cue@localhost:5432/{TEST_DB_NAME}"

EVENT_PRODUCTION_VERTICAL_CODE = next(code for code, _name in PLATFORM_VERTICALS if code == "event-production")

# Tables never named directly in the per-test TRUNCATE. verticals genuinely
# survives it (nothing being truncated is referenced *by* verticals in the
# CASCADE-relevant direction). ontology_terms does NOT actually survive —
# it has its own organisation_id FK pointing at organisations, so truncating
# organisations legitimately CASCADEs into it too (correct Postgres
# behaviour; not a bug — see _reseed_universal_ontology_terms below for how
# that's handled, and the comment on _clean_between_tests for how this was
# actually diagnosed).
#
# milestone_archetypes/milestone_archetype_items/milestone_archetype_dependencies
# (app/twin/models.py) used to be immune to this entirely (no FK into
# anything ever truncated). That stopped being true the moment
# MilestoneArchetype gained its own `organisation_id` (the tenant-extension
# tier, CUE-PRD.md §4.2.1) — now they sit on the exact same cascade chain
# ontology_terms does, and need the same reseed treatment; see
# _reseed_default_archetype below.
PRESERVE_TABLES = {
    "ontology_terms", "verticals", "alembic_version",
    "milestone_archetypes", "milestone_archetype_items", "milestone_archetype_dependencies",
}

COMMITMENT_ACTS = [
    ("offer", "Offer", "提议"),
    ("commit", "Commit", "承诺"),
    ("confirm", "Confirm", "确认"),
    ("revoke", "Revoke", "撤销"),
    ("renegotiate", "Renegotiate", "重新协商"),
    ("quote", "Quote", "报价"),
    ("approve", "Approve", "批准"),
    ("escalate", "Escalate", "升级"),
    ("query", "Query", "询问"),
]

# Mirrors alembic/versions/83b72f2a9175's seed, same deliberate duplication
# rationale as COMMITMENT_ACTS above (a migration is a frozen historical
# artefact tests shouldn't import from). milestone_archetype_items.type_code
# (app/twin/models.py) references these by code, so a test that exercises
# create_project's archetype materialization needs them present, same as
# every other test needs COMMITMENT_ACTS present.
MILESTONE_TYPES = [
    ("design_approval", "Design approval", "设计批准"),
    ("artwork_submission", "Artwork submission", "美术稿提交"),
    ("test_print", "Test print", "打样测试"),
    ("shop_drawing_confirmation", "Shop drawing confirmation", "车间图确认"),
    ("permit_issuance", "Permit issuance", "许可证签发"),
    ("fnb_confirmation", "F&B confirmation", "餐饮确认"),
    ("content_approval", "Content approval", "内容批准"),
    ("contractor_move_in", "Contractor move-in", "承包商进场"),
    ("exhibitor_check_in", "Exhibitor check-in", "参展商签到"),
    ("load_in", "Load-in", "货物进场"),
    ("rigging", "Rigging", "吊装"),
    ("install", "Install", "安装"),
    ("content_load", "Content load", "内容加载"),
    ("exhibits_ready", "Exhibits ready", "展品就绪"),
    ("rehearsal", "Rehearsal", "彩排"),
    ("doors", "Doors", "开幕"),
    ("strike", "Strike", "拆展"),
    ("crate_collection", "Crate collection", "箱柜回收"),
]


async def _ensure_test_database() -> None:
    admin_engine = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    async with admin_engine.connect() as conn:
        exists = (
            await conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": TEST_DB_NAME}
            )
        ).scalar()
        if not exists:
            await conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
    await admin_engine.dispose()

    # Schema grants and default privileges are per-database in Postgres, so
    # cue_app needs these re-issued here even though it already has them on
    # the dev database (db/init/01-create-app-role.sql). Idempotent — GRANT
    # and ALTER DEFAULT PRIVILEGES are both safe to repeat.
    grant_engine = create_async_engine(OWNER_TEST_URL, isolation_level="AUTOCOMMIT")
    async with grant_engine.connect() as conn:
        await conn.execute(text("GRANT CONNECT ON DATABASE cue_test TO cue_app"))
        await conn.execute(text("GRANT USAGE ON SCHEMA public TO cue_app"))
        await conn.execute(
            text(
                "ALTER DEFAULT PRIVILEGES FOR ROLE cue IN SCHEMA public "
                "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO cue_app"
            )
        )
        await conn.execute(
            text(
                "ALTER DEFAULT PRIVILEGES FOR ROLE cue IN SCHEMA public "
                "GRANT USAGE ON SEQUENCES TO cue_app"
            )
        )
    await grant_engine.dispose()


def _run_migrations() -> None:
    # Alembic's Python API, in-process, rather than shelling out to
    # `alembic upgrade head` via subprocess — fewer moving parts, one less
    # process boundary, exercises the same migration chain either way.
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session", autouse=True)
def _test_db_ready() -> None:
    """Runs once per test session, before any test — creates cue_test if
    missing, grants cue_app access to it, and brings it to head."""
    import asyncio

    asyncio.run(_ensure_test_database())
    _run_migrations()


@pytest_asyncio.fixture(scope="session")
async def owner_engine():
    """Connects as `cue` (schema owner) — bypasses RLS entirely. Used only for
    test setup/teardown (seeding cross-tenant fixtures, truncating between
    tests), never for the actual behaviour under test — that must go through
    cue_app or RLS assertions are meaningless."""
    engine = create_async_engine(OWNER_TEST_URL)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(scope="session")
async def owner_sessionmaker(owner_engine):
    return async_sessionmaker(owner_engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def owner_session(owner_sessionmaker):
    async with owner_sessionmaker() as session:
        yield session


@pytest_asyncio.fixture
async def app_session():
    """The app's real session factory (app.core.db.async_session_factory),
    already pointed at cue_test/cue_app by pytest-env — not a parallel
    reimplementation. This is what RLS-sensitive tests use, since it's a
    genuinely unprivileged connection, same as production."""
    from app.core.db import async_session_factory

    async with async_session_factory() as session:
        yield session


async def set_org_context(session, org_id: uuid.UUID, *, is_local: bool = False) -> None:
    """SELECT set_config(...), not a literal SET — Postgres's SET statement
    doesn't accept bind parameters. See app/core/db.py and
    scripts/extract_fixtures.py for the same mechanism in the app/scripts."""
    await session.execute(
        text("SELECT set_config('app.current_org_id', :oid, :is_local)"),
        {"oid": str(org_id), "is_local": is_local},
    )


def mint_token(org_id: uuid.UUID, *, subject: str | None = None, email: str | None = None) -> str:
    """Bearer token for the "local" auth provider (app/identity/tokens.py) —
    what every API-level test authenticates with, exercising the real
    get_current_user/RBAC dependency chain (app/api/deps.py) rather than the
    old X-Org-Id/X-User-Id header stub. A fresh `subject` (the default) mints
    a brand-new identity, auto-provisioned as a `users` row on its first
    authenticated request; pass the same `subject` again — or reuse the
    returned token — to keep acting as the same person across multiple
    calls."""
    settings = get_identity_settings()
    subject = subject or f"sub-{uuid.uuid4()}"
    email = email or f"{subject}@example.test"
    return mint_local_token(
        subject=subject,
        org_id=org_id,
        secret=settings.local_jwt_secret,
        issuer=settings.local_issuer,
        email=email,
    )


def auth_headers(org_id: uuid.UUID, **kwargs) -> dict[str, str]:
    return {"Authorization": f"Bearer {mint_token(org_id, **kwargs)}"}


async def _reseed_universal_ontology_terms(conn) -> None:
    """The 9 universal commitment_act terms, plus the 18 milestone_type terms
    (vertical-pack scoped to event-production, per eed5a4da79f6's re-key —
    see that migration's own comment for why universal-core stopped being
    right once a second vertical became a real roadmap item), re-inserted
    after every TRUNCATE. Not the seed migrations' own logic reused — this
    is a separate, deliberately duplicated copy, because a migration is a
    frozen historical artefact tests should never import from; drifting
    from it would only matter if the real migration's seed values changed,
    which is exactly the kind of change that should be caught by re-running
    this suite anyway. (seed_data/ content, by contrast, IS imported
    directly by both — see _reseed_default_archetype below for why that's a
    different, lower-risk kind of sharing.)

    Assumes `verticals` already has its event-production row — true from
    the start of every test session, since eed5a4da79f6-onward runs as part
    of `_run_migrations()` before any test executes, and `verticals` is
    never truncated (PRESERVE_TABLES, and nothing cascades into it)."""
    await conn.execute(
        text(
            "INSERT INTO ontology_terms "
            "(id, category, code, label_en, label_zh, sort_order, active, effective_from, version) "
            "VALUES (gen_random_uuid(), 'commitment_act', :code, :label_en, :label_zh, "
            ":sort_order, true, now(), 1)"
        ),
        [
            {"code": code, "label_en": label_en, "label_zh": label_zh, "sort_order": i}
            for i, (code, label_en, label_zh) in enumerate(COMMITMENT_ACTS)
        ],
    )
    await conn.execute(
        text(
            "INSERT INTO ontology_terms "
            "(id, category, code, label_en, label_zh, vertical_id, sort_order, active, effective_from, version) "
            "VALUES (gen_random_uuid(), 'milestone_type', :code, :label_en, :label_zh, "
            "(SELECT id FROM verticals WHERE code = :vertical_code), :sort_order, true, now(), 1)"
        ),
        [
            {
                "code": code, "label_en": label_en, "label_zh": label_zh, "sort_order": i,
                "vertical_code": EVENT_PRODUCTION_VERTICAL_CODE,
            }
            for i, (code, label_en, label_zh) in enumerate(MILESTONE_TYPES)
        ],
    )


async def _reseed_default_archetype(conn) -> None:
    """milestone_archetypes/items/dependencies, re-inserted after every
    TRUNCATE — see PRESERVE_TABLES' comment for why these are now on the
    same cascade chain ontology_terms is.

    Unlike COMMITMENT_ACTS/MILESTONE_TYPES above, this imports seed_data/
    directly instead of duplicating it: seed_data/ is not itself a
    migration (no point-in-time DDL to drift from), just versioned content
    both the migration and this fixture read the same way OntologyTerm rows
    themselves are read by application code — the duplication upstream
    exists specifically to catch a *migration's* seed values silently
    drifting from what tests expect, which doesn't apply to a plain data
    file with no historical-freeze requirement.
    """
    vertical_id = (
        await conn.execute(
            text("SELECT id FROM verticals WHERE code = :code"),
            {"code": EVENT_PRODUCTION_VERTICAL_CODE},
        )
    ).scalar_one()

    archetype_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO milestone_archetypes (id, code, vertical_id, organisation_id, name, is_default) "
            "VALUES (:id, :code, :vertical_id, NULL, :name, true)"
        ),
        {"id": archetype_id, "code": ARCHETYPE_CODE, "vertical_id": vertical_id, "name": ARCHETYPE_NAME},
    )

    item_ids_by_code: dict[str, uuid.UUID] = {code: uuid.uuid4() for code, *_ in ARCHETYPE_ITEMS}
    await conn.execute(
        text(
            "INSERT INTO milestone_archetype_items "
            "(id, archetype_id, sequence_order, type_code, name, day_offset, is_fixed) "
            "VALUES (:id, :archetype_id, :sequence_order, :type_code, :name, :day_offset, :is_fixed)"
        ),
        [
            {
                "id": item_ids_by_code[type_code],
                "archetype_id": archetype_id,
                "sequence_order": i,
                "type_code": type_code,
                "name": name,
                "day_offset": day_offset,
                "is_fixed": is_fixed,
            }
            for i, (type_code, name, day_offset, is_fixed) in enumerate(ARCHETYPE_ITEMS)
        ],
    )
    await conn.execute(
        text(
            "INSERT INTO milestone_archetype_dependencies "
            "(id, archetype_id, upstream_item_id, downstream_item_id, lag_days) "
            "VALUES (:id, :archetype_id, :upstream_item_id, :downstream_item_id, :lag_days)"
        ),
        [
            {
                "id": uuid.uuid4(),
                "archetype_id": archetype_id,
                "upstream_item_id": item_ids_by_code[upstream_code],
                "downstream_item_id": item_ids_by_code[downstream_code],
                "lag_days": lag_days,
            }
            for upstream_code, downstream_code, lag_days in ARCHETYPE_DEPENDENCIES
        ],
    )


@pytest_asyncio.fixture(scope="session")
async def seeded_vertical_id(owner_engine) -> uuid.UUID:
    """One shared 'event-production' vertical row, created once per session
    (like the ontology_terms seed) rather than per test."""
    vertical_id = uuid.uuid4()
    async with owner_engine.begin() as conn:
        existing = (
            await conn.execute(
                text("SELECT id FROM verticals WHERE code = 'event-production'")
            )
        ).scalar()
        if existing:
            return existing
        await conn.execute(
            text(
                "INSERT INTO verticals (id, code, name, active, created_at, updated_at) "
                "VALUES (:id, 'event-production', 'Event Production', true, now(), now())"
            ),
            {"id": vertical_id},
        )
    return vertical_id


@pytest_asyncio.fixture
async def org_and_project(app_session, seeded_vertical_id):
    """A real organisation + project, created through cue_app (not the owner
    role) — the same code path production would use, generating the org's
    UUID client-side first so the RLS WITH CHECK on the org's own insert can
    be satisfied (id = current_org_id) without a chicken-and-egg problem."""
    org_id = uuid.uuid4()
    project_id = uuid.uuid4()
    await set_org_context(app_session, org_id)

    app_session.add(Organisation(id=org_id, name="Test Org"))
    await app_session.flush()
    app_session.add(
        Project(
            id=project_id,
            organisation_id=org_id,
            vertical_id=seeded_vertical_id,
            name="Test Project",
            timezone="Asia/Singapore",
        )
    )
    await app_session.flush()
    await app_session.commit()
    return org_id, project_id


@pytest_asyncio.fixture
async def seeded_user(app_session, org_and_project) -> User:
    """A bare `users` row for org_and_project's organisation — exists to
    satisfy the FK now on commitments.verified_by / budgets.approved_by /
    audit_log.actor_id (app/identity/models.py), for tests that write those
    columns directly against app_session rather than through the
    authenticated REST API."""
    org_id, _ = org_and_project
    await set_org_context(app_session, org_id)
    user = User(
        organisation_id=org_id,
        issuer=get_identity_settings().local_issuer,
        external_subject=f"seeded-{uuid.uuid4()}",
        email=f"seeded-{uuid.uuid4()}@example.test",
    )
    app_session.add(user)
    await app_session.commit()
    return user


@pytest_asyncio.fixture
async def authed_org_and_project(app_session, org_and_project):
    """org_and_project, plus a real `users` row granted "administrator"
    membership on that project and a bearer token authenticating as them —
    the ready-made "someone who can do anything on this project" identity
    most REST-layer tests (test_projects_api.py, test_commitments_api.py)
    need, without each one having to provision a user through the API
    itself."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    subject = f"admin-{uuid.uuid4()}"
    user = User(
        organisation_id=org_id,
        issuer=get_identity_settings().local_issuer,
        external_subject=subject,
        email=f"{subject}@example.test",
    )
    app_session.add(user)
    await app_session.flush()
    app_session.add(
        Membership(user_id=user.id, project_id=project_id, role="administrator", granted_by=user.id)
    )
    await app_session.commit()
    token = mint_token(org_id, subject=subject, email=user.email)
    return org_id, project_id, user, token


@pytest_asyncio.fixture
async def parties(app_session, org_and_project):
    org_id, _ = org_and_project
    vendor = Party(organisation_id=org_id, display_name="Test Vendor", type="vendor_org")
    internal = Party(organisation_id=org_id, display_name="Test Internal", type="internal_staff")
    app_session.add_all([vendor, internal])
    await app_session.flush()
    await app_session.commit()
    return vendor, internal


@pytest_asyncio.fixture(autouse=True)
async def _clean_between_tests(owner_engine):
    """Runs after every test. Truncation (not rollback) is deliberate — tests
    exercising the deferred evidence trigger must actually COMMIT for it to
    fire at all, so there's no transaction left to roll back.

    Diagnosed the hard way: ontology_terms is not named in the TRUNCATE list
    below, but it has its own `organisation_id` FK pointing at organisations
    (the multi-vertical tenant-extension layer, CUE-Tech-Stack.md §5.1) — so
    truncating organisations legitimately CASCADEs into it too, wiping the
    seeded universal commitment_act terms on every single test's cleanup.
    That's correct Postgres behaviour once understood, not a bug to work
    around by trying to exclude ontology_terms from the cascade (not
    possible, and not fully desirable either — test-inserted ontology_terms
    rows, e.g. in test_ontology_uniqueness.py, should genuinely be cleared
    between tests too). The fix is to restore the seed data immediately
    after, not to prevent it from being swept up.
    """
    yield
    async with owner_engine.begin() as conn:
        tables = [t.name for t in Base.metadata.sorted_tables if t.name not in PRESERVE_TABLES]
        if tables:
            await conn.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
        await _reseed_universal_ontology_terms(conn)
        await _reseed_default_archetype(conn)
