import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Numeric
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, TZDateTime, UUIDPk


class LLMUsageEvent(Base, UUIDPk):
    """NFR-OBS-03: per-model cost/token accounting attributed to
    project/organisation. An append-only event log, not a Timestamped
    entity (app/core/base.py) — no `updated_at`, a usage event is never
    edited after the call it describes completes. See app/llm/cost.py's
    own docstring for why this table exists instead of Langfuse."""

    __tablename__ = "llm_usage_events"

    organisation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organisations.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"), index=True, default=None
    )
    role: Mapped[str] = mapped_column()
    purpose: Mapped[str] = mapped_column()
    provider: Mapped[str] = mapped_column()
    model: Mapped[str] = mapped_column()
    tokens_in: Mapped[int | None] = mapped_column(default=None)
    tokens_out: Mapped[int | None] = mapped_column(default=None)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6), default=None)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, index=True, server_default="now()")
