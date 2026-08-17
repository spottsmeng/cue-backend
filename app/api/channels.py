import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated

import httpx
from arq.connections import ArqRedis, RedisSettings, create_pool
from arq.jobs import Job, JobStatus
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_project, require_project_role
from app.api.schemas import (
    CapturePullResponse,
    CaptureStatusOut,
    ChannelCreate,
    ChannelHealthEventOut,
    ChannelHealthSignal,
    ChannelOut,
    MessageOut,
    WhatsAppConversationOut,
)
from app.capture.adapters.errors import CaptureConfigError
from app.capture.adapters.whatsapp import WhatsAppAdapter
from app.capture.models import ChannelHealthEvent, Message
from app.capture.worker import channel_job_id, enqueue_channel_ingestion
from app.core.db import get_session
from app.foresight.config import get_arq_settings
from app.identity.service import ADMIN_ROLES
from app.models import Channel, ChannelType, Project

# FR-ADM-06 names "attach channels" as part of project provisioning, the same
# admin-tier surface app/api/projects.py's add_member already gates with
# ADMIN_ROLES — health/reconnect are grouped under the same FR-ADM-09
# governance requirement, so this router gates every write the same way.
# There is no service-account/agent identity yet for a real capture agent to
# call the health endpoint as (out of scope this session, see Prompt 5) —
# when one exists, it will need its own auth path, not a project membership
# role; this is the placeholder until then.
_require_admin = require_project_role(*ADMIN_ROLES)

router = APIRouter(prefix="/projects/{project_id}/channels", tags=["channels"])


async def _refresh_updated_at(session: AsyncSession, channel: Channel) -> None:
    """`updated_at` has a server-side `onupdate=func.now()` — same
    MissingGreenlet trap app/api/commitments.py's _refresh_updated_at
    documents: after an UPDATE flush, SQLAlchemy expires it rather than
    fetching it eagerly, and a bare attribute access later would trigger a
    lazy-load outside the async greenlet bridge."""
    await session.refresh(channel, attribute_names=["updated_at"])


async def _resolve_channel_type(session: AsyncSession, code: str, *, require_capability: bool) -> str:
    """channel_types.code replaced ChannelTypeLiteral's static closed set
    (app/api/schemas.py) — validity is now a DB lookup, not a Python type,
    since the whole point of the reference-data table is that a new code is
    an insert, not a code change. `require_capability=True` is the FK-free
    equivalent of the old Literal's own "minus manual" carve-out: a Channel
    resource can never be "manual" (that value exists only for
    Evidence.channel's non-integration case, FR-LED-10)."""
    stmt = select(ChannelType.code).where(ChannelType.code == code, ChannelType.active.is_(True))
    if require_capability:
        stmt = stmt.where(ChannelType.capability.is_not(None))
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=422, detail=f"unknown or non-attachable channel type: {code!r}")
    return row


def _whatsapp_adapter() -> WhatsAppAdapter:
    try:
        return WhatsAppAdapter()
    except CaptureConfigError as e:
        raise HTTPException(status_code=503, detail=f"WhatsApp integration not configured: {e}") from e


@asynccontextmanager
async def _arq_redis() -> AsyncIterator[ArqRedis]:
    """Shared by `pull_channel_now` and `channel_capture_status` below —
    both need a real connection to the same arq/Valkey queue, neither owns
    a long-lived pool of its own (this codebase's API layer never enqueues
    or inspects arq jobs anywhere else)."""
    redis_settings = get_arq_settings()
    try:
        redis = await create_pool(
            RedisSettings(host=redis_settings.redis_host, port=redis_settings.redis_port)
        )
    except OSError as e:
        raise HTTPException(status_code=502, detail=f"capture queue unreachable: {e}") from e
    try:
        yield redis
    finally:
        await redis.aclose()


async def _grant_whatsapp_allowlist(channel_ref: str) -> None:
    """'Layer B Channel Picker' prompt: attaching a WhatsApp channel MUST
    grant Layer A's own Level C capture permission for it, in the same
    request — not a second, separate action left to the ops console.
    Called *before* the Channel row is inserted so a failed grant never
    leaves a Channel that silently captures nothing (see attach_channel)."""
    adapter = _whatsapp_adapter()
    try:
        await adapter.add_to_allowlist(channel_ref)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(
                status_code=503,
                detail="Layer A has no unambiguous WhatsApp account configured — cannot grant capture permission",
            ) from e
        raise HTTPException(status_code=502, detail=f"Layer A allowlist grant failed: {e}") from e
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Layer A unreachable: {e}") from e


async def _revoke_whatsapp_allowlist(channel_ref: str) -> None:
    """The matching revoke — detaching a WhatsApp channel MUST call this.
    Called before the Channel row is deleted, same fail-closed ordering as
    the grant above: if Layer A can't be reached, the Channel row stays so
    the detach can be retried, rather than deleting local state while the
    conversation keeps silently capturing on Layer A's side."""
    adapter = _whatsapp_adapter()
    try:
        await adapter.remove_from_allowlist(channel_ref)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(
                status_code=503,
                detail="Layer A has no unambiguous WhatsApp account configured — cannot revoke capture permission",
            ) from e
        raise HTTPException(status_code=502, detail=f"Layer A allowlist revoke failed: {e}") from e
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Layer A unreachable: {e}") from e


async def _get_channel(session: AsyncSession, project: Project, channel_id: uuid.UUID) -> Channel:
    """Only ever resolves an *attached* channel (`detached_at IS NULL`) —
    every endpoint below shares this lookup, so a detached channel 404s
    everywhere consistently: it can't receive health signals, be
    reconnected, be pulled, or be detached a second time, same as if the
    row were gone. Its messages remain queryable directly against
    `Message` (see detach_channel's docstring) — just not through this
    per-channel surface."""
    channel = (
        await session.execute(
            select(Channel).where(
                Channel.id == channel_id,
                Channel.project_id == project.id,
                Channel.detached_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if channel is None:
        raise HTTPException(status_code=404, detail="channel not found")
    return channel


@router.get("", response_model=list[ChannelOut])
async def list_channels(
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[Channel]:
    """Attached channels only (`detached_at IS NULL`) — a detached channel
    is offboarded, not archived-but-visible; its message history stays on
    the project (`GET .../messages` before detach, or a direct `Message`
    query) but it drops out of the picker/admin list the moment it's
    detached."""
    channels = (
        await session.execute(
            select(Channel).where(Channel.project_id == project.id, Channel.detached_at.is_(None))
        )
    ).scalars().all()
    return list(channels)


@router.get("/whatsapp/conversations", response_model=list[WhatsAppConversationOut])
async def list_whatsapp_conversations(
    project: Annotated[Project, Depends(_require_admin)],
) -> list[dict]:
    """WHAT TO BUILD #1 ('Layer B Channel Picker' prompt): backs the
    attach-flow's real conversation picker — proxies Layer A's live Machine
    API `GET /conversations`, gated the same ADMIN_ROLES tier as
    `attach_channel` below (the only caller this exists for). Deliberately
    a live lookup on every call, not cached beyond the request: Layer A
    resolves conversation names in real time (on reconnect for groups, via
    contact-sync for people), never from a static list — permanent caching
    here would go stale the same way. Not filtered by `project` — Layer A
    has no notion of "project" at all, only a WhatsApp account's own known
    conversations; `project` here only gates *who* may call this."""
    adapter = _whatsapp_adapter()
    try:
        return await adapter.list_conversations()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(
                status_code=503,
                detail="Layer A has no unambiguous WhatsApp account configured — conversation discovery unavailable",
            ) from e
        raise HTTPException(status_code=502, detail=f"Layer A conversation discovery failed: {e}") from e
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Layer A unreachable: {e}") from e


@router.post("", response_model=ChannelOut, status_code=201)
async def attach_channel(
    body: ChannelCreate,
    project: Annotated[Project, Depends(_require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Channel:
    """FR-ADM-06: 'attach channels' — the half of project provisioning the
    RBAC/delegation session left for this one (create/assign members was
    already built).

    WHAT TO BUILD #2 ('Layer B Channel Picker' prompt): for `type=
    "whatsapp"` with an `external_ref` (jid) selected from the picker
    above, this is the one call that both attaches the channel *and*
    grants Layer A's Level C capture permission for it — never two
    separate actions in two separate consoles. The allowlist grant runs
    *before* the Channel row is created: if Layer A rejects or is
    unreachable, nothing is persisted here either, so a PM never ends up
    with a Channel that reports success but silently captures nothing."""
    channel_type = await _resolve_channel_type(session, body.type, require_capability=True)
    if channel_type == "whatsapp" and body.external_ref:
        await _grant_whatsapp_allowlist(body.external_ref)
    channel = Channel(
        project_id=project.id,
        type=channel_type,
        external_ref=body.external_ref,
        display_name=body.display_name,
        healthy=True,
    )
    session.add(channel)
    await session.commit()
    return channel


@router.delete("/{channel_id}", status_code=204)
async def detach_channel(
    channel_id: uuid.UUID,
    project: Annotated[Project, Depends(_require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """WHAT TO BUILD #3 ('Layer B Channel Picker' prompt): the matching
    revoke — a WhatsApp channel detached here must stop being captured on
    Layer A's side too, not just disappear from this project's own channel
    list. Same fail-closed ordering as attach: the allowlist revoke runs
    before the Channel row is touched, so a Layer A failure leaves the
    Channel attached (retryable) instead of silently orphaning a
    still-active capture grant.

    Soft-delete, not `session.delete()`: a real Channel row already has
    `messages` hanging off it by the time anyone detaches it, and
    `messages_channel_id_fkey` has no `ON DELETE` action — deliberately, per
    CLAUDE.md's "no commitment without evidence" (a hard delete would either
    violate that FK and 500, or cascade and silently destroy Evidence for
    already-extracted commitments). `detached_at` stops capture without
    losing history; re-attaching the same `external_ref` later inserts a
    fresh Channel row (see attach_channel) rather than reviving this one."""
    channel = await _get_channel(session, project, channel_id)
    if channel.type == "whatsapp" and channel.external_ref:
        await _revoke_whatsapp_allowlist(channel.external_ref)
    channel.detached_at = datetime.now(timezone.utc)
    channel.healthy = False
    await session.commit()


@router.post("/{channel_id}/health", response_model=ChannelOut)
async def report_channel_health(
    channel_id: uuid.UUID,
    body: ChannelHealthSignal,
    project: Annotated[Project, Depends(_require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Channel:
    """FR-ADM-09: the receiving end of a capture-health signal — nothing
    calls this yet (a later milestone's real capture agents will), but the
    endpoint exists now so that milestone is wiring, not building, this
    surface. `body.detail` is accepted and returned to the caller but not
    persisted — Channel has no column for it, and no consumer of channel
    health history exists yet (out of scope this session)."""
    channel = await _get_channel(session, project, channel_id)
    channel.healthy = body.healthy
    await session.flush()
    await _refresh_updated_at(session, channel)
    await session.commit()
    return channel


@router.get("/{channel_id}/health/history", response_model=list[ChannelHealthEventOut])
async def channel_health_history(
    channel_id: uuid.UUID,
    project: Annotated[Project, Depends(_require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[ChannelHealthEvent]:
    """FR-CAP-09: the durable consumer surface over
    app/capture/health.py's run_capture_health_sweep — closes the gap
    backend/PROGRESS.md's M2 notes named ("no consumer of channel health
    history exists yet"). Most recent first."""
    channel = await _get_channel(session, project, channel_id)
    events = (
        await session.execute(
            select(ChannelHealthEvent)
            .where(ChannelHealthEvent.channel_id == channel.id)
            .order_by(ChannelHealthEvent.checked_at.desc())
        )
    ).scalars().all()
    return list(events)


@router.post("/{channel_id}/reconnect", response_model=ChannelOut)
async def reconnect_channel(
    channel_id: uuid.UUID,
    project: Annotated[Project, Depends(_require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Channel:
    """FR-ADM-09's reconnection workflow — an explicit admin action marking
    a degraded channel healthy again, distinct from a health signal simply
    reporting `healthy: true` on its own."""
    channel = await _get_channel(session, project, channel_id)
    channel.healthy = True
    await session.flush()
    await _refresh_updated_at(session, channel)
    await session.commit()
    return channel


@router.get("/{channel_id}/messages", response_model=list[MessageOut])
async def list_channel_messages(
    channel_id: uuid.UUID,
    project: Annotated[Project, Depends(_require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[Message]:
    """The debug console's own raw-capture view (project/tenant-admin
    gated, same `_require_admin` tier as everything else in this router) —
    real `Message` rows (app/capture/models.py), independent of extraction:
    a message shows up here the moment capture durably wrote it
    (NFR-AVL-02), whether or not extraction has run or produced anything.
    Chronological, oldest first — read as a conversation transcript, not a
    changelog."""
    channel = await _get_channel(session, project, channel_id)
    messages = (
        await session.execute(
            select(Message).where(Message.channel_id == channel.id).order_by(Message.sent_at.asc())
        )
    ).scalars().all()
    return list(messages)


@router.post("/{channel_id}/capture/pull-now", response_model=CapturePullResponse)
async def pull_channel_now(
    channel_id: uuid.UUID,
    project: Annotated[Project, Depends(_require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CapturePullResponse:
    """The 'manual "poll now" admin action' `app/capture/worker.py`'s own
    module docstring names as a real, anticipated consumer of
    `enqueue_channel_ingestion` — this is that consumer. Enqueues the real
    arq job the scheduled worker itself uses (`since=None`, the adapter's
    own "full backlog" convention), never a second, parallel ingestion
    path — same real consent-gating, identity resolution, dedup and
    extraction as any other trigger. Returns immediately (the job runs in
    the background); `GET .../messages` above is how a caller sees the
    result once it lands. A channel that already has a pull in flight
    (arq's own per-channel job-id dedup) returns `queued=False`, not an
    error.

    `_get_channel` below confirms `channel_id` actually belongs to
    `project` before anything is queued — `enqueue_channel_ingestion` takes
    a bare channel id with no project/org check of its own (the arq job
    body re-resolves the channel itself, org-unscoped), so skipping this
    would let an admin of *this* project enqueue ingestion for a channel
    belonging to an entirely different project by guessing its id.

    Callers should not have to guess what happened next by polling
    `GET .../messages` and hoping — `GET .../capture/status` (below) reports
    the real arq job state (queued / in progress / complete, with the
    actual result) behind this call, keyed by the same deterministic
    per-channel job id."""
    await _get_channel(session, project, channel_id)
    async with _arq_redis() as redis:
        job = await enqueue_channel_ingestion(
            redis, organisation_id=project.organisation_id, channel_id=channel_id, since=None
        )
    return CapturePullResponse(queued=job is not None)


@router.get("/{channel_id}/capture/status", response_model=CaptureStatusOut)
async def channel_capture_status(
    channel_id: uuid.UUID,
    project: Annotated[Project, Depends(_require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CaptureStatusOut:
    """The real status of whatever `POST .../capture/pull-now` most
    recently queued for this channel — meant to be polled by a debug UI
    that would otherwise have no way to tell "still running," "just
    finished with nothing new," and "genuinely stuck/failed" apart, short
    of repeatedly re-fetching `GET .../messages` and guessing. Reads arq's
    own job state directly (`arq.jobs.Job`, keyed by
    `app/capture/worker.py`'s `channel_job_id` — the same id `enqueue_
    channel_ingestion`'s own dedup already uses), never a second,
    separately-tracked status of this API's own invention that could drift
    from what arq itself thinks happened."""
    await _get_channel(session, project, channel_id)
    async with _arq_redis() as redis:
        job = Job(channel_job_id(channel_id), redis)
        status = await job.status()
        if status != JobStatus.complete:
            return CaptureStatusOut(status=status.value)

        info = await job.result_info()
        if info is None:
            # arq's own result TTL (`keep_result`, default 1 hour) expired
            # between the status check above and this read — genuinely
            # indistinguishable from "never queued" at this point, and
            # rendered identically client-side, so not treated as an error.
            return CaptureStatusOut(status="not_found")
        if not info.success:
            return CaptureStatusOut(status="complete", success=False, error=str(info.result))
        result = info.result if isinstance(info.result, dict) else {}
        if result.get("skipped"):
            return CaptureStatusOut(status="complete", success=True, skipped=True, skip_reason=result.get("reason"))
        return CaptureStatusOut(
            status="complete",
            success=True,
            received=result.get("received"),
            new_messages=result.get("new_messages"),
            duplicates=result.get("duplicates"),
            opted_out=result.get("opted_out"),
            commitments_created=result.get("commitments_created"),
            latest_sent_at=result.get("latest_sent_at"),
        )
