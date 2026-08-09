import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, Timestamped, TZDateTime, UUIDPk

# Capture-domain models (PRD §6.1 FR-CAP, §6.2 FR-NRM — Prompt 11 item 1) live
# here, not in app/models/ledger.py — same per-domain-module-owns-its-models
# precedent app/twin/models.py, app/documents/models.py and
# app/foresight/models.py already establish, and the same one
# app/capture/adapters/ and app/capture/schema.py (item 2, already built) are
# themselves part of.
#
# Message is the canonical envelope (FR-NRM-01) a queue consumer
# (app/capture/pipeline.py, item 3) builds from a ChannelAdapter's
# RawCapturedMessage (app/capture/schema.py) — deliberately not the same
# class. RawCapturedMessage is the adapter-to-queue boundary shape and never
# touches the database; Message is the durable, RLS-scoped, queryable row
# Evidence.message_id finally gets a real FK to (this migration is what
# resolves the "Points at the future Message/capture table — not modelled
# yet" comment that has sat on that column since the very first migration).


class Message(Base, UUIDPk, Timestamped):
    """PRD FR-NRM-01: source, channel, author identity, timestamp, language,
    media refs, payload hash — one row per captured message, regardless of
    which channel it arrived through.

    `sender_external_id` is always populated (whatever raw identity string
    the channel handed over — phone / WeChat ID / UPN / email);
    `author_party_id` / `identity_confidence` start NULL and are filled in by
    the identity resolver (item 4, app/capture/identity.py) as a queue
    consumer, not by the normaliser that inserts this row — NFR-AVL-02 ("capture
    must never lose a message") means a message whose sender can't yet be
    resolved to a Party is still durably captured, not dropped or blocked on
    resolution succeeding first.

    `payload_hash` is FR-CAP-11's dedup key (app/capture/schema.py's
    compute_payload_hash, already computed by every adapter) — unique per
    project, not globally, since the same underlying message legitimately
    recomputes to the same hash if it arrives via two paths *within one
    project* (the case FR-CAP-11 names: "a Teams message also present in a
    mailbox"), but two different projects/tenants have no such relationship
    to deduplicate across.
    """

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("project_id", "payload_hash", name="messages_project_payload_hash_key"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True, comment="RLS scope"
    )
    channel_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("channels.id"), index=True
    )
    external_id: Mapped[str] = mapped_column(comment="the channel's own message id")
    sender_external_id: Mapped[str] = mapped_column(
        comment="raw identity string as the channel reports it — phone / WeChat ID / UPN / email"
    )
    author_party_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parties.id"), default=None, index=True
    )
    identity_confidence: Mapped[float | None] = mapped_column(
        default=None, comment="FR-NRM-03: confidence of author_party_id's resolution, if resolved"
    )
    identity_manually_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    sent_at: Mapped[datetime] = mapped_column(TZDateTime)
    language: Mapped[str | None] = mapped_column(
        default=None, comment="FR-NRM-02: bcp47, or a code-switched pair e.g. 'zh+en'; set by the normaliser"
    )
    text: Mapped[str | None] = mapped_column(default=None)
    payload_hash: Mapped[str] = mapped_column(comment="FR-CAP-11 dedup key, see class docstring")
    extraction_attempted_at: Mapped[datetime | None] = mapped_column(
        TZDateTime, default=None,
        comment="set once the ledger extractor has been invoked for this message, success or reject",
    )


class MessageMedia(Base, UUIDPk, Timestamped):
    """FR-CAP-12: original media preserved at full fidelity; derived
    artefacts (thumbnails, OCR text) are separate objects/rows, never
    overwrites of the original — this row *is* that separation. One row per
    RawCapturedMedia (app/capture/schema.py) the adapter reported;
    `storage_key` is where item 6's media pipeline puts the original bytes
    via app/documents/storage.py's StorageBackend (reused, not a second
    abstraction). `thumbnail_key` / `ocr_text` / `exif` all start NULL and
    are filled in by that same pipeline, independently of and after the
    original upload — a partially-processed row (original stored, derivation
    not yet run or not applicable to this kind) is a normal, valid state,
    not an error.
    """

    __tablename__ = "message_media"

    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id"), index=True
    )
    kind: Mapped[str] = mapped_column(comment='"image" | "document" | "voice_note" | ...')
    original_filename: Mapped[str | None] = mapped_column(default=None)
    source_uri: Mapped[str | None] = mapped_column(
        default=None,
        comment="RawCapturedMedia.uri — the adapter's own fetch reference (e.g. a Graph driveItem "
        "id, a Mattermost file id); what item 6's media pipeline fetches storage_key's bytes from",
    )
    storage_key: Mapped[str | None] = mapped_column(
        default=None, comment="StorageBackend object key for the original bytes; NULL until fetched"
    )
    thumbnail_key: Mapped[str | None] = mapped_column(
        default=None, comment="FR-CAP-12 derived artefact — StorageBackend object key, image kind only"
    )
    ocr_text: Mapped[str | None] = mapped_column(
        default=None, comment="FR-NRM-05 — image/PDF/Office kinds only"
    )
    exif: Mapped[dict | None] = mapped_column(
        JSONB, default=None, comment="FR-NRM-06 — onsite photograph timestamp/geolocation, image kind only"
    )
    transcript: Mapped[str | None] = mapped_column(
        default=None, comment="FR-VOI-01 — voice_note kind only, written by the ASR pipeline (item 7)"
    )
    transcript_confidence: Mapped[float | None] = mapped_column(
        default=None, comment="FR-VOI-04 — per-utterance detail lives in `transcript_detail`, this is the mean"
    )
    transcript_detail: Mapped[dict | None] = mapped_column(
        JSONB, default=None, comment="FR-VOI-04: list of {text, start, end, confidence} per utterance"
    )
    transcript_language: Mapped[str | None] = mapped_column(
        default=None, comment="FR-VOI-01/02: which ASR client handled it — 'en' or 'zh'"
    )


class ChannelHealthEvent(Base, UUIDPk):
    """FR-CAP-09: capture-health signal history, per channel per project —
    the durable record behind Channel.healthy (app/models/project.py), which
    only ever holds the *current* state. Written by item 9's arq-scheduled
    check (app/capture/health.py) each time it calls a channel's own
    ChannelAdapter.health() and POSTs the result to the existing
    /channels/{id}/health endpoint (app/api/channels.py) — this table is
    what makes that endpoint's history queryable rather than fire-and-forget,
    and what the 15-minute degradation SLA (FR-CAP-09) is checked against.
    """

    __tablename__ = "channel_health_events"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True, comment="RLS scope"
    )
    channel_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("channels.id"), index=True
    )
    healthy: Mapped[bool] = mapped_column(Boolean)
    detail: Mapped[dict | None] = mapped_column(JSONB, default=None)
    checked_at: Mapped[datetime] = mapped_column(TZDateTime)


class PartyOrganisationMapping(Base, UUIDPk, Timestamped):
    """FR-NRM-04: "Maintain party-to-organisation mapping (person → vendor
    company) with effective-dating." A `person`-type Party (app/models/party.py)
    can move between vendor companies over a long-running engagement (a
    contact leaves one supplier for another, mid-project) — this table is the
    history of who that person represented and when, not a single mutable
    field on Party itself (which would silently lose the history FR-NRM-04
    explicitly asks to maintain).

    `effective_to IS NULL` means "still current" — the same open-ended-range
    convention this codebase doesn't otherwise use yet, chosen because it is
    the standard SQL idiom for effective-dated data and there's no existing
    local precedent to follow or diverge from instead. Overlapping ranges for
    the same `person_party_id` are a data-integrity concern the service layer
    (app/parties/organisation_mapping.py) enforces at write time — closing
    the previous mapping's `effective_to` before opening a new one — rather
    than a DB-level exclusion constraint, matching the level of enforcement
    RetentionPolicy's own axis-resolution already uses for a similar "most
    specific / most current wins" shape.
    """

    __tablename__ = "party_organisation_mappings"

    # Party itself has no RLS policy of its own — a pre-existing gap
    # documented at every query site that touches it bare (e.g.
    # app/api/consent.py's consent_action_request). This table is new, not
    # bound by that precedent, and Party is organisation-scoped (not
    # project-scoped) — so it gets a direct organisation_id + policy, the
    # same denormalized-scope-column shape channel_types.organisation_id
    # already uses, rather than either leaving this table ungoverned too or
    # inventing a join-through-parties policy that Party's own missing
    # policy would make meaningless anyway.
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organisations.id"), index=True, comment="RLS scope"
    )
    person_party_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parties.id"), index=True, comment="a Party of type='person'"
    )
    organisation_party_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parties.id"), index=True, comment="a Party of type='vendor_org'"
    )
    role_title: Mapped[str | None] = mapped_column(default=None)
    effective_from: Mapped[datetime] = mapped_column(TZDateTime)
    effective_to: Mapped[datetime | None] = mapped_column(
        TZDateTime, default=None, comment="NULL = still current"
    )


class ChannelExtractionSchedule(Base, UUIDPk, Timestamped):
    """FR-CAP-10 (Should): "Support scheduled extraction windows in
    addition to event-driven capture, configurable per channel." Mirrors
    app/reports/models.py's ReportScheduleConfig shape closely (same
    "small, config-only, ride the existing arq worker" posture) —
    interval-minute granularity rather than that table's day/hour-of-week
    fields, since a capture window is a recurring cadence ("check this
    mailbox every 30 minutes"), not a weekly meeting-prep deadline.
    Deliberately minimal, per Prompt 11's own "small addition... don't
    over-invest relative to the Must-priority items above" instruction —
    no admin CRUD API in this pass (schema + the arq job that reads it,
    app/capture/schedule.py, same "mechanism exists, not a v1 feature
    commitment" posture MilestoneArchetype.organisation_id already has
    elsewhere in this codebase).
    """

    __tablename__ = "channel_extraction_schedules"
    __table_args__ = (
        UniqueConstraint("channel_id", name="channel_extraction_schedules_one_per_channel"),
        CheckConstraint("interval_minutes > 0", name="channel_extraction_schedules_positive_interval"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True, comment="RLS scope"
    )
    channel_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("channels.id"), index=True
    )
    interval_minutes: Mapped[int] = mapped_column()
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
