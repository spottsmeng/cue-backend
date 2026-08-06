import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# Every point-in-time column in this schema must be timestamptz, per PRD §4.3
# ("due_at | timestamptz?" etc.). SQLAlchemy's default mapping for `datetime` is
# a timezone-naive DateTime unless told otherwise — use this explicitly on every
# hand-declared datetime column, not just via the Timestamped mixin below.
TZDateTime = DateTime(timezone=True)


class UUIDPk:
    """UUID primary key, generated application-side (no pgcrypto extension needed)."""

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now(), onupdate=func.now()
    )
