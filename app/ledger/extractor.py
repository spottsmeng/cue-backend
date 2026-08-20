import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as dt_timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.capture.fixtures import FixtureCase, ProjectContext
from app.ledger.audit import record_audit_event
from app.ledger.context import (
    LedgerContextItem,
    relates_to_refs,
    render_ledger_context,
    resolve_ref,
)
from app.ledger.schema import (
    ExtractedCommitment,
    ExtractionResult,
    build_extraction_json_schema,
    load_extraction_prompt_template,
)
from app.ledger.supersession import propose_supersession_candidates
from app.llm.client import ModelClient
from app.llm.cost import record_llm_usage
from app.llm.factory import get_client
from app.models import Commitment, Evidence, OntologyTerm, Organisation, Party

# Fallback only, for a project whose `timezone` is unset or not a valid IANA
# name. Real resolution uses the project's own zone — see _project_tzinfo.
_SGT = dt_timezone(timedelta(hours=8))

# team_collaboration channels (Mattermost/Teams — internal Pico staff
# discussing a vendor, never the vendor themselves) have no confidently
# identified vendor to fall back to when the model can't name one — unlike
# external_vendor_chat, where the message's own sender always *is* the
# vendor. This is the one-row bucket a PM triages from Vendor Status
# (app/reports/composer.py's Party.type == "vendor_org" filter), not a
# silent attach to whoever typed the message.
_UNRESOLVED_VENDOR_NAME = "Unresolved Vendor"

# Below this self-reported confidence a commitment goes to human review
# instead of straight onto the ledger. The model's own 0-1 estimate is not a
# calibrated probability — especially on the local model — so this is used
# only in the safe direction: it can add a commitment to the review queue, it
# can never remove one from it. A documented starting point, not a number
# tuned against real traffic, same posture as every other threshold in this
# codebase's scheduled jobs (app/capture/reconciliation.py's own constants).
_LOW_CONFIDENCE = 0.7


class RejectedExtraction(Exception):
    """Raised when a model-returned commitment fails code-level verification.
    CLAUDE.md: 'evidence_span must be an exact substring of the source
    message, verified in code, not trusted.' Never persisted, never silently
    dropped either — the caller decides what to do with the rejection.

    Raised during the verification pass, which completes before *any* row is
    written (see extract_case) — so a rejection leaves no partial ledger
    state behind, whether or not the caller rolls back.
    """


@dataclass
class ExtractionOutcome:
    """What one message actually did to the ledger.

    `created` are new Commitment rows. `linked` are ids of commitments that
    already existed and which this message was found to be *about* — a status
    check, a restatement, a colleague relaying the consequence of a delay.
    Those add an Evidence row to the existing commitment and nothing else:
    the message is corroboration for something already on the ledger, not a
    new promise, and the whole point of the `relates_to` field is that the
    ledger stops gaining a fresh row every time someone discusses one.

    A dataclass rather than the bare `list[Commitment]` this used to return,
    because "no commitments created" and "no commitments created, but two
    existing ones gained evidence" are very different outcomes and a caller
    counting `len()` could not previously tell them apart.
    """

    created: list[Commitment] = field(default_factory=list)
    linked: list[uuid.UUID] = field(default_factory=list)
    # Ids of the Evidence rows *this* extraction wrote, for both the created
    # and the linked commitments. app/capture/pipeline.py backfills
    # Evidence.message_id (and voice-note media refs) onto exactly these — it
    # used to re-select by commitment_id instead, which is only equivalent
    # while a commitment can never gain evidence from a second message. The
    # `relates_to` path makes that false, and re-pointing an older message's
    # evidence at a newer message would corrupt the citation a PM clicks
    # through to.
    evidence_ids: list[uuid.UUID] = field(default_factory=list)


@dataclass
class _VerifiedItem:
    """One model-returned commitment that has passed every code-side check,
    carrying the resolved values the write pass needs so nothing is looked up
    or re-derived twice."""

    item: ExtractedCommitment
    span_start: int
    span_end: int
    act_term: OntologyTerm
    due_at: datetime | None
    vendor_name: str
    party_confident: bool
    relates_to_id: uuid.UUID | None
    # More than one of the tenant's vendors named inside this single item's
    # evidence span — the under-splitting signature, the mirror of
    # _overlapping_span_indexes. One commitment cannot be owed by two
    # companies, so either the model welded two promises into one row or the
    # span is too wide to attribute; both are for a human, not a guess.
    names_multiple_vendors: bool = False


def build_prompt(
    context: ProjectContext,
    case: FixtureCase,
    ledger_context: list[LedgerContextItem] | None = None,
) -> str:
    """Same construction as cue-eval/run_eval.py's build_prompt, against the
    same template file — a number measured in the eval harness stays
    comparable to what production actually sends."""
    template = load_extraction_prompt_template()
    milestones = "\n".join(
        "  - {}: {}".format(m["name"], m["due"]) for m in context["known_milestones"]
    )
    weekday = case.get("sent_weekday")
    return template.format(
        project=context["project"],
        client=context["client"],
        timezone=context["timezone"],
        venue=context["venue"],
        build_up=" to ".join(context["build_up"]),
        event_days=", ".join(context["event_days"]),
        doors=context["doors"],
        milestones=milestones,
        open_commitments=render_ledger_context(ledger_context or []),
        channel=case["channel"],
        party=case["party"],
        sent_at=case["sent_at"],
        weekday_hint=" ({})".format(weekday) if weekday else "",
        message=case["message"],
    )


def _project_tzinfo(context: ProjectContext) -> tzinfo:
    """The project's own zone, for reading naive timestamps.

    Was a fixed +08:00, which is correct for every fixture and for Pico, and
    silently eight hours wrong for any other tenant — in a product whose
    entire value is due dates. `Project.timezone` already existed and was
    already interpolated into the extraction prompt; only this function
    ignored it. Same `ZoneInfo(project.timezone)` pattern as
    app/reports/schedule.py and app/writeback/rate_limit.py.
    """
    try:
        return ZoneInfo(context["timezone"])
    except (KeyError, ZoneInfoNotFoundError, ValueError):
        return _SGT


def _parse_timestamp(value: str, tz: tzinfo) -> datetime:
    """due_at/sent_at from the model or fixtures are ISO 8601, sometimes
    date-only ("2026-06-18"). due_at is timestamptz, so a naive result needs a
    zone — `tz` is the project's own, from _project_tzinfo."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


async def _get_or_create_party(
    session: AsyncSession, organisation_id: uuid.UUID, display_name: str, party_type: str
) -> Party:
    stmt = select(Party).where(
        Party.organisation_id == organisation_id,
        Party.display_name == display_name,
        Party.type == party_type,
    )
    party = (await session.execute(stmt)).scalar_one_or_none()
    if party is None:
        party = Party(organisation_id=organisation_id, display_name=display_name, type=party_type)
        session.add(party)
        await session.flush()
    return party


def _match_known_vendor(vendors: list[dict], name: str) -> str | None:
    """Case-insensitive match of a model-extracted counterparty name against
    the project's already-known vendor_org parties (`context["vendors"]`) —
    returns the *canonical* stored name (not the model's raw string) so
    `_get_or_create_party` lands on the existing row rather than minting a
    near-duplicate that only differs in case."""
    needle = name.strip().lower()
    for vendor in vendors:
        canonical = vendor.get("party", "")
        if canonical.strip().lower() == needle:
            return canonical
    return None


def _normalise_for_match(text: str) -> str:
    """Casefolded, with every run of non-alphanumerics collapsed to one space,
    so "Ah Seng's" and "Ah Seng Production" line up on "ah seng" and
    punctuation cannot break a match."""
    return " ".join("".join(c if c.isalnum() else " " for c in text.casefold()).split())


# A one-token vendor match has to be at least this long to count. "Ah" would
# otherwise match the "ah" inside any word; "Vertex" and "Bloomworks" are
# distinctive on their own. Two-token matches skip the check — "ah seng" is
# specific enough at any length.
_MIN_SINGLE_TOKEN_VENDOR_MATCH = 4


def _vendors_named_in(text: str, vendor_names: list[str]) -> list[str]:
    """Canonical vendor names this text appears to name, longest match first.

    Matches the longest *leading* token-prefix of each vendor name, not any
    token anywhere. That distinction is load-bearing in this very corpus: "Ah
    Seng Production" and "Kim Seng Logistics" share the token "seng", so
    any-token matching would attribute Ah Seng's commitments to Kim Seng
    roughly at random. Prefix matching asks for "ah seng" or "kim seng" and
    gets neither wrong.

    Prefix rather than whole-name because the possessive is how people
    actually write it — cue-eval CD03's message says "Ah Seng's LED screen
    install", and the model faithfully copies that into deliverable_original
    while leaving counterparty_name empty. The name is right there; nothing
    was reading it.

    CJK names fall out of the same code path without special-casing: they
    normalise to a single token and match as a substring, which is what you
    want when there are no word boundaries to split on.
    """
    haystack = _normalise_for_match(text)
    found: list[tuple[int, str]] = []
    for canonical in vendor_names:
        tokens = _normalise_for_match(canonical).split()
        for size in range(len(tokens), 0, -1):
            prefix = " ".join(tokens[:size])
            if size == 1 and len(prefix) < _MIN_SINGLE_TOKEN_VENDOR_MATCH:
                continue
            if prefix in haystack:
                found.append((len(prefix), canonical))
                break
    return [canonical for _length, canonical in sorted(found, reverse=True)]


async def _load_org_vendor_names(session: AsyncSession, organisation_id: uuid.UUID) -> list[str]:
    """Every vendor this tenant knows, not just the ones already holding a
    commitment on this project.

    `context["vendors"]` is built by joining Party to Commitment
    (app/capture/extraction_bridge.py), so a vendor named in a message before
    they have their first commitment is invisible to it — which is exactly
    the case CD03 is made of, and exactly when getting attribution right
    matters most.
    """
    return list(
        (
            await session.execute(
                select(Party.display_name).where(
                    Party.organisation_id == organisation_id,
                    Party.type == "vendor_org",
                )
            )
        ).scalars().all()
    )


def _clean_counterparty_name(case: FixtureCase, counterparty_name: str | None) -> str | None:
    """cue-eval/prompt.txt asks the model, in words, to "never fill this with
    the sender's own name — it names who the commitment is ABOUT, not who is
    speaking". CLAUDE.md's first hard rule is that asking is not how this
    codebase gets guarantees, so this is that same rule enforced in code: a
    counterparty that is just the sender echoed back is discarded, and the
    commitment falls through to the unresolved-vendor + human-review path
    exactly as if the model had returned null.

    Without this, a model echoing the sender on a team_collaboration channel
    would mint a `vendor_org` Party named after a Pico staff member — a real
    person appearing in the vendor directory and in vendor reliability
    metrics, which is worse than having no vendor at all.
    """
    if counterparty_name is None:
        return None
    cleaned = counterparty_name.strip()
    if not cleaned:
        return None
    if cleaned.casefold() == (case.get("party") or "").strip().casefold():
        return None
    return cleaned


def _resolve_vendor_for_item(
    case: FixtureCase,
    context: ProjectContext,
    counterparty_name: str | None,
    *,
    span_text: str | None = None,
    org_vendor_names: list[str] | None = None,
) -> tuple[str, bool]:
    """Who the commitment's `party` (who owes it) should be, and whether
    that attribution is confident enough to skip pending_verification.

    external_vendor_chat (WhatsApp/WeChat): the sender genuinely is the
    vendor — `case["party"]` (already resolved from the message author,
    app/capture/identity.py) is used as-is. `counterparty_name` is
    deliberately *not* consulted here even now that the model populates it
    reliably: on a direct vendor chat, a vendor naming another vendor
    ("I'll coordinate the lift with Kim Seng") is describing a third party,
    not transferring the promise to them, so preferring the mentioned name
    over the sender would move commitments onto the wrong vendor's ledger.
    The reported over/under-splitting bug is an internal-channel one; this
    branch is left exactly as it was rather than changed speculatively.

    team_collaboration (Mattermost/Teams): the sender is internal Pico staff
    *discussing* a vendor, never the vendor — the author is never used as the
    vendor; instead the model's own `counterparty_name` for this commitment
    is matched against the project's known vendors, falling back to the
    model's literal string (unconfident) or, failing that, an explicit
    "unresolved" placeholder (also unconfident).
    """
    if case.get("channel_capability") != "team_collaboration":
        return case["party"], True

    # The model left counterparty_name empty, but the span it cited may name
    # a vendor anyway — CD03's "Ah Seng's LED screen install" is the shape.
    # Recovered in code rather than by prompt wording on purpose: two attempts
    # to teach the model this rule are recorded in CLAUDE.md, both reverted
    # for breaking IC01, and one by burying the amount in a deliverable field
    # — the exact failure the deliverable rule exists to prevent. A string
    # match against the tenant's own vendor list cannot regress a case it
    # never touches.
    if not counterparty_name and span_text:
        named = _vendors_named_in(span_text, org_vendor_names or [])
        if len(named) == 1:
            # Deliberately still unconfident: this is inference, and
            # FR-LED-07's posture is that the review queue is the safe
            # direction. A PM now sees the real vendor on a row they were
            # going to check anyway, instead of "Unresolved Vendor".
            return named[0], False

    if counterparty_name:
        matched = _match_known_vendor(context.get("vendors", []), counterparty_name)
        if matched is not None:
            return matched, True
        return counterparty_name, False

    return _UNRESOLVED_VENDOR_NAME, False


async def _get_or_create_internal_party(
    session: AsyncSession, organisation_id: uuid.UUID
) -> Party:
    """The internal-staff side of every extracted commitment — the party a
    vendor owes the deliverable *to*.

    Was the literal "Pico Project Team" for every tenant, which would have put
    a competitor's name on every row of customer #2's ledger. Resolved by
    `type` rather than by name so an organisation that already has an
    internal_staff party keeps it (no orphaned rows, no migration); only a
    tenant that has none gets one minted from their own organisation name.
    """
    existing = (
        await session.execute(
            select(Party).where(
                Party.organisation_id == organisation_id,
                Party.type == "internal_staff",
            ).order_by(Party.created_at.asc()).limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    org_name = (
        await session.execute(
            select(Organisation.name).where(Organisation.id == organisation_id)
        )
    ).scalar_one_or_none()
    return await _get_or_create_party(
        session, organisation_id, f"{org_name} Project Team" if org_name else "Project Team",
        "internal_staff",
    )


async def _get_commitment_act_term(session: AsyncSession, code: str) -> OntologyTerm:
    stmt = select(OntologyTerm).where(
        OntologyTerm.category == "commitment_act",
        OntologyTerm.code == code,
        OntologyTerm.vertical_id.is_(None),
        OntologyTerm.organisation_id.is_(None),
    )
    term = (await session.execute(stmt)).scalar_one_or_none()
    if term is None:
        raise LookupError(f"commitment_act ontology term not seeded: {code!r}")
    return term


# The review queue's triage vocabulary. Stable machine keys, not sentences —
# app/reports/composer.py and the frontend map them to display text, the same
# separation ontology_terms.code draws everywhere else in this codebase.
REVIEW_MONETARY = "monetary_field"
REVIEW_DATE = "date_field"
REVIEW_VENDOR_UNCONFIRMED = "vendor_unconfirmed"
REVIEW_OVER_SPLIT = "overlapping_spans"
REVIEW_MERGED_VENDORS = "multiple_vendors_in_span"
REVIEW_LOW_CONFIDENCE = "low_model_confidence"
# Reached through the `relates_to` path rather than at creation.
REVIEW_UNAPPLIED_CLAIM = "unapplied_claim"
REVIEW_LOW_CONFIDENCE_LINK = "low_confidence_link"


def _verification_reasons(v: "_VerifiedItem", *, over_split: bool) -> list[str]:
    """Every reason this commitment needs a human, not just the first.

    Returned as a list rather than collapsed to a boolean because
    `verification_state` already answers "does someone need to look" and
    answers nothing else. A PM opening a queue of thirty rows cannot tell a
    price waiting for confirmation from a possible hallucination, and those
    want opposite amounts of attention. Order is fixed so two identical
    extractions produce identical rows.
    """
    reasons: list[str] = []
    # FR-LED-07: money and dates route to review regardless of confidence.
    if v.item.amount is not None:
        reasons.append(REVIEW_MONETARY)
    if v.due_at is not None:
        reasons.append(REVIEW_DATE)
    if not v.party_confident:
        reasons.append(REVIEW_VENDOR_UNCONFIRMED)
    if over_split:
        reasons.append(REVIEW_OVER_SPLIT)
    if v.names_multiple_vendors:
        reasons.append(REVIEW_MERGED_VENDORS)
    if v.item.confidence < _LOW_CONFIDENCE:
        reasons.append(REVIEW_LOW_CONFIDENCE)
    return reasons


def _overlapping_span_indexes(verified: list[_VerifiedItem]) -> set[int]:
    """Indexes of items whose evidence span overlaps another item's.

    Two commitments extracted from the *same* stretch of text are the
    signature of one promise being split in two — the over-splitting half of
    the reported bug. This does not reject them (a message can legitimately
    interleave two promises in one clause, and rejecting the message would
    lose the real commitment along with the invented one); it routes both to
    human review, which is the answer to "guessing wrong in either direction
    is worse than asking".
    """
    flagged: set[int] = set()
    for i in range(len(verified)):
        for j in range(i + 1, len(verified)):
            a, b = verified[i], verified[j]
            if a.span_start < b.span_end and b.span_start < a.span_end:
                flagged.add(i)
                flagged.add(j)
    return flagged


async def extract_case(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    organisation_id: uuid.UUID,
    context: ProjectContext,
    case: FixtureCase,
    client: ModelClient | None = None,
    ledger_context: list[LedgerContextItem] | None = None,
) -> ExtractionOutcome:
    """Prompt -> model -> schema-validate -> code-verify -> write.

    **Verify everything, then write.** The verification pass below completes
    for every returned item before the write pass adds a single row. It used
    to be one loop that wrote as it went and raised on the first bad
    evidence_span — which meant a three-commitment message whose third item
    failed left the first two persisted (the caller cannot roll back without
    also losing the captured Message itself, NFR-AVL-02), and marked the
    message extracted so it was never retried. That is under-splitting caused
    by code rather than by the model, and it is why this is two passes.

    `ledger_context` is the already-logged commitments the model was shown
    (app/ledger/context.py). An item that points at one of them via
    `relates_to` does not become a new Commitment: it attaches an Evidence
    row to the commitment it names. That is the fix for a message being read
    as a new promise when it is a colleague restating, chasing or reacting to
    a promise already on the ledger — information no prompt wording can
    supply, because it is not in the message.

    Does not commit — caller controls the transaction boundary (see
    scripts/extract_fixtures.py), since the evidence-existence trigger is
    deferred to COMMIT and a batch of cases may want to commit per-case or
    all together.

    `client` defaults to the configured extraction-role client (Ollama or
    Anthropic, per .env) but can be overridden — tests pass a fake client so
    they're fast and don't require a live Ollama, without needing a mocking
    framework or any change to production callers.
    """
    ledger_context = ledger_context or []
    prompt = build_prompt(context, case, ledger_context)
    schema = build_extraction_json_schema(relates_to_refs(ledger_context))
    client = client or get_client("extraction")

    raw, usage = await client.complete(prompt, schema)
    await record_llm_usage(
        session, organisation_id=organisation_id, project_id=project_id,
        role="extraction", purpose="ledger_extraction", usage=usage,
    )
    try:
        result = ExtractionResult.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as e:
        raise RejectedExtraction(f"model output failed schema validation: {e}") from e

    # --- pass 1: verify. Nothing below this point writes anything. ---
    tz = _project_tzinfo(context)
    org_vendor_names = await _load_org_vendor_names(session, organisation_id)
    verified: list[_VerifiedItem] = []
    for item in result.commitments:
        if not item.evidence_span or item.evidence_span not in case["message"]:
            raise RejectedExtraction(
                f"evidence_span not found verbatim in source message: {item.evidence_span!r}"
            )

        # The JSON Schema enum (app/ledger/schema.py) should already make an
        # unknown ref undecodable, but the ref is re-checked here for the same
        # reason evidence_span is: a constraint the model was given is not a
        # constraint the database can rely on until code has re-verified it.
        relates_to_id = resolve_ref(ledger_context, item.relates_to)
        if item.relates_to and relates_to_id is None:
            raise RejectedExtraction(
                f"relates_to names a commitment that was not offered to the model: {item.relates_to!r}"
            )

        counterparty_name = _clean_counterparty_name(case, item.counterparty_name)
        vendor_name, party_confident = _resolve_vendor_for_item(
            case, context, counterparty_name,
            span_text=item.evidence_span, org_vendor_names=org_vendor_names,
        )
        span_start = case["message"].index(item.evidence_span)
        verified.append(
            _VerifiedItem(
                item=item,
                span_start=span_start,
                span_end=span_start + len(item.evidence_span),
                act_term=await _get_commitment_act_term(session, item.act_type),
                due_at=_parse_timestamp(item.due_at, tz) if item.due_at else None,
                vendor_name=vendor_name,
                party_confident=party_confident,
                relates_to_id=relates_to_id,
                # Only where attribution comes from the message body. On a
                # direct vendor chat the sender *is* the vendor, so a span
                # mentioning two other companies is a vendor describing
                # subcontractors, not two promises welded together — flagging
                # it there would be a false positive on the commonest shape of
                # message in the corpus.
                names_multiple_vendors=(
                    case.get("channel_capability") == "team_collaboration"
                    and len(_vendors_named_in(item.evidence_span, org_vendor_names)) > 1
                ),
            )
        )

    overlapping = _overlapping_span_indexes(verified)

    # --- pass 2: write. Every check has passed for every item. ---
    outcome = ExtractionOutcome()
    if not verified:
        return outcome

    internal = await _get_or_create_internal_party(session, organisation_id)

    for index, v in enumerate(verified):
        if v.relates_to_id is not None:
            evidence_id = await _attach_evidence_to_existing(
                session, project_id=project_id, commitment_id=v.relates_to_id,
                case=case, verified=v, tz=tz,
            )
            outcome.linked.append(v.relates_to_id)
            outcome.evidence_ids.append(evidence_id)
            continue

        vendor = await _get_or_create_party(session, organisation_id, v.vendor_name, "vendor_org")

        # FR-LED-07: price, approval, date and scope-change fields route to
        # pending_verification regardless of confidence. Three further routes
        # into the same queue, rather than a new state — one queue a PM
        # already checks, not four: a vendor that couldn't be confidently
        # identified from an internal-channel message; a commitment quoting
        # the same text as another one (the over-split signature); and a
        # commitment the model itself was not confident about.
        reasons = _verification_reasons(v, over_split=index in overlapping)

        commitment = Commitment(
            project_id=project_id,
            party_id=vendor.id,
            counterparty_id=internal.id,
            act_type_id=v.act_term.id,
            state="proposed",
            deliverable_en=v.item.deliverable_en,
            deliverable_original=v.item.deliverable_original,
            due_at=v.due_at,
            amount=v.item.amount,
            currency=v.item.currency,
            confidence=v.item.confidence,
            field_confidence={},
            verification_state="pending_verification" if reasons else "auto",
            verification_reasons=reasons,
        )
        session.add(commitment)
        await session.flush()  # need commitment.id for the evidence row below

        evidence = _build_evidence(case, v, commitment_id=commitment.id, tz=tz)
        session.add(evidence)
        await session.flush()  # need evidence.id for the audit row below
        outcome.evidence_ids.append(evidence.id)

        # FR-LED-12: every ledger mutation, not only the REST-driven ones —
        # actor_id=None since this write has no human behind it.
        await record_audit_event(
            session,
            project_id=project_id,
            commitment_id=commitment.id,
            action="created",
            actor_id=None,
            to_state=commitment.state,
            evidence_id=evidence.id,
        )

        # FR-LED-05: propose (never apply) a supersession candidate against
        # any prior commitment for this same vendor+deliverable — see
        # app/ledger/supersession.py's own module docstring for why this is
        # AI-proposed/human-confirmed, not automatic. `item.price_changed`
        # is cue-eval/schema.json's own tuned extraction-time signal
        # (ExtractedCommitment's own field) — threading it through here as a
        # hint doesn't touch the tuned extraction prompt or schema at all,
        # just uses output that was already being produced and discarded.
        await propose_supersession_candidates(
            session, commitment,
            hint="price_changed" if v.item.price_changed else None,
            organisation_id=organisation_id,
        )
        outcome.created.append(commitment)

    return outcome


def _build_evidence(
    case: FixtureCase, v: _VerifiedItem, *, commitment_id: uuid.UUID, tz: tzinfo
) -> Evidence:
    return Evidence(
        commitment_id=commitment_id,
        channel=case["channel"],
        sent_at=_parse_timestamp(case["sent_at"], tz),
        language=case["lang"],
        original_text=case["message"],
        span_start=v.span_start,
        span_end=v.span_end,
    )


# Act types that assert a change to an existing commitment rather than
# corroborating it. Paired with amount/due_at below: what makes a linked
# message a claim rather than a chase.
_REVISION_ACTS = ("renegotiate", "revoke")


def _unapplied_claims(verified: _VerifiedItem) -> dict:
    """What a linked message asserts that the existing commitment does not
    already say. Deliberately *not* applied — see
    _attach_evidence_to_existing — only recorded, so a human can act on it."""
    claims: dict[str, object] = {}
    if verified.item.amount is not None:
        claims["amount"] = verified.item.amount
        if verified.item.currency:
            claims["currency"] = verified.item.currency
    if verified.due_at is not None:
        claims["due_at"] = verified.due_at.isoformat()
    if verified.item.act_type in _REVISION_ACTS:
        claims["act_type"] = verified.item.act_type
    if verified.item.price_changed:
        claims["price_changed"] = True
    return claims


async def _attach_evidence_to_existing(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    commitment_id: uuid.UUID,
    case: FixtureCase,
    verified: _VerifiedItem,
    tz: tzinfo,
) -> uuid.UUID:
    """The `relates_to` path: this message is *about* a commitment that is
    already on the ledger, so the ledger gains a citation, not a row.

    Deliberately additive-only — no field on the existing commitment is
    touched. A message saying "the 4pm start still holds" is corroboration;
    letting extraction quietly rewrite an existing commitment's due date or
    amount from a passing mention in someone else's sentence is exactly the
    unreviewed automatic edit app/ledger/supersession.py's own docstring
    refuses to make. A genuine revision arrives as a new commitment with
    relates_to null and is picked up by the supersession path, which a human
    confirms.

    Additive-only is *not* the same as silent, though, and it used to be. A
    message saying "the LED install is now $6,200" links correctly, and the
    $6,200 was then dropped on the floor — no row, no field, no flag, no
    queue entry, one `logger.info` in app/capture/pipeline.py. That is worse
    than the invented-commitment bug this whole path exists to fix: a fake
    row is visible and can be withdrawn, an unapplied price change is not.
    So the claim is never applied, but it *is* recorded in the audit detail
    and the commitment is routed to human review — the same queue and the
    same FR-LED-07 reasoning that sends any money or date field there on the
    create path. Low model confidence on the link itself routes the same way:
    the create path's confidence gate had no counterpart here, so an unsure
    link was accepted in silence.
    """
    evidence = _build_evidence(case, verified, commitment_id=commitment_id, tz=tz)
    session.add(evidence)
    await session.flush()

    claims = _unapplied_claims(verified)
    low_confidence = verified.item.confidence < _LOW_CONFIDENCE
    detail: dict[str, object] = {
        "source": "extraction",
        "reason": "message referred to this already-logged commitment",
        "act_type": verified.item.act_type,
        "confidence": verified.item.confidence,
    }

    if claims or low_confidence:
        commitment = (
            await session.execute(select(Commitment).where(Commitment.id == commitment_id))
        ).scalar_one()
        reason = REVIEW_UNAPPLIED_CLAIM if claims else REVIEW_LOW_CONFIDENCE_LINK
        detail["unapplied_claims"] = claims
        detail["flagged_reason"] = reason
        # Kept so a human_verified commitment that a later message contradicts
        # shows *what* it had been verified as, rather than the verification
        # silently disappearing.
        detail["prior_verification_state"] = commitment.verification_state
        commitment.verification_state = "pending_verification"
        if reason not in commitment.verification_reasons:
            # Appends rather than replaces: the row may already be in the
            # queue for its own reasons, and a later message asserting a
            # change does not retire those.
            commitment.verification_reasons = [*commitment.verification_reasons, reason]
        await session.flush()

    await record_audit_event(
        session,
        project_id=project_id,
        commitment_id=commitment_id,
        action="evidence_added",
        actor_id=None,
        evidence_id=evidence.id,
        detail=detail,
    )
    return evidence.id
