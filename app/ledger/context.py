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

The retrieval itself is plain SQL plus an in-process lexical rank, not
embeddings. app/ask/retrieve.py's hybrid pgvector+lexical search exists and
would work, but "the open commitments on this project" is a small,
exactly-specified set — a similarity search over it would add an embedding
round-trip to every extraction and a failure mode (a wrong neighbour ranked
first) for recall a WHERE clause plus token overlap already reaches. Same
reasoning app/ledger/supersession.py's own find_candidate_priors states for
staying with SQL.

What the lexical rank is for: the *window* has to stay short (a long list
dilutes attention on the message itself, and it is a per-message token cost
on the hot path), but "short" used to mean "the twelve newest", which made
being old indistinguishable from being absent. Since a commitment the model
was never shown cannot be cited, an aged-out commitment silently became a
duplicate ledger row — the exact bug this module exists to prevent, returning
at the project size where it costs most. So the window stays at twelve and
the *choice* of which twelve now accounts for what the message says.
"""

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Commitment, Party

logger = logging.getLogger("cue.ledger.context")

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

# How deep the relevance pass is allowed to look before ranking. The window
# the model sees stays at _DEFAULT_LIMIT; this only widens the set that window
# is *chosen from*, so a commitment older than twelve rows can still be
# offered when the message is plainly about it. Capped rather than unbounded
# so a long-running project cannot turn every extraction into a full-table
# scan.
_RELEVANCE_SCAN_CAP = 200

# Slots reserved for relevance matches, out of _DEFAULT_LIMIT. The rest are
# filled by recency, so this can only ever *add* a row the old behaviour would
# have missed — it never empties the window on a message that matches nothing,
# which is the common case and the one the recency ordering already served.
_RELEVANCE_RESERVE = 6

# Latin tokens shorter than this carry no disambiguating signal ("to", "of").
_MIN_TOKEN_LEN = 3

# Deliverables are short noun phrases, so this only has to cover the few
# function words that survive into one ("supply and install", "screen for
# stage").
_STOPWORDS = frozenset(
    {"and", "the", "for", "with", "per", "plus", "from", "into", "our", "their"}
)


def _is_cjk(char: str) -> bool:
    return "一" <= char <= "鿿"


def _tokens(text: str) -> set[str]:
    """Comparable units of a message or a deliverable name.

    Whitespace tokenisation alone is wrong for half this corpus: Chinese runs
    together with no spaces, so a 中文 deliverable would score zero against a
    中文 message naming it. Latin words fall out as words; CJK runs fall out as
    character bigrams, which is the standard cheap substitute for segmentation
    and enough to tell "铝框交付" from "屏幕安装".
    """
    lowered = text.casefold()
    tokens: set[str] = set()
    latin: list[str] = []
    cjk_run: list[str] = []

    def flush_cjk() -> None:
        if len(cjk_run) == 1:
            tokens.add(cjk_run[0])
        for i in range(len(cjk_run) - 1):
            tokens.add(cjk_run[i] + cjk_run[i + 1])
        cjk_run.clear()

    def flush_latin() -> None:
        word = "".join(latin)
        if len(word) >= _MIN_TOKEN_LEN and word not in _STOPWORDS:
            tokens.add(word)
        latin.clear()

    for char in lowered:
        if _is_cjk(char):
            flush_latin()
            cjk_run.append(char)
        elif char.isalnum():
            flush_cjk()
            latin.append(char)
        else:
            flush_latin()
            flush_cjk()
    flush_latin()
    flush_cjk()
    return tokens


def _relevance_score(message_tokens: set[str], vendor: str, deliverable_en: str) -> int:
    """How strongly this commitment looks like what the message is about.

    Vendor match is weighted above deliverable match because it is the
    stronger disambiguator: two commitments on a project routinely share a
    deliverable name ("screen install") and almost never share a vendor. Any
    single token of the vendor name counts, so "Ah Seng's" in a message
    reaches "Ah Seng Production" on the ledger — the possessive form that
    cue-eval CD03 shows the model itself writing.

    Used only to decide which rows are *offered* to the model, never to decide
    the link. A wrong ranking costs a worse candidate list, which is what the
    recency-only version already had; it cannot produce a wrong `relates_to`,
    because the model still has to choose and code still re-verifies the ref.
    """
    score = 0
    if message_tokens & _tokens(vendor):
        score += 3
    score += len(message_tokens & _tokens(deliverable_en))
    return score


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


def _select_by_relevance(rows: list, *, message: str, limit: int) -> list:
    """Pick `limit` rows out of the recency-ordered scan window.

    `rows` arrives newest-first. Relevance takes at most _RELEVANCE_RESERVE of
    the slots, recency fills the rest, and the result is re-sorted back into
    the original recency order so the positional refs (C1, C2, ...) still read
    newest-first and stay stable for a given input.
    """
    if len(rows) <= limit:
        return rows

    message_tokens = _tokens(message)
    scored = [
        (index, _relevance_score(message_tokens, vendor_name, commitment.deliverable_en))
        for index, (commitment, vendor_name) in enumerate(rows)
    ]
    # Sort by score, then by the incoming recency order, so ties resolve
    # deterministically rather than by whatever order the scan returned.
    relevant = sorted(
        (entry for entry in scored if entry[1] > 0),
        key=lambda entry: (-entry[1], entry[0]),
    )[:_RELEVANCE_RESERVE]

    chosen = {index for index, _score in relevant}
    for index in range(len(rows)):
        if len(chosen) >= limit:
            break
        chosen.add(index)

    truncated = len(rows) - len(chosen)
    if truncated > 0:
        # The failure this whole function exists to stop being silent. A
        # commitment the model was never shown cannot be cited, so it becomes
        # a duplicate ledger row instead of a link, and nothing downstream
        # can tell that from a genuinely new promise.
        logger.info(
            "ledger context truncated: showing %d of %d open commitments "
            "(%d relevance-matched), %d not offered to the model",
            len(chosen), len(rows), len(relevant), truncated,
        )

    return [rows[index] for index in sorted(chosen)]


async def load_open_commitment_context(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    pinned_commitment_ids: tuple[uuid.UUID, ...] = (),
    limit: int = _DEFAULT_LIMIT,
    message: str | None = None,
) -> list[LedgerContextItem]:
    """The project's open commitments most likely to be what a message is
    about — relevance-matched first, then recency, newest first.

    `message` is the inbound text being extracted. Without it this is the
    pure recency window it has always been. With it, up to
    _RELEVANCE_RESERVE of the slots go to commitments that lexically match
    the message, and the remainder are filled by recency exactly as before.

    Why this matters more than it looks: cue-eval CD01 and CD02 are the same
    *class* of message — internal staff reacting to something already logged —
    and the only difference between them is whether the commitment was in this
    list. CD02, with it, links correctly 5/5 runs. CD01, without it, invents a
    commitment 5/5 runs. A recency-only window silently turns the first into
    the second as soon as a project has more live commitments than fit, which
    is every real Pico event. The list stays short (token cost and attention
    dilution are real, see _DEFAULT_LIMIT); what changes is that being old is
    no longer the same as being invisible.

    Reserving rather than replacing is deliberate. On a message that matches
    nothing the result is byte-identical to the old behaviour, so this can add
    a row the old window would have missed and cannot take away the ones it
    would have shown.

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
        .limit(limit if message is None else _RELEVANCE_SCAN_CAP)
    )
    rows = list((await session.execute(stmt)).all())

    if message is not None:
        rows = _select_by_relevance(rows, message=message, limit=limit)

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
