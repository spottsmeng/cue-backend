"""FR-ASK-06's own explicit instruction, read literally: "Do not fake an
'action taken' response — if the underlying capability doesn't exist, the
endpoint should say it can't do that yet, not pretend to." Telling the
answer-generation model "please don't fabricate an action" inside its own
prompt is not enforcement — CLAUDE.md's whole discipline is "enforce, don't
ask," and that call's own `answer` field is free text with nothing
code-level stopping it from narrating "I've sent a message to the vendor"
while citing real (but action-unrelated) evidence.

This module is the actual enforcement: a narrow, separately schema-
constrained classification step that runs *before* app/ask/answer.py ever
builds an answer-generation prompt. The branch on its result is plain code
(app/ask/answer.py's answer_query), so an action-shaped request never
reaches the call capable of writing that fabricated sentence — there is no
execution path in which it could, not a prompt asking it not to.
"""

import json
import logging

from pydantic import BaseModel

from app.llm.client import ModelClient

logger = logging.getLogger("app.ask.intent")

_INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "is_action_request": {"type": "boolean"},
        "action_summary": {"type": ["string", "null"]},
    },
    "required": ["is_action_request", "action_summary"],
}

# FR-ASK-06's own four examples ("chase a vendor, confirm a date, reschedule
# a dependent task, draft a client update") reused verbatim as the positive
# examples below — this classifier's boundary is exactly that requirement's
# own enumeration, not a separately invented notion of "action-like."
_PROMPT_TEMPLATE = """You are a gatekeeper for a project-management assistant that can only answer questions grounded in existing records — it cannot take any action in the outside world yet (that capability, "Write-back," has not been built).

Decide whether the message below is asking the assistant to actually DO something outbound — contact or message a vendor/client, draft a message or update to send to them, confirm or change a date with them, or reschedule a task with an external party — or whether it is a question about information that already exists.

Examples that ARE an action request: "chase the vendor for an update", "tell the client the schedule slipped", "draft a message to the AV vendor about pricing", "reschedule the rigging with the contractor", "confirm the install date with them".

Examples that are NOT an action request (ordinary questions): "what did the vendor say about pricing?", "when is the rigging scheduled?", "has the AV vendor confirmed the install date?", "summarise what's outstanding with this vendor".

If it is an action request, briefly name the action in action_summary (e.g. "chase the AV vendor for a price update"); otherwise leave action_summary null.

Message: {question}"""


class IntentClassification(BaseModel):
    is_action_request: bool
    action_summary: str | None = None


async def classify_intent(question: str, reasoning_client: ModelClient) -> IntentClassification:
    """Fails open (treated as "not an action request") if the reasoning
    model itself can't be reached — same NFR-AVL-03 "degrade gracefully"
    posture app/ask/answer.py's own _embed_question already applies to the
    embedding client, for the same underlying reason: if the reasoning model
    is unreachable, the answer-generation call a genuine question would need
    is equally unreachable, so failing open here doesn't open a new failure
    mode — it just declines to *additionally* enforce the FR-ASK-06 guard on
    a request that was already going to fail downstream for the same
    infrastructure reason.
    """
    prompt = _PROMPT_TEMPLATE.format(question=question)
    try:
        raw = await reasoning_client.complete(prompt, _INTENT_SCHEMA)
        parsed = json.loads(raw)
        return IntentClassification(**parsed)
    except Exception:  # noqa: BLE001 — any failure of an external model call
        logger.warning(
            "intent classification failed; treating as a question, not an action request", exc_info=True
        )
        return IntentClassification(is_action_request=False, action_summary=None)
