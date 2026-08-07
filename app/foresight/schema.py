from pydantic import BaseModel

# app/foresight/contradiction.py's LLM-assisted judgment for free-text spec
# claims (attribute='finish') — mirrors app/ledger/schema.py's split between
# the raw JSON Schema sent as a hard output constraint and the Pydantic
# model that validates the parsed response, same "if you change one, change
# the other" discipline. Inline here rather than a separate on-disk schema
# file (unlike cue-eval/schema.json) — this is a small, single-purpose
# judgment schema with no eval harness of its own to keep in sync with.

CONTRADICTION_JUDGMENT_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "contradicts": {
            "type": "boolean",
            "description": "true only if the two claims describe genuinely incompatible "
            "specifications for the same item; false if compatible, consistent, or simply "
            "phrased at different levels of specificity.",
        },
        "explanation": {"type": "string"},
    },
    "required": ["contradicts", "explanation"],
}


class ContradictionJudgment(BaseModel):
    contradicts: bool
    explanation: str
