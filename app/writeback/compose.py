import json

from pydantic import ValidationError

from app.llm.client import ModelClient
from app.llm.factory import get_client
from app.models import Commitment
from app.writeback.schema import COMPOSE_DRAFT_JSON_SCHEMA, ComposedDraft

# FR-WBK-03 (PRD §6.12): "Phrase confirmations as a single closed question
# answerable in one word." CUE-PRD.md §12.6 makes this the entire vendor-side
# product surface — "at most one message per group per day ... phrased as a
# single closed question" — so getting this prompt right is load-bearing the
# same way the extraction prompt is (CLAUDE.md: "treat it with the same
# discipline"). get_client("reasoning"), not "extraction" — this is a
# judgment/generation task over a commitment's current field values, the same
# role distinction app/foresight/contradiction.py's own comment draws for a
# comparable "not extraction" LLM call.
_PROMPT_TEMPLATE = """You are drafting a single outbound message to a vendor, confirming one specific commitment on a project. The vendor's group language is: {language}.

Commitment: {deliverable}
Due: {due}
Amount: {amount}
Current state: {state}

Write ONE closed question, in {language}, that a vendor could answer in a single word (e.g. "yes"/"no", or a one-word confirmation) to confirm whether this commitment still holds as stated. Do not restate every field — ask about the one thing that most needs reconfirming right now. Do not add greetings, signatures, or explanation. The question must end with a question mark."""

_QUESTION_MARKS = ("?", "？")  # ASCII + full-width (CJK) question marks


def _format_commitment(commitment: Commitment) -> dict[str, str]:
    return {
        "deliverable": commitment.deliverable_en,
        "due": commitment.due_at.isoformat() if commitment.due_at else "not specified",
        "amount": (
            f"{commitment.amount} {commitment.currency}" if commitment.amount is not None
            else "not specified"
        ),
        "state": commitment.state,
    }


class ComposeError(Exception):
    """Raised when the model's output fails schema validation, or (verified
    in code, not trusted, same discipline CLAUDE.md sets for evidence spans)
    isn't actually phrased as a question — never silently sent as-is."""


async def compose_draft(
    commitment: Commitment, *, language: str, client: ModelClient | None = None
) -> str:
    client = client or get_client("reasoning")
    fields = _format_commitment(commitment)
    prompt = _PROMPT_TEMPLATE.format(language=language, **fields)

    raw = await client.complete(prompt, COMPOSE_DRAFT_JSON_SCHEMA)
    try:
        draft = ComposedDraft.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as e:
        raise ComposeError(f"model output failed schema validation: {e}") from e

    question = draft.question.strip()
    if not question.endswith(_QUESTION_MARKS):
        raise ComposeError(f"composed draft is not phrased as a closed question: {question!r}")
    return question
