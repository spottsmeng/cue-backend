import uuid

from sqlalchemy import Enum, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, Timestamped, UUIDPk
from app.models.project import ChannelType

PartyType = Enum("person", "vendor_org", "internal_staff", name="party_type")


class Party(Base, UUIDPk, Timestamped):
    """PRD §4.1: PARTY — a resolved identity, org-scoped so the same vendor
    contact is one row across every project they appear in."""

    __tablename__ = "parties"

    organisation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organisations.id"), index=True
    )
    display_name: Mapped[str] = mapped_column()
    type: Mapped[str] = mapped_column(PartyType)


class ChannelIdentity(Base, UUIDPk, Timestamped):
    """PRD FR-NRM-03: resolve phone / WeChat ID / UPN / email to one `party`,
    with confidence and manual override."""

    __tablename__ = "channel_identities"
    __table_args__ = (UniqueConstraint("channel_type", "external_id"),)

    party_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parties.id"), index=True
    )
    channel_type: Mapped[str] = mapped_column(ChannelType)
    external_id: Mapped[str] = mapped_column(comment="phone / WeChat ID / UPN / email")
    confidence: Mapped[float] = mapped_column(default=1.0)
    manually_verified: Mapped[bool] = mapped_column(default=False)
