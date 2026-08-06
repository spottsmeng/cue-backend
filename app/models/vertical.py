from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamped, UUIDPk


class Vertical(Base, UUIDPk, Timestamped):
    """One row per industry CUE serves — 'event-production' at v1.

    See CUE-Tech-Stack.md §5.1 for the layering this supports:
    universal core -> vertical pack -> tenant extension.
    """

    __tablename__ = "verticals"

    code: Mapped[str] = mapped_column(unique=True, comment="'event-production' | ...")
    name: Mapped[str] = mapped_column()
    active: Mapped[bool] = mapped_column(default=True)
