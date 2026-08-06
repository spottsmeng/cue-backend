import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TZDateTime, UUIDPk


class OntologyTerm(Base, UUIDPk):
    """Versioned reference data for the event-production ontology (PRD §4.2):
    deliverable classes, milestone types, vendor categories, deviation classes,
    commitment act types. A new term is an insert, never a migration.

    Layered by (vertical_id, organisation_id) so the same table supports
    multiple industries and per-tenant extension without a redesign —
    see CUE-Tech-Stack.md §5.1 and CUE-PRD.md §4.2.1:

      vertical_id NULL, organisation_id NULL  -> universal core (e.g. commitment_act)
      vertical_id SET,  organisation_id NULL  -> platform vertical pack (e.g. event-production)
      vertical_id SET,  organisation_id SET   -> one tenant's extension within their vertical

    A project's effective ontology for a category is the union of all three,
    resolved at query time, never copied.
    """

    __tablename__ = "ontology_terms"
    __table_args__ = (
        # nulls_not_distinct: without it, Postgres treats NULL != NULL, so two
        # identical universal-core rows (vertical_id AND organisation_id both
        # NULL) would NOT violate this constraint — silently allowing duplicate
        # 'commitment_act'/'confirm' rows. Requires Postgres 15+ (we're on 17).
        UniqueConstraint(
            "category", "code", "vertical_id", "organisation_id",
            postgresql_nulls_not_distinct=True,
        ),
    )

    category: Mapped[str] = mapped_column(
        index=True,
        comment="'deliverable_class' | 'milestone_type' | 'vendor_category' | "
        "'commitment_act' | 'deviation_class' | 'venue_construct' | 'phase'",
    )
    code: Mapped[str] = mapped_column(comment="stable machine key, never renamed once referenced")
    label_en: Mapped[str] = mapped_column()
    label_zh: Mapped[str] = mapped_column()

    vertical_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("verticals.id"), default=None, index=True
    )
    organisation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organisations.id"), default=None, index=True
    )

    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ontology_terms.id"), default=None
    )
    sort_order: Mapped[int] = mapped_column(default=0)
    active: Mapped[bool] = mapped_column(default=True)
    effective_from: Mapped[datetime] = mapped_column(TZDateTime)
    effective_to: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    version: Mapped[int] = mapped_column(default=1)
