import uuid
from datetime import datetime

from sqlalchemy import Enum, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, Timestamped, TZDateTime, UUIDPk

# Fixed, closed set — bounded by which integrations the platform actually implements
# an adapter for. Not domain vocabulary, so it stays a native enum rather than an
# ontology_terms row (see CUE-Tech-Stack.md §5 for that distinction).
# "manual" (added alongside the audit trail / lifecycle work, migration
# d05dce0f415d) is the one non-integration value: FR-LED-10's evidence for a
# commitment entered by hand, not captured off a channel at all.
ChannelType = Enum(
    "whatsapp", "wechat", "teams", "outlook", "sharepoint", "manual", name="channel_type"
)


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


class Channel(Base, UUIDPk, Timestamped):
    """PRD §4.1: CHANNEL ||--o{ MESSAGE : carries"""

    __tablename__ = "channels"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True
    )
    type: Mapped[str] = mapped_column(ChannelType)
    external_ref: Mapped[str | None] = mapped_column(
        default=None, comment="Group ID / mailbox / drive ID, per channel type"
    )
    healthy: Mapped[bool] = mapped_column(default=True)
