import uuid

from sqlalchemy import ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamped, UUIDPk


class Organisation(Base, UUIDPk, Timestamped):
    """The tenant root. RLS policies scope every tenant-owned table to one row here.

    PRD §4.1: ORGANISATION ||--o{ PROJECT : owns
    """

    __tablename__ = "organisations"

    name: Mapped[str] = mapped_column()
    default_vertical_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("verticals.id"),
        default=None,
        comment="Pre-fills new projects; a tenant is not restricted to one vertical (PRD §4.2.1)",
    )
