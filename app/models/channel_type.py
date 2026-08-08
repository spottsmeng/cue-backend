import uuid

from sqlalchemy import CheckConstraint, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, Timestamped, UUIDPk

# The axis above brand/channel-type: what kind of thing a channel_types row
# lets an adapter do. Small and slow-moving by nature (tied 1:1 to
# ChannelAdapter's own method surface), unlike `code` below — kept as a
# plain CHECK-constrained column, not a second reference table, per this
# session's own design note (CUE-Tech-Stack.md §5.2).
CAPABILITIES = ("external_vendor_chat", "team_collaboration", "email", "file_storage")


class ChannelType(Base, UUIDPk, Timestamped):
    """Reference data behind what `channels.type` / `channel_identities.
    channel_type` (both FK this table by `code`, not `id`) name. Retires the
    native `channel_type` Postgres enum that used to live on
    app/models/project.py — that enum's own comment already argued "fixed,
    closed set bounded by which integrations the platform implements an
    adapter for", which is exactly why it needed to become insertable data,
    not a migration, the moment "which integrations the platform
    implements" became a swap-in/swap-out product requirement rather than a
    fixed Pico-only list. See CUE-Tech-Stack.md §5.2 for why this is a
    second reference table, deliberately not folded into `ontology_terms`
    (natural-key FK vs surrogate-id, no bilingual/hierarchy need) — and for
    why every *other* closed enum in this codebase (CommitmentState,
    ConsentStatus, etc.) correctly stays a native enum, unaffected by this.

    `capability` groups codes that can substitute for one another within a
    project (`teams`/`mattermost` both `team_collaboration`) — a tenant's
    real backend for a capability is which channel_types row(s) a project
    attaches, a config choice, not an architecture one. NULL only for
    `manual` (FR-LED-10's non-integration provenance value): not a real
    integration, so it has no capability to belong to.

    `organisation_id` NULL = platform-shipped (every row this session
    seeds). Set = one tenant's own future addition — the schema hook for
    the not-yet-built Configuration UI, same "mechanism exists, not a v1
    feature commitment" posture MilestoneArchetype.organisation_id already
    has for tenant-authored archetypes.
    """

    __tablename__ = "channel_types"
    __table_args__ = (
        UniqueConstraint("code", name="channel_types_code_key"),
        CheckConstraint(
            "capability IS NULL OR capability IN "
            "('external_vendor_chat', 'team_collaboration', 'email', 'file_storage')",
            name="channel_types_capability_known_value",
        ),
    )

    code: Mapped[str] = mapped_column(
        comment="stable machine key, never renamed once referenced — the FK "
        "target for channels.type / channel_identities.channel_type"
    )
    capability: Mapped[str | None] = mapped_column(default=None)
    organisation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organisations.id"), default=None, index=True
    )
    active: Mapped[bool] = mapped_column(default=True)
