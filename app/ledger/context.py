"""The "what has already been logged for this conversation" lookup that
extraction reads *before* it reads a new message.

Why this module exists (and why a prompt reword could never have replaced it):
app/ledger/extractor.py's build_prompt used to format project context plus
exactly one message, and nothing else. A message that reacts to an earlier one — "can we get written
confirmation the 4pm start still lands?" — contains no token that
distinguishes it from a genuinely new request, because the fact that makes it
a restatement lives in a *different* message. Asking the model to be more
careful cannot supply information the model was never given; two attempts to
do so are recorded in extraction-tuning-task.md, both reverted.

So this hands the model a short, numbered, closed set of the commitments
already on the ledger for this project, and cue-eval/schema.json's
`relates_to` lets it point at one instead of inventing a new record. The
choice is deliberately a closed enum injected into the JSON Schema at call
time (app/ledger/schema.py's build_extraction_json_schema), not free text —
CLAUDE.md's "enforce, don't ask": the model cannot name a commitment that was
not offered to it, because the decoder will not let it.

The retrieval itself is plain SQL, not embeddings. app/ask/retrieve.py's
hybrid pgvector+lexical search exists and would work, but "the open
commitments on this project" is a small, exactly-specified set — a similarity
search over it would add a failure mode (a wrong neighbour ranked first) and
an embedding round-trip to every extraction, buying nothing a WHERE clause
does not already give. Same reasoning app/ledger/supersession.py's own
find_candidate_priors states for staying with SQL.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Commitment, Party

# A commitment in one of these states is still live enough that a new message
# could plausibly be talking about it. "delivered" is included on purpose:
# status-check chatter ("did the crates land?") clusters on things that just
# completed, and that chatter is precisely what was being mis-extracted as new
# commitments. "withdrawn" is not — it is the one state that means "this is no
# longer part of the ledger at all", so offering it back to the model would
# invite re-opening something a human explicitly retired.
_OPEN_STATES = ("proposed", "committed", "at_risk", "renegotiated", "broken", "delivered")

# How many commitments the model is shown. Small on purpose: this list is
# prepended to every extraction call, so it is a per-message token cost on the
# hot path, and a long list measurably dilutes attention on the message itself
# — the failure mode CLAUDE.md warns about under "do not over-tune against the
# local model", reached here through context length rather than prompt wording.
# Twelve covers a busy week of one project's live commitments.
_DEFAULT_LIMIT = 12

_REF_PREFIX = "C"


@dataclass(frozen=True)
class LedgerContextItem:
    """One already-logged commitment, as the model sees it. `ref` is a short
    stable handle ("C1") rather than the UUID: the model only ever has to echo
    two or three characters back, and app/ledger/extractor.py maps the ref to
    the real `commitment_id` in code, so a malformed or invented reference
    cannot reach the database as an id."""

    ref: str
    commitment_id: uuid.UUID
    vendor: str
    deliverable_en: str
    due_at: str | None
    amount: str | None
    state: str


async def load_open_commitment_context(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    pinned_commitment_ids: tuple[uuid.UUID, ...] = (),
    limit: int = _DEFAULT_LIMIT,
) -> list[LedgerContextItem]:
    """The project's most recent open commitments, newest first.

    `pinned_commitment_ids` are forced into the result even if they fall
    outside the `limit` window. The caller that needs this is
    app/capture/pipeline.py: when app/writeback/reply.py has *just* matched
    this same inbound message to a pending write-back and transitioned a
    commitment, that commitment is by construction the one the message is
    about — it must be in the list, or extraction is being asked the very
    question it cannot answer while being denied the answer.

    Scoped to project_id at the query level, the same belt-and-braces posture
    over RLS every other query in this codebase uses.
    """
    stmt = (
        select(Commitment, Party.display_name)
        .join(Party, Party.id == Commitment.party_id)
        .where(
            Commitment.project_id == project_id,
            Commitment.state.in_(_OPEN_STATES),
        )
        # `id` breaks the tie deliberately: one message routinely produces
        # several commitments in a single transaction, and those share a
        # created_at to the microsecond. Ordering on the timestamp alone
        # leaves their relative order up to the planner, which would let the
        # *contents* of a limited window shift between two identical calls —
        # and the refs (C1, C2, ...) are positional, so an unstable window is
        # an unstable meaning for "C2".
        .order_by(Commitment.created_at.desc(), Commitment.id.desc())
        .limit(limit)
    )
    rows = list((await session.execute(stmt)).all())

    if pinned_commitment_ids:
        present = {c.id for c, _ in rows}
        missing = [cid for cid in pinned_commitment_ids if cid not in present]
        if missing:
            pinned_stmt = (
                select(Commitment, Party.display_name)
                .join(Party, Party.id == Commitment.party_id)
                .where(Commitment.project_id == project_id, Commitment.id.in_(missing))
            )
            rows = list((await session.execute(pinned_stmt)).all()) + rows

    items: list[LedgerContextItem] = []
    for index, (commitment, vendor_name) in enumerate(rows, start=1):
        items.append(
            LedgerContextItem(
                ref=f"{_REF_PREFIX}{index}",
                commitment_id=commitment.id,
                vendor=vendor_name,
                deliverable_en=commitment.deliverable_en,
                due_at=commitment.due_at.isoformat() if commitment.due_at else None,
                amount=(
                    f"{commitment.amount} {commitment.currency or ''}".strip()
                    if commitment.amount is not None
                    else None
                ),
                state=commitment.state,
            )
        )
    return items


def render_ledger_context(items: list[LedgerContextItem]) -> str:
    """The block substituted into cue-eval/prompt.txt's {open_commitments}.

    An explicit "(none)" rather than an empty string when there is nothing
    logged yet: a bare blank under a heading reads to a model as a truncated
    prompt, and the empty case is the common one on a new project, so it is
    worth being unambiguous about.
    """
    if not items:
        return "  (none — nothing has been logged for this project yet)"
    lines = []
    for item in items:
        parts = [f"{item.vendor}: {item.deliverable_en}"]
        if item.due_at:
            parts.append(f"due {item.due_at}")
        if item.amount:
            parts.append(item.amount)
        parts.append(item.state)
        lines.append("  {} — {}".format(item.ref, ", ".join(parts)))
    return "\n".join(lines)


def relates_to_refs(items: list[LedgerContextItem]) -> list[str]:
    return [item.ref for item in items]


def resolve_ref(items: list[LedgerContextItem], ref: str | None) -> uuid.UUID | None:
    """Model-returned ref -> real commitment id, or None if it named nothing
    (or named something not on the list — which the JSON Schema enum should
    already have made impossible, but this is the code-side check that makes
    that guarantee real rather than assumed, the same way evidence_span is
    re-verified in code instead of trusted)."""
    if not ref:
        return None
    for item in items:
        if item.ref == ref:
            return item.commitment_id
    return None
