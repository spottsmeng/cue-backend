"""Orchestration for the report resource — `current` (a plain read, no
persistence involved), and `export` (FR-RPT-06's verification gate, then
FR-RPT-08's immutable snapshot). app/api/reports.py is the thin HTTP layer
over this module, same split app/documents/service.py draws for its domain.
"""

import uuid
from datetime import datetime, timezone as dt_timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.documents.storage import StorageBackend
from app.models import Commitment, Project
from app.reports.composer import compose_report, compute_budget_summary
from app.reports.export_pdf import render_pdf
from app.reports.export_pptx import render_pptx
from app.reports.models import ReportSnapshot
from app.reports.render import build_render_model
from app.reports.schema import LivingWipReportOut
from app.reports.templates import resolve_template

_CONTENT_TYPES = {
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "pdf": "application/pdf",
}


class ExportBlocked(Exception):
    """FR-RPT-06: raised with the exact commitments blocking export — the
    API layer turns this into the 409 body naming them, per Prompt 8's own
    testing expectation ("the right commitment named in the response")."""

    def __init__(self, blocking: list[Commitment]):
        self.blocking = blocking
        super().__init__(
            "export blocked: "
            + ", ".join(f"{c.id} ({c.deliverable_en!r})" for c in blocking)
            + " still pending_verification"
        )


async def get_current_report(
    session: AsyncSession, project: Project, storage: StorageBackend
) -> LivingWipReportOut:
    """FR-RPT-01: recomputed fresh on every read — no cache (see
    app/reports/models.py's module docstring for why no table backs this)."""
    return await compose_report(session, project, storage)


async def export_report(
    session: AsyncSession,
    *,
    project: Project,
    storage: StorageBackend,
    format: str,
    template_code: str | None,
    actor_id: uuid.UUID | None,
    trigger: str,
) -> ReportSnapshot:
    """FR-RPT-06 then FR-RPT-07/08: checked before a single byte is
    rendered — an unverified monetary field blocks the whole export, not
    just the figure it belongs to, since a partial deck with some numbers
    silently omitted would misrepresent the project's status as much as a
    fabricated one would (§8.4).

    Does not commit — same transaction-boundary convention as every other
    service-layer function in this codebase; the caller (the API endpoint,
    or app/reports/schedule.py's scheduled runner) owns that.
    """
    _budget_summary, blocking = await compute_budget_summary(session, project)
    if blocking:
        raise ExportBlocked(blocking)

    report = await compose_report(session, project, storage)
    template = resolve_template(template_code)
    resolved_template_code = template_code or "cue_placeholder"
    render_model = build_render_model(report, template)

    if format == "pptx":
        file_bytes = await render_pptx(render_model)
    elif format == "pdf":
        file_bytes = await render_pdf(render_model)
    else:
        raise ValueError(f"unsupported export format: {format!r}")

    # A fresh, unique key per export — WHAT TO BUILD #2's "a second export
    # of the same underlying state is a new snapshot, not an overwrite,"
    # same never-mutate-original-bytes posture
    # app/documents/storage.py's MinioStorageBackend already establishes.
    storage_ref = f"reports/{project.id}/{uuid.uuid4()}/report.{format}"
    await storage.put(storage_ref, file_bytes, _CONTENT_TYPES[format])

    snapshot = ReportSnapshot(
        project_id=project.id,
        format=format,
        template_code=resolved_template_code,
        storage_ref=storage_ref,
        report_json=report.model_dump(mode="json"),
        trigger=trigger,
        generated_by=actor_id,
        generated_at=datetime.now(dt_timezone.utc),
    )
    session.add(snapshot)
    await session.flush()
    return snapshot


async def list_snapshots(session: AsyncSession, project: Project) -> list[ReportSnapshot]:
    return list(
        (
            await session.execute(
                select(ReportSnapshot)
                .where(ReportSnapshot.project_id == project.id)
                .order_by(ReportSnapshot.generated_at.desc())
            )
        )
        .scalars()
        .all()
    )


async def get_snapshot(
    session: AsyncSession, project: Project, snapshot_id: uuid.UUID
) -> ReportSnapshot | None:
    return (
        await session.execute(
            select(ReportSnapshot).where(
                ReportSnapshot.id == snapshot_id, ReportSnapshot.project_id == project.id
            )
        )
    ).scalar_one_or_none()


__all__ = [
    "ExportBlocked",
    "get_current_report",
    "export_report",
    "list_snapshots",
    "get_snapshot",
]
