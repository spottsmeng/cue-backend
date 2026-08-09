import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

# --- LLM-facing schemas (app/writeback/compose.py, app/writeback/reply.py) ---
#
# CLAUDE.md's extraction discipline, applied here too, per Prompt 12's own
# instruction: "Output shape is controlled by JSON Schema ... Never by asking
# politely in the prompt." Both calls below are validated against these
# schemas the same way app/ledger/schema.py's ExtractionResult is —
# model-output-shape enforced structurally, model-output-*content* still
# verified in code afterward (compose.py checks the question actually ends
# in a question mark; reply.py's proposed to_state still goes through
# app/ledger/lifecycle.py's validate_transition before anything is trusted).

COMPOSE_DRAFT_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": "A single closed question, in the target language, answerable in one "
            "word (e.g. yes/no, or a single confirmation word) — never a multi-part or open-ended "
            "question, never a restatement of the full commitment in prose.",
        },
    },
    "required": ["question"],
}


class ComposedDraft(BaseModel):
    question: str


# FR-WBK-06: "Parse the reply into a structured commitment state transition,
# in either language." `to_state` is intentionally free-text here (not
# constrained to CommitmentState's own enum in the schema) — a model that
# doesn't know the current state can't reliably pick from a state machine's
# full vocabulary, and app/ledger/lifecycle.py's validate_transition is what
# actually decides validity afterward; forcing the model to guess a smaller
# set here would just move the same failure mode one layer earlier without
# removing it. `parseable=false` is the model's own signal that the reply
# doesn't resolve to a clear transition at all (e.g. a question back, a
# non-answer) — FR-WBK-07's fallback path branches on this before ever
# looking at `to_state`.
REPLY_PARSE_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "parseable": {
            "type": "boolean",
            "description": "true only if the reply clearly resolves the closed question that was "
            "asked into an unambiguous commitment decision.",
        },
        "to_state": {
            "type": ["string", "null"],
            "description": "The commitment lifecycle state the reply implies "
            "('committed', 'delivered', 'renegotiated', 'withdrawn', 'broken', 'at_risk', "
            "'proposed'), or null if parseable is false.",
        },
        "reasoning": {"type": "string"},
    },
    "required": ["parseable", "to_state", "reasoning"],
}


class ReplyParse(BaseModel):
    parseable: bool
    to_state: str | None = None
    reasoning: str = ""


# --- REST-facing schemas (app/api/writeback.py) ---

OutboundMessageStatusLiteral = Literal["draft", "authorised", "sent"]
OutboundMessageReplyOutcomeLiteral = Literal["transitioned", "escalated"]


class WritebackDraftRequest(BaseModel):
    commitment_id: uuid.UUID


class OutboundMessageOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    channel_id: uuid.UUID
    commitment_id: uuid.UUID
    party_id: uuid.UUID
    to_external_id: str
    language: str
    draft_text: str
    status: OutboundMessageStatusLiteral
    authorised_by: uuid.UUID | None
    authorised_at: datetime | None
    sent_at: datetime | None
    reply_message_id: uuid.UUID | None
    reply_outcome: OutboundMessageReplyOutcomeLiteral | None
    reply_detail: dict
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class WritebackConfigOut(BaseModel):
    project_id: uuid.UUID
    daily_ceiling: int


class WritebackConfigUpdate(BaseModel):
    daily_ceiling: int
