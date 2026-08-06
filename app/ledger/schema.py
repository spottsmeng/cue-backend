import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

# The repo's cue-eval/ directory, sibling to backend/. Not a package import
# (cue-eval is deliberately stdlib-only) — just a shared path on disk.
_CUE_EVAL_DIR = Path(__file__).resolve().parents[3] / "cue-eval"

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
    evidence_span: str
    confidence: float


class ExtractionResult(BaseModel):
    commitments: list[ExtractedCommitment]


def load_extraction_json_schema() -> dict:
    """The raw JSON Schema passed to the model as a hard output constraint —
    loaded from cue-eval/schema.json, the single tuned source, not duplicated."""
    return json.loads((_CUE_EVAL_DIR / "schema.json").read_text(encoding="utf-8"))


def load_extraction_prompt_template() -> str:
    """cue-eval/prompt.txt, loaded directly so production extraction runs the
    exact artefact the eval harness measures — not a copy that can drift."""
    return (_CUE_EVAL_DIR / "prompt.txt").read_text(encoding="utf-8")
