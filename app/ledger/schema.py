import copy
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

# backend/cue-eval/. Not a package import (cue-eval is deliberately
# stdlib-only) — just a shared path on disk.
_CUE_EVAL_DIR = Path(__file__).resolve().parents[2] / "cue-eval"

ActType = Literal[
    "offer", "commit", "confirm", "revoke", "renegotiate", "quote", "approve", "escalate", "query"
]


class ExtractedCommitment(BaseModel):
    """Mirrors cue-eval/schema.json's `commitments[]` item exactly. Two
    representations of the same contract exist on purpose: schema.json is what
    gets sent to the model as the structured-output constraint (loaded raw,
    below); this Pydantic model is what validates and types the parsed response
    on the way into the DB. If you change one, change the other — cue-eval's
    own regression suite is what would catch a silent drift between them.
    """

    act_type: ActType
    deliverable_en: str
    deliverable_original: str
    due_at: str | None = None
    amount: float | None = None
    currency: str | None = None
    price_changed: bool | None = None
    counterparty_name: str | None = None
    relates_to: str | None = None
    evidence_span: str
    confidence: float


class ExtractionResult(BaseModel):
    commitments: list[ExtractedCommitment]


def load_extraction_json_schema() -> dict:
    """The raw JSON Schema passed to the model as a hard output constraint —
    loaded from cue-eval/schema.json, the single tuned source, not duplicated."""
    return json.loads((_CUE_EVAL_DIR / "schema.json").read_text(encoding="utf-8"))


def build_extraction_json_schema(allowed_refs: list[str] | None = None) -> dict:
    """cue-eval/schema.json with `relates_to` narrowed to the refs actually
    offered on this call.

    CLAUDE.md's first hard rule is "Enforce, don't ask. Output shape is
    controlled by JSON Schema — enums, typed nulls, required fields. Never by
    asking politely in the prompt." A model pointing at an already-logged
    commitment is exactly that kind of constraint: the set of things it may
    point at is known at call time and is small, so it belongs in the schema
    the decoder is constrained by, not in a sentence asking the model to only
    use listed references.

    With no refs supplied (a project with an empty ledger, or a caller that
    does not load context at all) the enum collapses to `[None]` — the field
    becomes structurally impossible to fill, rather than being left open for
    the model to invent a plausible-looking "C1" that refers to nothing. The
    static file keeps the wider `["string", "null"]` type so it stays readable
    and valid on its own; this narrowing is per-call, never written back.

    `type` is *removed* when the enum goes on, and that is not cosmetic. A
    union type alongside an enum containing null is legal JSON Schema, and
    Ollama accepts it, but the Anthropic structured-outputs validator rejects
    it outright:

        output_config.format.schema: Invalid schema:
        Enum value None does not match declared type '['string', 'null']'

    Production extraction runs claude-haiku-4-5, so every call carrying ledger
    context returned a 400 — the entire `relates_to` path was dead against the
    production model while passing locally, because the local model is the
    only one that accepts the shape. An enum already fixes the value space
    exactly, so the type annotation was redundant even where it was accepted.
    """
    schema = copy.deepcopy(load_extraction_json_schema())
    relates_to = schema["properties"]["commitments"]["items"]["properties"]["relates_to"]
    relates_to.pop("type", None)
    relates_to["enum"] = [None, *(allowed_refs or [])]
    return schema


def load_extraction_prompt_template() -> str:
    """cue-eval/prompt.txt, loaded directly so production extraction runs the
    exact artefact the eval harness measures — not a copy that can drift."""
    return (_CUE_EVAL_DIR / "prompt.txt").read_text(encoding="utf-8")
