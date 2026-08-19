import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, Timestamped, TZDateTime, UUIDPk

# Layer A (layer-A/) is a standalone Express capture gateway, separate from
# this backend — it deliberately holds no database of its own (its own
# CredentialVault docstring: "no external dependency... deliberately, so
# this can be tested for real, in-process"). This package is what makes an
# incident there (two processes racing for the same WhatsApp session,
# diagnosed by live-attaching a debugger — no other path to visibility
# existed) diagnosable from a dashboard instead: the backend polls Layer A's
# admin API on an interval (poller.py) and persists what it sees here, in
# CUE's own Postgres, which is the only durable state this whole feature has.
#
# `account_id` throughout is Layer A's own opaque accountId string — not a
# CUE FK, since Layer A accounts have no first-class CUE identity today.
#
# organisation_id is a direct column on every table here (not a project
# join) — this is org-wide ops infrastructure, not project-scoped, same
# shape app/models/governance.py's RetentionPolicy already uses and the
# same "tenant_isolation" RLS policy shape (see the migration that creates
# these tables).

LayerASnapshotSource = Enum("poll", "transition", name="layer_a_snapshot_source")

# The 6-value closed set at Layer A's admin API boundary (layer-A/src/
# session-manager/index.ts's WorkerStatus, plus the "unknown" the admin
# route itself synthesizes when no state exists yet). Native enum, same
# "structural, not domain vocabulary" reasoning app/models/ledger.py's
# CommitmentState comment gives — widening it if Layer A ever adds a 7th
# status is a one-line ALTER TYPE ... ADD VALUE, same cost audit_action
# already pays for exactly this reason.
LayerAWorkerStatus = Enum(
    "connecting", "connected", "reconnecting", "unhealthy", "disconnected", "unknown",
    name="layer_a_worker_status",
)

LayerAAlertType = Enum(
    "sustained_disconnect", "reconnect_flapping", "session_conflict",
    name="layer_a_alert_type",
)
LayerAAlertSeverity = Enum("serious", "critical", name="layer_a_alert_severity")
LayerAAlertState = Enum("open", "resolved", name="layer_a_alert_state")
LayerADeliveryChannel = Enum("webhook", "email", "banner", name="layer_a_delivery_channel")


class LayerAHealthSnapshot(Base, UUIDPk):
    """Durable mirror of Layer A's health data, at finer and longer-lived
    granularity than Layer A's own in-memory, 200-entry-per-account ring
    buffer (HealthHistoryStore, wiped on every restart).

    Two distinct sources feed this table, and they carry different fields —
    `/admin/accounts` (a live read) returns a richer object (HealthResult)
    than `/admin/accounts/:id/health-history` (WorkerState only, no
    `detail`) — so `last_disconnect_reason`, `last_disconnect_status_code`,
    `healthy` and `risk_tier` are only ever populated on `source='poll'`
    rows; `source='transition'` rows (backfilled from Layer A's own history
    ring buffer each sweep) leave them null. `recorded_at` is the sweep
    time for poll rows, and Layer A's own HealthHistoryEntry.recordedAt
    (its transition timestamp) for transition rows.
    """

    __tablename__ = "layer_a_health_snapshots"
    __table_args__ = (
        # Partial — poll rows only. Poll rows are safely unique per (account,
        # sweep tick): recorded_at is this process's own now(), computed
        # once per account per sweep. Transition rows are NOT constrained
        # here: Layer A's own history timestamps are millisecond-resolution
        # Date.now() values, and two genuinely distinct transitions (e.g.
        # "connecting" immediately followed by "connected" during a fast
        # real startup) can legitimately share one millisecond — a plain
        # (non-partial) unique constraint on this same key was found, via
        # this feature's own real-fixture-server test, to silently drop the
        # second of two such real events. Cross-sweep dedup for transition
        # rows instead relies entirely on the poller's own since-cursor
        # filtering (app/layer_a/poller.py's _insert_new_transitions) rather
        # than a DB-level constraint.
        Index(
            "ix_layer_a_health_snapshots_poll_dedup_key",
            "organisation_id", "account_id", "source", "recorded_at",
            unique=True,
            postgresql_where=text("source = 'poll'"),
        ),
        Index(
            "ix_layer_a_health_snapshots_account_recorded",
            "organisation_id", "account_id", "recorded_at",
        ),
    )

    organisation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organisations.id"), index=True, comment="RLS scope"
    )
    account_id: Mapped[str] = mapped_column(index=True, comment="Layer A's own opaque accountId, not a CUE FK")
    source: Mapped[str] = mapped_column(LayerASnapshotSource)
    recorded_at: Mapped[datetime] = mapped_column(TZDateTime)
    status: Mapped[str] = mapped_column(LayerAWorkerStatus)
    connect_attempts: Mapped[int] = mapped_column(default=0)
    last_connected_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    last_message_timestamp: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    last_error: Mapped[str | None] = mapped_column(default=None)
    last_disconnect_reason: Mapped[str | None] = mapped_column(
        default=None, comment="poll rows only — not present on Layer A's /health-history endpoint"
    )
    last_disconnect_status_code: Mapped[int | None] = mapped_column(
        default=None, comment="poll rows only — the Boom status code, e.g. 440 = session conflict"
    )
    healthy: Mapped[bool | None] = mapped_column(default=None, comment="poll rows only")
    risk_tier: Mapped[str | None] = mapped_column(default=None, comment="poll rows only")
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now(), comment="ingestion time, distinct from recorded_at's event time"
    )


class LayerAConflictEvent(Base, UUIDPk):
    """Durable, unbounded copy of Layer A's own `GET /admin/conflicts` feed
    (its file-backed log is capped at the last 50 entries) — a pid-lock
    refusal on the losing process, the "another process tried to start"
    half of the session_conflict alert type.
    """

    __tablename__ = "layer_a_conflict_events"
    __table_args__ = (
        UniqueConstraint(
            "organisation_id", "detected_at", "refused_pid",
            name="layer_a_conflict_events_dedup_key",
        ),
    )

    organisation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organisations.id"), index=True, comment="RLS scope"
    )
    detected_at: Mapped[datetime] = mapped_column(TZDateTime)
    refused_pid: Mapped[int] = mapped_column()
    owner_pid: Mapped[int] = mapped_column()
    ingested_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())


class LayerAAlert(Base, UUIDPk):
    """One open/resolved lifecycle row per incident, across the three
    distinct alert types this feature names deliberately, per the product
    decision behind it: sustained_disconnect and reconnect_flapping are
    condition-shaped (they auto-resolve once the underlying condition
    clears — see app/layer_a/alerts.py); session_conflict is event-shaped
    (a conflict already happened, it doesn't "clear" — it stays open until
    an admin acknowledges it, and acknowledging *is* resolving it for this
    type only).

    `account_id` is null only for a session_conflict alert sourced from a
    pid-lock refusal, which is process-wide, not per-account.

    The partial unique index (only *open* rows constrained) lets the
    evaluator run every sweep safely via INSERT ... ON CONFLICT DO NOTHING
    with no separate "is one already open" query race.
    """

    __tablename__ = "layer_a_alerts"
    __table_args__ = (
        # nulls_not_distinct: a session_conflict alert sourced from a
        # pid-lock refusal has account_id=NULL (process-wide, not
        # per-account) — Postgres's default unique-index semantics treat
        # every NULL as distinct from every other NULL, which would let
        # two such alerts stay open simultaneously for the same org. This
        # flag is what makes NULL == NULL for this index's purposes, so
        # the same idempotent ON CONFLICT DO NOTHING the other two alert
        # types rely on also works for the account-less case.
        Index(
            "ix_layer_a_alerts_open_unique",
            "organisation_id", "alert_type", "account_id",
            unique=True,
            postgresql_where=text("state = 'open'"),
            postgresql_nulls_not_distinct=True,
        ),
    )

    organisation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organisations.id"), index=True, comment="RLS scope"
    )
    alert_type: Mapped[str] = mapped_column(LayerAAlertType)
    account_id: Mapped[str | None] = mapped_column(default=None)
    severity: Mapped[str] = mapped_column(LayerAAlertSeverity)
    state: Mapped[str] = mapped_column(LayerAAlertState, default="open")
    opened_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    condition_detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), default=None
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)


class LayerAAlertConfig(Base, UUIDPk, Timestamped):
    """One row per organisation, admin-UI-editable at runtime — deliberately
    not env-only config: the alert thresholds need to be changeable without
    a redeploy ("configurable duration" / "configurable threshold ...
    configurable short window"). `enabled=false` by default is also the
    feature's opt-in gate — the poller only sweeps organisations with a
    row here where enabled is true, so a fresh checkout/CI never needs a
    live Layer A.
    """

    __tablename__ = "layer_a_alert_config"
    __table_args__ = (
        UniqueConstraint("organisation_id", name="layer_a_alert_config_one_per_org"),
        CheckConstraint("sustained_disconnect_minutes > 0", name="layer_a_alert_config_positive_disconnect_minutes"),
        CheckConstraint("reconnect_attempt_threshold > 0", name="layer_a_alert_config_positive_attempt_threshold"),
        CheckConstraint("reconnect_attempt_window_minutes > 0", name="layer_a_alert_config_positive_window_minutes"),
    )

    organisation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organisations.id"), index=True, comment="RLS scope"
    )
    enabled: Mapped[bool] = mapped_column(default=False)
    sustained_disconnect_minutes: Mapped[int] = mapped_column(default=5)
    reconnect_attempt_threshold: Mapped[int] = mapped_column(default=5)
    reconnect_attempt_window_minutes: Mapped[int] = mapped_column(default=10)
    webhook_url: Mapped[str | None] = mapped_column(default=None)
    webhook_secret: Mapped[str | None] = mapped_column(
        default=None, comment="HMAC-SHA256 signing key, generated server-side the first time webhook_url is set"
    )
    webhook_enabled: Mapped[bool] = mapped_column(default=False)
    email_recipients: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    email_enabled: Mapped[bool] = mapped_column(default=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), default=None
    )


class LayerAAlertDelivery(Base, UUIDPk):
    """Durable delivery-attempt log across all three destinations — the
    "reviewable, not fired-and-forgotten" requirement extended to the
    destinations themselves. `channel='banner'` is logged too (it trivially
    "succeeds" — there's no delivery failure mode for a read endpoint), so
    every destination shares one consistent delivery-log shape.
    """

    __tablename__ = "layer_a_alert_deliveries"

    alert_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("layer_a_alerts.id"), index=True
    )
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organisations.id"), index=True,
        comment="RLS scope, denormalized from the parent alert for direct-column RLS",
    )
    channel: Mapped[str] = mapped_column(LayerADeliveryChannel)
    attempted_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    success: Mapped[bool] = mapped_column()
    detail: Mapped[str | None] = mapped_column(default=None, comment="error message on failure")
