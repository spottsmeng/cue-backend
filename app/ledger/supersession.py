"""FR-LED-05 (PRD §4.3, referenced by app/parties/compute.py's own module
docstring): "detect and link supersession — a later commitment replacing an
earlier one." `Commitment.supersedes` has never been populated by any path
in this build until this module — confirmed by grep before writing it, the
same "verified in code, not assumed" discipline every other gap-closing
session in this codebase already applies.

Deliberately AI-*proposes*, human-*confirms* — not fully automatic. Three
precedents already establish this exact shape elsewhere in CUE (Deviation's
auto_drafted -> confirmed, OutboundMessage's draft -> authorised -> sent,
Commitment's own pending_verification -> human_verified), for the same
reason CLAUDE.md states once and this module extends: "No commitment
without evidence... verified in code, not trusted." An unreviewed automatic
edit to a *different* commitment's own supersedes array — a financial/scope
relationship — is exactly the kind of thing that discipline exists to
prevent. `propose_supersession_candidates` below only ever writes a
CommitmentSupersessionCandidate row (never mutates Commitment.supersedes
directly); `confirm_supersession_candidate` is the only path that does.
"""

import json
import uuid
from datetime import datetime, timezone as dt_timezone

from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.client import ModelClient
from app.llm.cost import record_llm_usage
from app.llm.factory import get_client
from app.models import Commitment, CommitmentSupersessionCandidate
from app.parties.service import recompute_vendor_metrics

# Bounds the number of LLM calls one new commitment can ever trigger — a
# vendor with a long history of same-named commitments (e.g. a recurring
# "Staffing — day crew" line item) shouldn't fan out into dozens of
# comparison calls; the most recent few are what a real revision would be
# against anyway.
_CANDIDATE_LIMIT = 3

# A judgment/generation task over already-extracted structured commitment
# fields, not extraction itself — get_client("reasoning"), the same role
# distinction app/writeback/compose.py's own comment draws for a comparable
# "not extraction" call.
_PROMPT_TEMPLATE = """You are reviewing two commitments recorded for the same vendor on the same project, judging whether the newer one is a revision of the older one — a later message updating price, scope or terms for the same underlying deliverable — rather than a genuinely separate, unrelated commitment that happens to share a name.

Older commitment (recorded first): "{old_deliverable}", amount {old_amount}, due {old_due}
Newer commitment (recorded later): "{new_deliverable}", amount {new_amount}, due {new_due}
{hint}
Does the newer commitment revise/replace the older one? Judge only from what is stated above — do not assume a revision purely because the deliverable names match if nothing else here is consistent with that."""

_SUPERSESSION_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "supersedes": {
            "type": "boolean",
            "description": "true only if the newer commitment genuinely revises the older one",
        },
        "reasoning": {
            "type": "string",
            "description": "one sentence, naming the specific fields that support the judgment",
        },
    },
    "required": ["supersedes", "reasoning"],
}


class _SupersessionJudgment(BaseModel):
    supersedes: bool
    reasoning: str


def _format_amount(commitment: Commitment) -> str:
    if commitment.amount is None:
        return "not specified"
    return f"{commitment.amount} {commitment.currency or ''}".strip()


def _format_due(commitment: Commitment) -> str:
    return commitment.due_at.isoformat() if commitment.due_at else "not specified"


async def find_candidate_priors(session: AsyncSession, commitment: Commitment) -> list[Commitment]:
    """Plain SQL, no model call — same vendor, same deliverable name
    (Commitment.deliverable_en is a canonical noun phrase per CLAUDE.md's
    extraction rules, so a case-insensitive exact match is a real signal,
    not a coincidence), recorded strictly before this one. Cheap and
    narrow: the common case (a genuinely new, unrelated commitment) returns
    nothing and never reaches the LLM call below at all."""
    stmt = (
        select(Commitment)
        .where(
            Commitment.party_id == commitment.party_id,
            Commitment.id != commitment.id,
            func.lower(Commitment.deliverable_en) == commitment.deliverable_en.lower(),
            Commitment.created_at < commitment.created_at,
        )
        .order_by(Commitment.created_at.desc())
        .limit(_CANDIDATE_LIMIT)
    )
    return list((await session.execute(stmt)).scalars().all())


async def propose_supersession_candidates(
    session: AsyncSession,
    commitment: Commitment,
    *,
    hint: str | None = None,
    client: ModelClient | None = None,
    organisation_id: uuid.UUID | None = None,
) -> list[CommitmentSupersessionCandidate]:
    """For each same-vendor, same-deliverable prior commitment found, asks
    the model once whether the new one looks like a revision of it. Only
    ever writes a row for a "yes" verdict — never for a "no", same "only
    surface a real finding" posture app/foresight/risk.py's own detectors
    already establish for this codebase's other auto-drafted findings. A
    malformed/unparseable model response is skipped, not raised — this
    function runs inline with commitment creation (extract_case /
    create_commitment), and a candidate proposal failing must never block
    the commitment itself from being recorded.

    `hint` is an optional short string threaded from a caller that already
    has extra signal — e.g. extract_case passing along cue-eval/schema.json's
    own `price_changed` field, a tuned extraction-time signal that already
    existed but was never consumed downstream before this module. Does not
    commit — caller's transaction boundary, same convention
    recompute_vendor_metrics/draft_deviation already follow.
    """
    priors = await find_candidate_priors(session, commitment)
    if not priors:
        return []

    client = client or get_client("reasoning")
    hint_line = "(Note: this newer commitment was flagged as a price change at extraction time.)\n" if hint == "price_changed" else ""

    created: list[CommitmentSupersessionCandidate] = []
    for prior in priors:
        prompt = _PROMPT_TEMPLATE.format(
            old_deliverable=prior.deliverable_en, old_amount=_format_amount(prior), old_due=_format_due(prior),
            new_deliverable=commitment.deliverable_en, new_amount=_format_amount(commitment),
            new_due=_format_due(commitment), hint=hint_line,
        )
        raw, usage = await client.complete(prompt, _SUPERSESSION_JSON_SCHEMA)
        if organisation_id is not None:
            await record_llm_usage(
                session, organisation_id=organisation_id, project_id=commitment.project_id,
                role="reasoning", purpose="supersession_candidate", usage=usage,
            )
        try:
            judgment = _SupersessionJudgment.model_validate(json.loads(raw))
        except (json.JSONDecodeError, ValidationError):
            continue
        if not judgment.supersedes:
            continue

        candidate = CommitmentSupersessionCandidate(
            project_id=commitment.project_id, commitment_id=commitment.id,
            supersedes_commitment_id=prior.id, reasoning=judgment.reasoning, status="pending",
        )
        session.add(candidate)
        created.append(candidate)

    if created:
        await session.flush()
    return created


async def confirm_supersession_candidate(
    session: AsyncSession, *, candidate: CommitmentSupersessionCandidate, actor_id: uuid.UUID,
) -> CommitmentSupersessionCandidate:
    """The only path that ever mutates Commitment.supersedes. Reassigns the
    whole list (never an in-place .append) — SQLAlchemy's change tracking
    on a plain ARRAY column only fires on reassignment, the same reasoning
    every other array-column write in this codebase already follows
    (app/documents/models.py's SpecClaim.contradicts is scalar so this
    doesn't apply there, but this is the first array-column *write* path in
    the Ledger domain).

    Also triggers a fresh vendor-metrics recompute (app/parties/service.py)
    so revision_churn/price_drift_pct reflect the new link immediately —
    same "a metric refresh follows every commitment state change" posture
    that module's own docstring establishes for transitions, extended here
    to a supersedes-array change, which is exactly the kind of event those
    two metrics exist to react to.
    """
    commitment = (
        await session.execute(select(Commitment).where(Commitment.id == candidate.commitment_id))
    ).scalar_one()
    if candidate.supersedes_commitment_id not in commitment.supersedes:
        commitment.supersedes = [*commitment.supersedes, candidate.supersedes_commitment_id]

    candidate.status = "confirmed"
    candidate.reviewed_by = actor_id
    candidate.reviewed_at = datetime.now(dt_timezone.utc)
    await session.flush()
    await session.refresh(candidate, attribute_names=["updated_at"])

    await recompute_vendor_metrics(session, commitment.party_id)
    return candidate


async def reject_supersession_candidate(
    session: AsyncSession, *, candidate: CommitmentSupersessionCandidate, actor_id: uuid.UUID,
) -> CommitmentSupersessionCandidate:
    """No Commitment change — a rejected candidate is simply a model
    judgment a human overruled, kept (not deleted) as its own honest record
    rather than silently discarded."""
    candidate.status = "rejected"
    candidate.reviewed_by = actor_id
    candidate.reviewed_at = datetime.now(dt_timezone.utc)
    await session.flush()
    await session.refresh(candidate, attribute_names=["updated_at"])
    return candidate
