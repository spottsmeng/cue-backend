import uuid

from sqlalchemy import Enum, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, Timestamped, UUIDPk

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

    # FR-VRG-02 segmentation fields, added by the Vendor Reliability Graph
    # session (Prompt 10). `vendor_category_term_id` reuses the
    # `vendor_category` ontology_terms category this docstring's own
    # neighbour (app/models/ontology.py) has named since the first
    # migration but never seeded — see seed_data/vendor_categories.py.
    # `city` is a plain nullable column, not a lookup table: unlike vendor
    # category (a governed, versioned vocabulary a tenant might extend) or
    # event archetype (a per-project template choice, see Project's own
    # archetype_code), a vendor's city is free-text reference data with no
    # taxonomy or reuse need of its own.
    vendor_category_term_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ontology_terms.id"),
        default=None,
        comment="ontology_terms row, category='vendor_category'",
    )
    city: Mapped[str | None] = mapped_column(default=None)


class ChannelIdentity(Base, UUIDPk, Timestamped):
    """PRD FR-NRM-03: resolve phone / WeChat ID / UPN / email to one `party`,
    with confidence and manual override."""

    __tablename__ = "channel_identities"
    __table_args__ = (UniqueConstraint("channel_type", "external_id"),)

    party_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parties.id"), index=True
    )
    channel_type: Mapped[str] = mapped_column(ForeignKey("channel_types.code"))
    external_id: Mapped[str] = mapped_column(comment="phone / WeChat ID / UPN / email")
    confidence: Mapped[float] = mapped_column(default=1.0)
    manually_verified: Mapped[bool] = mapped_column(default=False)
