from typing import Literal

from pydantic import BaseModel

# Mirrors app/ledger/schema.py's shape (a Pydantic model that validates and
# types the parsed response, plus a hand-written JSON Schema sent to the
# model as the structured-output constraint) — no separate eval harness for
# spec-claim extraction exists this session (unlike cue-eval/, which is
# commitment extraction's tuned, regression-gated artefact per CLAUDE.md), so
# the schema lives inline here rather than in a loaded file.

SpecClaimAttributeLiteral = Literal["dimension", "finish", "quantity", "price"]


class ExtractedSpecClaim(BaseModel):
    """CUE-PRD.md §4.3's Spec Claim fields the model is responsible for
    proposing — `document_version_id` and `evidence` are supplied by
    app/documents/extractor.py itself, never by the model."""

    location_code: str | None = None
    attribute: SpecClaimAttributeLiteral
    value: str
    evidence_span: str
    confidence: float


class SpecClaimExtractionResult(BaseModel):
    claims: list[ExtractedSpecClaim]


SPEC_CLAIM_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "location_code": {"type": ["string", "null"]},
                    "attribute": {
                        "type": "string",
                        "enum": ["dimension", "finish", "quantity", "price"],
                    },
                    "value": {"type": "string"},
                    "evidence_span": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["attribute", "value", "evidence_span", "confidence"],
            },
        }
    },
    "required": ["claims"],
}


def load_spec_claim_json_schema() -> dict:
    return SPEC_CLAIM_JSON_SCHEMA
