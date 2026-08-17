"""FR-RPT-09 (Should): scheduled generation ahead of a recurring client
meeting. Deliberately minimal, per Prompt 8's own instruction not to
over-invest here relative to the Must-priority items above — this is a
plain scan (mirrors app/foresight/worker.py's run_foresight_sweep shape
closely: same schema-owner discovery bypass, same reasoning for it, same
directly-callable-without-a-broker contract), registered onto the same arq
worker process app/foresight/worker.py already stood up rather than a
second one for one lightweight periodic job.
"""

import logging
import uuid
from datetime import datetime, timezone as dt_timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.core.db import async_session_factory, set_local_org_context
from app.documents.storage import get_storage_backend
from app.models import Project
from app.reports.models import ReportScheduleConfig
from app.reports.service import ExportBlocked, export_report

logger = logging.getLogger("app.reports.schedule")


async def _discover_due_schedules() -> list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]]:
    """(organisation_id, project_id, schedule_id) for every active schedule,
    across every tenant. The one deliberate RLS bypass in this module, same
    justification app/foresight/worker.py's _discover_active_projects
    already gives: no service-account/agent identity exists yet in this
    codebase to run a platform-level scan as. Every subsequent read/write
    for a discovered schedule still runs through the ordinary RLS-enforced
    session with that project's own org context set."""
    engine = create_async_engine(get_settings().migration_database_url)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT p.organisation_id, rsc.project_id, rsc.id "
                        "FROM report_schedule_configs rsc "
                        "JOIN projects p ON p.id = rsc.project_id "
                        "WHERE rsc.active = true AND p.archived_at IS NULL"
                    )
                )
            ).all()
            return [(row.organisation_id, row.project_id, row.id) for row in rows]
    finally:
        await engine.dispose()


def _is_due(config: ReportScheduleConfig, project: Project, now_utc: datetime) -> bool:
    """Hour-level granularity, matched against the cron tick that calls this
    function (app/foresight/worker.py's own WorkerSettings runs every 15
    minutes) — `minute_local` is stored for a future finer-grained scheduler
    to read, not checked here, a documented simplification rather than a
    missed requirement (FR-RPT-09 says "ahead of a recurring client
    meeting," which an hour-granularity schedule already satisfies)."""
    local_now = now_utc.astimezone(ZoneInfo(project.timezone))
    if local_now.weekday() != config.day_of_week or local_now.hour != config.hour_local:
        return False
    if config.last_run_at is None:
        return True
    last_local = config.last_run_at.astimezone(ZoneInfo(project.timezone))
    already_ran_this_hour = last_local.date() == local_now.date() and last_local.hour == local_now.hour
    return not already_ran_this_hour


async def run_due_report_schedules(ctx: dict | None = None) -> int:
    """The arq cron job body — also directly callable (by tests, or a
    one-off ops invocation) without a running worker/broker, same contract
    app/foresight/worker.py's run_foresight_sweep already establishes.
    Returns the number of snapshots generated."""
    storage = get_storage_backend()
    targets = await _discover_due_schedules()
    now_utc = datetime.now(dt_timezone.utc)
    generated = 0

    for organisation_id, project_id, schedule_id in targets:
        async with async_session_factory() as session:
            # set_local_org_context (app/core/db.py), not
            # org_scoped_transaction: this whole block ends in its own
            # explicit commit-or-rollback (below), not an unconditional
            # commit — `is_local=true` covers it either way, since Postgres
            # resets it at the end of the current transaction regardless of
            # which of the two the try/except actually takes.
            await set_local_org_context(session, organisation_id)
            project = (
                await session.execute(select(Project).where(Project.id == project_id))
            ).scalar_one_or_none()
            config = (
                await session.execute(
                    select(ReportScheduleConfig).where(ReportScheduleConfig.id == schedule_id)
                )
            ).scalar_one_or_none()
            if project is None or config is None or not config.active:
                continue
            if not _is_due(config, project, now_utc):
                continue

            try:
                await export_report(
                    session,
                    project=project,
                    storage=storage,
                    format=config.format,
                    template_code=config.template_code,
                    actor_id=None,
                    trigger="scheduled",
                )
                config.last_run_at = now_utc
                await session.commit()
                generated += 1
            except ExportBlocked as e:
                await session.rollback()
                logger.info(
                    "scheduled report for project %s skipped (monetary fields unverified): %s",
                    project_id, e,
                )
            except Exception:
                await session.rollback()
                logger.exception("scheduled report generation failed for project %s", project_id)

    return generated
