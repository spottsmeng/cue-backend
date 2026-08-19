import json
from datetime import datetime, timedelta, timezone as dt_timezone
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.capture.fixtures import FixtureCase, ProjectContext
from app.ledger.audit import record_audit_event
from app.ledger.schema import (
    ExtractionResult,
    load_extraction_json_schema,
    load_extraction_prompt_template,
)
from app.ledger.supersession import propose_supersession_candidates
from app.llm.client import ModelClient
from app.llm.cost import record_llm_usage
from app.llm.factory import get_client
from app.models import Commitment, Evidence, OntologyTerm, Party

_SGT = dt_timezone(timedelta(hours=8))

# team_collaboration channels (Mattermost/Teams — internal Pico staff
# discussing a vendor, never the vendor themselves) have no confidently
# identified vendor to fall back to when the model can't name one — unlike
# external_vendor_chat, where the message's own sender always *is* the
# vendor. This is the one-row bucket a PM triages from Vendor Status
# (app/reports/composer.py's Party.type == "vendor_org" filter), not a
# silent attach to whoever typed the message.
_UNRESOLVED_VENDOR_NAME = "Unresolved Vendor"


class RejectedExtraction(Exception):
    """Raised when a model-returned commitment fails code-level verification.
    CLAUDE.md: 'evidence_span must be an exact substring of the source
    message, verified in code, not trusted.' Never persisted, never silently
    dropped either — the caller decides what to do with the rejection."""


def build_prompt(context: ProjectContext, case: FixtureCase) -> str:
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
        channel=case["channel"],
        party=case["party"],
        sent_at=case["sent_at"],
        weekday_hint=" ({})".format(weekday) if weekday else "",
        message=case["message"],
    )


def _parse_timestamp(value: str) -> datetime:
    """due_at/sent_at from the model or fixtures are ISO 8601, sometimes
    date-only ("2026-06-18"). due_at is timestamptz, so a naive result needs a
    zone — fixtures are all Asia/Singapore; production should use the
    project's own `timezone` field instead of this fixed +08:00."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_SGT)
    return dt


async def _get_or_create_party(
    session: AsyncSession, organisation_id: UUID, display_name: str, party_type: str
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


def _resolve_vendor_for_item(
    case: FixtureCase, context: ProjectContext, counterparty_name: str | None
) -> tuple[str, bool]:
    """Who the commitment's `party` (who owes it) should be, and whether
    that attribution is confident enough to skip pending_verification.

    external_vendor_chat (WhatsApp/WeChat): the sender genuinely is the
    vendor — `case["party"]` (already resolved from the message author,
    app/capture/identity.py) is used as-is, same behaviour as before this
    task. team_collaboration (Mattermost/Teams): the sender is internal
    Pico staff *discussing* a vendor, never the vendor — the author is never
    used as the vendor; instead the model's own `counterparty_name` for this
    commitment is matched against the project's known vendors, falling back
    to the model's literal string (unconfident) or, failing that, an
    explicit "unresolved" placeholder (also unconfident).
    """
    if case.get("channel_capability") != "team_collaboration":
        return case["party"], True

    if counterparty_name:
        matched = _match_known_vendor(context.get("vendors", []), counterparty_name)
        if matched is not None:
            return matched, True
        return counterparty_name, False

    return _UNRESOLVED_VENDOR_NAME, False


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


async def extract_case(
    session: AsyncSession,
    *,
    project_id: UUID,
    organisation_id: UUID,
    context: ProjectContext,
    case: FixtureCase,
    client: ModelClient | None = None,
) -> list[Commitment]:
    """Prompt -> model -> schema-validate -> code-verify evidence -> write
    Commitment + Evidence. Does not commit — caller controls the transaction
    boundary (see scripts/extract_fixtures.py), since the evidence-existence
    trigger is deferred to COMMIT and a batch of cases may want to commit
    per-case or all together.

    `client` defaults to the configured extraction-role client (Ollama or
    Anthropic, per .env) but can be overridden — tests pass a fake client so
    they're fast and don't require a live Ollama, without needing a mocking
    framework or any change to production callers.
    """
    prompt = build_prompt(context, case)
    schema = load_extraction_json_schema()
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

    internal = await _get_or_create_party(
        session, organisation_id, "Pico Project Team", "internal_staff"
    )

    created: list[Commitment] = []
    for item in result.commitments:
        if item.evidence_span not in case["message"]:
            raise RejectedExtraction(
                f"evidence_span not found verbatim in source message: {item.evidence_span!r}"
            )

        vendor_name, party_confident = _resolve_vendor_for_item(case, context, item.counterparty_name)
        vendor = await _get_or_create_party(session, organisation_id, vendor_name, "vendor_org")

        act_term = await _get_commitment_act_term(session, item.act_type)
        due_at = _parse_timestamp(item.due_at) if item.due_at else None

        # FR-LED-07: price, approval, date and scope-change fields route to
        # pending_verification regardless of confidence. A commitment whose
        # vendor couldn't be confidently identified from an internal-channel
        # message reuses the same state (task: "route low-confidence
        # attribution to human review, not a silent guess") rather than a
        # third value — one queue a PM already checks, not two.
        verification_state = (
            "pending_verification"
            if (item.amount is not None or due_at is not None or not party_confident)
            else "auto"
        )

        commitment = Commitment(
            project_id=project_id,
            party_id=vendor.id,
            counterparty_id=internal.id,
            act_type_id=act_term.id,
            state="proposed",
            deliverable_en=item.deliverable_en,
            deliverable_original=item.deliverable_original,
            due_at=due_at,
            amount=item.amount,
            currency=item.currency,
            confidence=item.confidence,
            field_confidence={},
            verification_state=verification_state,
        )
        session.add(commitment)
        await session.flush()  # need commitment.id for the evidence row below

        span_start = case["message"].index(item.evidence_span)
        evidence = Evidence(
            commitment_id=commitment.id,
            channel=case["channel"],
            sent_at=_parse_timestamp(case["sent_at"]),
            language=case["lang"],
            original_text=case["message"],
            span_start=span_start,
            span_end=span_start + len(item.evidence_span),
        )
        session.add(evidence)
        await session.flush()  # need evidence.id for the audit row below

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
        # (ExtractedCommitment's own field) — it existed before this
        # session but nothing downstream ever consumed it; threading it
        # through here as a hint doesn't touch the tuned extraction prompt
        # or schema at all, just uses output that was already being
        # produced and discarded.
        await propose_supersession_candidates(
            session, commitment,
            hint="price_changed" if item.price_changed else None,
            organisation_id=organisation_id,
        )
        created.append(commitment)

    return created
