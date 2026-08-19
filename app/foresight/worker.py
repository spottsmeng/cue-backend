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

from arq import cron, func
from arq.connections import RedisSettings
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.ask.embed_worker import run_embedding_sweep
from app.capture.health import run_capture_health_sweep
from app.capture.reconciliation import run_gap_reconciliation_sweep
from app.capture.schedule import run_due_extraction_schedules
from app.capture.worker import ingest_channel_job
from app.core.config import get_settings
from app.core.db import async_session_factory, engine, org_scoped_transaction
from app.foresight.config import get_arq_settings
from app.foresight.contradiction import scan_contradictions
from app.foresight.escalation import escalate_unacknowledged_risks
from app.foresight.forecast import scan_forecast, scan_overdue_commitments
from app.foresight.notification import deliver_due_notifications
from app.foresight.silence import scan_silence
from app.layer_a.poller import run_layer_a_poll_sweep
from app.models import Project
from app.observability.drift import run_asr_drift_check, run_extraction_drift_check
from app.observability.otel import configure_otel, traced_job
from app.reports.schedule import run_due_report_schedules

logger = logging.getLogger("app.foresight.worker")

# No FastAPI `app` here — this is the arq worker process, a separate
# `uv run arq ...` invocation from the API server main.py starts; each
# process configures OTel for itself. `engine` passed so the SQLAlchemy
# instrumentation covers this process's own DB writes too, not just the
# API's.
configure_otel("cue-arq-worker", engine=engine)


async def run_project_sweep(session: AsyncSession, project: Project) -> None:
    """Runs every Foresight detection/maintenance pass for one project, in a
    fixed order — silence and contradiction first, since forecast.py's own
    heuristic reads an *open* Silence Radar flag; running forecast after
    them makes a same-sweep silence flag visible to it immediately rather
    than one sweep cycle later. Commits after each sub-scan rather than
    once at the end, so a bug in one detector can't roll back another's
    already-good work for the same project — each sub-scan's own
    `org_scoped_transaction` (app/core/db.py) re-asserts RLS context right
    before that commit, since a commit releases the session's connection
    back to the pool and a concurrent job in this same worker process could
    otherwise reuse it before the next sub-scan runs (see that context
    manager's own docstring for why a single set-once-per-sweep call is
    unsafe here)."""
    async with org_scoped_transaction(session, project.organisation_id):
        await scan_silence(session, project)
    async with org_scoped_transaction(session, project.organisation_id):
        await scan_contradictions(session, project)
    async with org_scoped_transaction(session, project.organisation_id):
        await scan_forecast(session, project)
    async with org_scoped_transaction(session, project.organisation_id):
        await scan_overdue_commitments(session, project)
    async with org_scoped_transaction(session, project.organisation_id):
        await escalate_unacknowledged_risks(session, project)
    async with org_scoped_transaction(session, project.organisation_id):
        await deliver_due_notifications(session, project)


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
            async with org_scoped_transaction(session, organisation_id):
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

    `run_due_report_schedules` (FR-RPT-09, app/reports/schedule.py),
    `run_embedding_sweep` (Prompt 9, app/ask/embed_worker.py),
    `ingest_channel_job` (Prompt 11 item 3, app/capture/worker.py),
    `run_capture_health_sweep` (item 9, app/capture/health.py),
    `run_gap_reconciliation_sweep` (item 10, app/capture/reconciliation.py)
    and `run_due_extraction_schedules` (item 11, app/capture/schedule.py)
    all ride this same worker process rather than standing up a second
    arq/Valkey pair — the same "don't over-invest in scheduling machinery"
    instruction Prompt 8 gave, applied again here. Not a Foresight concern
    living here by accident; this class is simply the one background-job
    process this codebase runs at all. `ingest_channel_job` is enqueued on
    demand (item 10's own reconciliation, item 11's own schedule reader),
    not itself a cron_jobs entry — unlike the other five, there is no fixed
    "run every 15 minutes for every X" shape to it: which channels need
    ingesting and when is exactly what item 10/11's own cron jobs (below)
    decide. `run_capture_health_sweep` *is* a fixed-cadence cron_jobs entry
    (every channel, every 15 minutes) — FR-CAP-09's own SLA number, not this
    module's usual "not tuned against any measured load" disclaimer.
    """

    # M10 (NFR-OBS-01): every job body wrapped in one span
    # (app/observability/otel.py's traced_job) — wrapped once here, not at
    # each job's own definition, and the *same* wrapped object referenced
    # in both functions/cron_jobs below (arq dispatches by function
    # __name__, which functools.wraps preserves) so on-demand dispatch and
    # the scheduled tick both go through the same traced entry point.
    _traced_foresight_sweep = traced_job(run_foresight_sweep)
    _traced_report_schedules = traced_job(run_due_report_schedules)
    _traced_embedding_sweep = traced_job(run_embedding_sweep)
    _traced_capture_health_sweep = traced_job(run_capture_health_sweep)
    _traced_gap_reconciliation_sweep = traced_job(run_gap_reconciliation_sweep)
    _traced_extraction_drift_check = traced_job(run_extraction_drift_check)
    _traced_asr_drift_check = traced_job(run_asr_drift_check)
    _traced_layer_a_poll_sweep = traced_job(run_layer_a_poll_sweep)
    # Bare (not `arq.func`-wrapped) — `arq.cron`'s own `coroutine` argument
    # requires a real `inspect.iscoroutinefunction`, and raises if handed a
    # `Function` wrapper instead (confirmed against arq's own source, not
    # assumed); its per-function timeout override is applied directly on
    # the `cron(...)` call below instead, which takes its own `timeout=`.
    _traced_extraction_schedules = traced_job(run_due_extraction_schedules)

    # arq's own worker-wide default (`job_timeout=300`) is too short for
    # either of these: both eventually call app/capture/pipeline.py's
    # `ingest_channel_backlog`, which makes one real per-message LLM
    # extraction call — genuinely observed taking several minutes for a
    # single-digit-message backlog against local Ollama, so a channel with
    # a real backlog routinely exceeds 300s. A job arq cancels mid-flight
    # for exceeding job_timeout doesn't just fail cleanly: it can leave the
    # cancelled greenlet-bridged async DB call in a corrupted state
    # (`sqlalchemy.exc.MissingGreenlet` on the next statement) — found for
    # real via this exact path, surfaced through the new capture debug
    # console's own real status reporting (`GET .../capture/status`) once
    # that made a previously-silent, always-eventually-retried failure
    # directly visible for the first time. A per-function `timeout=`
    # override (not a worker-wide bump, which would also give every fast
    # cron sweep the same generous allowance it doesn't need) is the fix —
    # 30 minutes, a documented, generous-but-bounded starting point (not
    # tuned against any measured real-backlog-size distribution, there is
    # none yet), not the previous unbounded-in-practice risk of simply
    # removing the timeout. `ingest_channel_job` is only ever dispatched
    # on-demand (never cron'd), so `arq.func`'s own `timeout=` is enough
    # for it; `run_due_extraction_schedules` is cron-only in this codebase,
    # so its equivalent override lives on the `cron(...)` call itself
    # below, not here — this constant is shared by both call sites so they
    # can't silently drift apart.
    _INGEST_JOB_TIMEOUT_SECONDS = 1800
    _traced_ingest_channel_job = func(
        traced_job(ingest_channel_job), timeout=_INGEST_JOB_TIMEOUT_SECONDS
    )

    functions = [
        _traced_foresight_sweep, _traced_report_schedules, _traced_embedding_sweep,
        _traced_ingest_channel_job, _traced_capture_health_sweep, _traced_gap_reconciliation_sweep,
        _traced_extraction_schedules, _traced_extraction_drift_check, _traced_asr_drift_check,
        _traced_layer_a_poll_sweep,
    ]
    # Every 15 minutes — frequent enough that a real silence/forecast/
    # escalation condition surfaces promptly, infrequent enough not to
    # hammer Postgres with a full-tenant scan; not tuned against any
    # measured load (there is none yet), a documented starting point like
    # every other threshold in this module, adjustable without a code
    # change once there's real traffic to tune against. The report schedule
    # scan shares this same cadence (app/reports/schedule.py's `_is_due`
    # docstring: hour-granularity schedules don't need a finer tick).
    # run_capture_health_sweep's own 15-minute cadence is not this same
    # "arbitrary starting point" — it is FR-CAP-09's literal SLA number.
    cron_jobs = [
        cron(_traced_foresight_sweep, minute=set(range(0, 60, 15))),
        cron(_traced_report_schedules, minute=set(range(0, 60, 15))),
        cron(_traced_embedding_sweep, minute=set(range(0, 60, 15))),
        cron(_traced_capture_health_sweep, minute=set(range(0, 60, 15))),
        # FR-CAP-10's own scheduled-window reader — same tick as every
        # other 15-minute sweep; a schedule's own interval_minutes (checked
        # by _is_due, app/capture/schedule.py) is what actually governs how
        # often a given channel's window fires, this is just how often the
        # check itself runs. `timeout=_INGEST_JOB_TIMEOUT_SECONDS` — this
        # loops over every due schedule's own real `ingest_channel_backlog`
        # call, the identical slow-LLM-extraction exposure
        # `_traced_ingest_channel_job`'s own timeout override exists for.
        cron(
            _traced_extraction_schedules,
            minute=set(range(0, 60, 15)),
            timeout=_INGEST_JOB_TIMEOUT_SECONDS,
        ),
        # FR-CAP-08's own reconciliation job — deliberately less frequent
        # than the health check above: a triggered backfill calls a real
        # fetch_backlog() against live channel infrastructure, materially
        # heavier than a health ping, so this runs on the half-hour rather
        # than every 15 minutes.
        cron(_traced_gap_reconciliation_sweep, minute={0, 30}),
        # M10 (NFR-OBS-05/FR-VOI-06): extraction-accuracy drift daily —
        # cheap while production config is still Ollama (the default), and
        # "regular cadence" is Prompt 13's own phrasing, no exact number
        # pinned, unlike ASR below. ASR drift monthly — Prompt 13's own
        # explicit number for that capability specifically.
        cron(_traced_extraction_drift_check, hour={3}, minute={0}),
        cron(_traced_asr_drift_check, day={1}, hour={3}, minute={0}),
        # Layer A observability (task-layer-A-observability-dashboard-prompt.
        # txt) — every 1 minute, not this module's usual 15-minute default:
        # the sustained-disconnect alert's own default threshold is 5
        # minutes, and the reconnect-flapping window defaults to 10 — a
        # 15-minute poll cadence would routinely miss both entirely rather
        # than just detecting them a little late. Cheap per tick (a handful
        # of small HTTP calls to one Layer A process, opt-in per
        # organisation via layer_a_alert_config.enabled — see
        # app/layer_a/poller.py), so the tighter cadence costs little.
        # timeout= override for the same MissingGreenlet-risk reason
        # _INGEST_JOB_TIMEOUT_SECONDS exists — generous relative to this
        # job's actual expected runtime, not tuned against measured load.
        cron(_traced_layer_a_poll_sweep, minute=set(range(0, 60, 1)), timeout=120),
    ]
    redis_settings = RedisSettings(host=_arq_settings.redis_host, port=_arq_settings.redis_port)
