"""No DB needed — pure validation of the Pydantic mirror of
cue-eval/schema.json, and a drift check between the two representations
(app/ledger/schema.py's own docstring warns these must be kept in sync by
hand; this is the automated tripwire for that)."""

import pytest
from pydantic import ValidationError

from app.ledger.schema import ExtractionResult, load_extraction_json_schema


def test_valid_extraction_parses():
    result = ExtractionResult.model_validate(
        {
            "commitments": [
                {
                    "act_type": "quote",
                    "deliverable_en": "truss supply and install",
                    "deliverable_original": "truss",
                    "amount": 8400,
                    "currency": "SGD",
                    "evidence_span": "truss 报价出来了",
                    "confidence": 0.9,
                }
            ]
        }
    )
    assert result.commitments[0].act_type == "quote"
    assert result.commitments[0].due_at is None  # optional field, correctly defaults


def test_missing_required_field_rejected():
    with pytest.raises(ValidationError):
        ExtractionResult.model_validate(
            {
                "commitments": [
                    {
                        "act_type": "quote",
                        "deliverable_en": "truss",
                        # deliverable_original missing — required by schema.json
                        "evidence_span": "truss",
                        "confidence": 0.9,
                    }
                ]
            }
        )


def test_invalid_act_type_rejected():
    with pytest.raises(ValidationError):
        ExtractionResult.model_validate(
            {
                "commitments": [
                    {
                        "act_type": "cancel",  # not in the enum
                        "deliverable_en": "x",
                        "deliverable_original": "x",
                        "evidence_span": "x",
                        "confidence": 0.9,
                    }
                ]
            }
        )


def test_pydantic_model_matches_json_schema_act_types():
    """Drift tripwire: if schema.json's act_type enum is ever tuned (CLAUDE.md
    treats it as a tuned artefact) without updating the Pydantic ActType
    Literal, this catches it instead of failing silently at parse time."""
    from app.ledger.schema import ActType
    from typing import get_args

    json_schema = load_extraction_json_schema()
    json_enum = set(json_schema["properties"]["commitments"]["items"]["properties"]["act_type"]["enum"])
    pydantic_enum = set(get_args(ActType))
    assert json_enum == pydantic_enum


def test_pydantic_required_fields_match_json_schema():
    json_schema = load_extraction_json_schema()
    json_required = set(json_schema["properties"]["commitments"]["items"]["required"])

    from app.ledger.schema import ExtractedCommitment

    pydantic_required = {
        name for name, field in ExtractedCommitment.model_fields.items() if field.is_required()
    }
    assert json_required == pydantic_required
