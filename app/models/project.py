import uuid
from datetime import datetime

from sqlalchemy import ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, Timestamped, TZDateTime, UUIDPk


class Project(Base, UUIDPk, Timestamped):
    """PRD §4.1: PROJECT is the scope root for channels, commitments, documents,
    milestones, deviations, memberships, meetings and the budget baseline."""

    __tablename__ = "projects"

    organisation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organisations.id"), index=True
    )
    vertical_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("verticals.id"), index=True
    )
    name: Mapped[str] = mapped_column()
    client_name: Mapped[str | None] = mapped_column(default=None)
    venue: Mapped[str | None] = mapped_column(default=None)
    timezone: Mapped[str] = mapped_column(default="Asia/Singapore")
    event_start: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    event_end: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    archived_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    archetype_code: Mapped[str | None] = mapped_column(
        default=None,
        comment=(
            "FR-VRG-02's 'event archetype' segmentation axis. Set once, at "
            "materialize_archetype time (app/twin/service.py), to whichever "
            "MilestoneArchetype was actually resolved for this project — "
            "that table is a template copied from and never referenced "
            "again afterward (backend/PROGRESS.md's M1 notes), so this is "
            "the only place a project's own archetype choice survives. "
            "Null for a project materialized before this column existed, "
            "or one whose vertical/org had no archetype to resolve at all."
        ),
    )
    writeback_daily_ceiling: Mapped[int] = mapped_column(
        default=1,
        comment=(
            "FR-WBK-04's hard ceiling — outbound messages per group (channel) "
            "per local calendar day, enforced by app/writeback/rate_limit.py "
            "against real OutboundMessage rows, not an application counter. "
            "Changed only via PATCH /projects/{id}/writeback/config "
            "(ADMIN_ROLES-gated, audited via WritebackAuditLog) — never a "
            "bare column write, per CLAUDE.md's 'no code path, including an "
            "admin override, may exceed it without an explicit, audited "
            "configuration change' instruction."
        ),
    )


class Channel(Base, UUIDPk, Timestamped):
    """PRD §4.1: CHANNEL ||--o{ MESSAGE : carries"""

    __tablename__ = "channels"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True
    )
    type: Mapped[str] = mapped_column(
        ForeignKey("channel_types.code"),
        comment="channel_types.code — which adapter/capability this channel is",
    )
    external_ref: Mapped[str | None] = mapped_column(
        default=None, comment="Group ID / mailbox / drive ID, per channel type"
    )
    display_name: Mapped[str | None] = mapped_column(
        default=None,
        comment=(
            "Human-readable label, cached at attach time — not authoritative "
            "and not kept in sync. 'Layer B Channel Picker' design choice: "
            "GET /conversations on Layer A resolves WhatsApp group/contact "
            "names live and would drift from a stored copy the moment a "
            "group is renamed, but re-resolving it on every channels list "
            "read would make that read depend on Layer A being reachable "
            "just to render already-attached channels. A once-off label "
            "captured from the picker at attach time (good-enough per "
            "'Names are not authoritative', never re-synced) was chosen "
            "over either a live join or a background refresh job, neither "
            "of which this session builds. Null for channel types with no "
            "discovery mechanism (external_ref alone still identifies them)."
        ),
    )
    healthy: Mapped[bool] = mapped_column(default=True)
    detached_at: Mapped[datetime | None] = mapped_column(
        TZDateTime,
        default=None,
        comment=(
            "Soft-delete marker for DELETE .../channels/{id} — never a hard "
            "delete, since messages already captured on this channel (and any "
            "Evidence built on them, per CLAUDE.md's 'no commitment without "
            "evidence') must survive detach. NULL means attached/active; "
            "list/get endpoints filter this out by default. Re-attaching the "
            "same external_ref always inserts a new Channel row rather than "
            "reactivating this one (see FR-CAP-06 — a re-join is a fresh "
            "consent event, not a resumed one)."
        ),
    )
