"""arq (Valkey-backed) worker — CUE-Tech-Stack.md §2.4: "Lightweight task
queue — arq (Valkey-backed) — fire-and-forget jobs... notification fan-out."
Foresight (Prompt 7) is the first milestone in this codebase that needs a
scheduled/background job at all (Silence Radar has to run periodically, not
just on request) — this module is that genuinely new infrastructure.

Run the worker with: `uv run arq app.foresight.worker.WorkerSettings`
(see backend/README.md for the full local-dev setup, including the
docker-compose `valkey` service this connects to).
"""

import logging
import uuid

from arq import cron
from arq.connections import RedisSettings
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import get_settings
from app.core.db import async_session_factory
from app.foresight.config import get_arq_settings
from app.foresight.contradiction import scan_contradictions
from app.foresight.escalation import escalate_unacknowledged_risks
from app.foresight.forecast import scan_forecast, scan_overdue_commitments
from app.foresight.notification import deliver_due_notifications
from app.foresight.silence import scan_silence
from app.models import Project
from app.reports.schedule import run_due_report_schedules

logger = logging.getLogger("app.foresight.worker")


async def _set_org_context(session: AsyncSession, org_id: uuid.UUID) -> None:
    """`is_local=false` (session-wide) — this session runs one project's
    full sweep across several sequential commits, same reasoning
    scripts/extract_fixtures.py's own use of this mechanism gives; contrast
    with app/core/db.py's per-request `is_local=true`."""
    await session.execute(
        text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)}
    )


async def run_project_sweep(session: AsyncSession, project: Project) -> None:
    """Runs every Foresight detection/maintenance pass for one project, in a
    fixed order — silence and contradiction first, since forecast.py's own
    heuristic reads an *open* Silence Radar flag; running forecast after
    them makes a same-sweep silence flag visible to it immediately rather
    than one sweep cycle later. Commits after each sub-scan rather than
    once at the end, so a bug in one detector can't roll back another's
    already-good work for the same project."""
    await scan_silence(session, project)
    await session.commit()
    await scan_contradictions(session, project)
    await session.commit()
    await scan_forecast(session, project)
    await session.commit()
    await scan_overdue_commitments(session, project)
    await session.commit()
    await escalate_unacknowledged_risks(session, project)
    await session.commit()
    await deliver_due_notifications(session, project)
    await session.commit()


async def _discover_active_projects() -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Returns (organisation_id, project_id) for every non-archived
    project, across every tenant. The one deliberate RLS bypass in this
    module: connects as the schema-owner role (same
    settings.migration_database_url tests/conftest.py's owner_engine and
    scripts/extract_fixtures.py already use this way), because "which
    projects exist across every organisation" is exactly what a
    platform-level scheduled job needs to answer and no single tenant's RLS
    context could ever see all of — there is no service-account/agent
    identity in this codebase yet (backend/PROGRESS.md's M2 notes: "a
    placeholder until that identity model exists"). Every subsequent read/
    write for a given project still runs through the ordinary RLS-enforced
    app_session with that project's own org context set — only this
    discovery step bypasses RLS, never the processing itself."""
    engine = create_async_engine(get_settings().migration_database_url)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text("SELECT organisation_id, id FROM projects WHERE archived_at IS NULL")
                )
            ).all()
            return [(row.organisation_id, row.id) for row in rows]
    finally:
        await engine.dispose()


async def run_foresight_sweep(ctx: dict | None = None) -> int:
    """The arq cron job body — also directly callable (by tests, or a
    one-off ops invocation) without a running worker/broker at all, since it
    takes no queue-specific state from `ctx`. Returns the number of projects
    swept."""
    targets = await _discover_active_projects()
    swept = 0
    for organisation_id, project_id in targets:
        async with async_session_factory() as session:
            await _set_org_context(session, organisation_id)
            project = (
                await session.execute(select(Project).where(Project.id == project_id))
            ).scalar_one_or_none()
            if project is None:
                continue  # deleted between discovery and processing — skip, not an error
            try:
                await run_project_sweep(session, project)
                swept += 1
            except Exception:
                await session.rollback()
                logger.exception("Foresight sweep failed for project %s", project_id)
    return swept


_arq_settings = get_arq_settings()


class WorkerSettings:
    """arq's own discovery convention — a plain class with `functions`/
    `cron_jobs`/`redis_settings` attributes, referenced by dotted path on
    the command line (module docstring).

    `run_due_report_schedules` (FR-RPT-09, app/reports/schedule.py) rides
    this same worker process rather than standing up a second arq/Valkey
    pair — Prompt 8's own instruction not to over-invest in scheduling
    machinery beyond the Must-priority reporting items. Not a Foresight
    concern living here by accident; this class is simply the one
    background-job process this codebase runs at all.
    """

    functions = [run_foresight_sweep, run_due_report_schedules]
    # Every 15 minutes — frequent enough that a real silence/forecast/
    # escalation condition surfaces promptly, infrequent enough not to
    # hammer Postgres with a full-tenant scan; not tuned against any
    # measured load (there is none yet), a documented starting point like
    # every other threshold in this module, adjustable without a code
    # change once there's real traffic to tune against. The report schedule
    # scan shares this same cadence (app/reports/schedule.py's `_is_due`
    # docstring: hour-granularity schedules don't need a finer tick).
    cron_jobs = [
        cron(run_foresight_sweep, minute=set(range(0, 60, 15))),
        cron(run_due_report_schedules, minute=set(range(0, 60, 15))),
    ]
    redis_settings = RedisSettings(host=_arq_settings.redis_host, port=_arq_settings.redis_port)
