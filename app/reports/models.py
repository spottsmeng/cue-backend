import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Enum, ForeignKey, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, Timestamped, TZDateTime, UUIDPk

# Living WIP / reporting-domain models (PRD §6.10, FR-RPT — Prompt 8) live
# here, not in app/models/ledger.py — same per-domain-module-owns-its-models
# precedent app/twin/models.py, app/documents/models.py and
# app/foresight/models.py already established.
#
# Only two things about a report actually need persistence. The "current"
# report (§12.2: "never a refresh button") is recomputed fresh from
# Commitment/Budget/Milestone/Risk/Deviation/AuditLog on every read
# (app/reports/composer.py) — there is nothing to cache and nothing to go
# stale, so no table backs it. What *does* need a table:
#
# 1. ReportSnapshot (FR-RPT-08): an export is a point-in-time artefact a
#    Producer presents to a client — it must still read the same tomorrow
#    even after the underlying ledger has moved on. Immutable by construction
#    (the migration's own append-only trigger, no UPDATE/DELETE at all — this
#    table has no legitimate mutation the way twin_audit_log's SET-NULL-on-
#    delete carve-out needs, since nothing else points at it via FK).
# 2. ReportScheduleConfig (FR-RPT-09, Should): the one piece of "does a
#    recurring export need to happen" state a scheduled arq job (reusing
#    app/foresight/worker.py's WorkerSettings, per Prompt 8's own "ride the
#    arq worker... don't over-invest" instruction) has to read.

ReportFormat = Enum("pptx", "pdf", name="report_format")
ReportTrigger = Enum("manual", "scheduled", name="report_trigger")


class ReportSnapshot(Base, UUIDPk):
    """FR-RPT-08: an immutable export. `report_json` is the full composed
    report (app/reports/schema.py's LivingWipReportOut, as a dict) at the
    moment of export — kept alongside the rendered file itself
    (`storage_ref`, via app/documents/storage.py's StorageBackend, reused
    rather than a second object-store abstraction) so a snapshot is
    self-contained and independently reproducible even if the rendering
    pipeline changes later. Exporting the same underlying state twice
    creates two distinct rows with two distinct storage keys — never an
    overwrite of an existing one (WHAT TO BUILD #2's own instruction).
    """

    __tablename__ = "report_snapshots"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True, comment="RLS scope"
    )
    format: Mapped[str] = mapped_column(ReportFormat)
    template_code: Mapped[str] = mapped_column(comment="app/reports/templates.py registry key")
    storage_ref: Mapped[str] = mapped_column(
        comment="StorageBackend object key for the rendered pptx/pdf bytes"
    )
    report_json: Mapped[dict] = mapped_column(JSONB, comment="LivingWipReportOut as composed at export time")
    trigger: Mapped[str] = mapped_column(ReportTrigger)
    generated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), default=None,
        comment="null for a scheduled/system-triggered export",
    )
    generated_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())


class ReportScheduleConfig(Base, UUIDPk, Timestamped):
    """FR-RPT-09/10's config surface — a recurring export request ("every
    Monday at 9am local, as a PPTX, in the Acme template") plus FR-RPT-10's
    per-client template selection. Deliberately minimal, per Prompt 8's own
    instruction not to over-invest here relative to the Must-priority items:
    hour/day granularity (no timezone-of-its-own — local to the project's
    own `Project.timezone`, same "operational rhythm is a project-level
    property" reasoning app/foresight/models.py's QuietHoursConfig already
    applies), and execution is a plain scan
    (app/reports/schedule.py's run_due_report_schedules) riding the same arq
    worker process app/foresight/worker.py already stood up — not a new
    scheduler.
    """

    __tablename__ = "report_schedule_configs"
    __table_args__ = (
        CheckConstraint("day_of_week >= 0 AND day_of_week <= 6", name="report_schedule_day_of_week_range"),
        CheckConstraint("hour_local >= 0 AND hour_local <= 23", name="report_schedule_hour_local_range"),
        CheckConstraint(
            "minute_local >= 0 AND minute_local <= 59", name="report_schedule_minute_local_range"
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True, comment="RLS scope"
    )
    day_of_week: Mapped[int] = mapped_column(comment="0=Monday .. 6=Sunday, local to Project.timezone")
    hour_local: Mapped[int] = mapped_column()
    minute_local: Mapped[int] = mapped_column(default=0)
    format: Mapped[str] = mapped_column(ReportFormat)
    template_code: Mapped[str] = mapped_column(default="cue_placeholder")
    active: Mapped[bool] = mapped_column(default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
