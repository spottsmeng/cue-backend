import itertools
import json
import re

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.documents.models import Document, DocumentVersion, SpecClaim
from app.foresight.consequence import deliverable_downstream_consequence
from app.foresight.deviation import draft_deviation
from app.foresight.models import Risk
from app.foresight.notification import dispatch_event
from app.foresight.risk import create_or_supersede_risk
from app.foresight.schema import CONTRADICTION_JUDGMENT_JSON_SCHEMA, ContradictionJudgment
from app.llm.client import ModelClient
from app.llm.factory import get_client
from app.models import Project

# FR-FOR-03/04 (PRD §6.9): contradiction / spec-drift detection. Compares
# SpecClaim rows sharing a deliverable_id or location_code — app/documents/
# models.py's SpecClaim docstring hands this comparison to Foresight
# explicitly ("`contradicts` is populated later, not by this session").
#
# Rule-based direct value comparison for structured attributes (quantity,
# price, and dimension when both sides parse as pure numeric patterns);
# LLM-assisted judgment (get_client("reasoning") — a comparison judgment is
# reasoning, not extraction, same role distinction app/llm/factory.py
# already draws) for genuinely natural-language claims (finish, or a
# dimension claim that doesn't parse cleanly) — per Prompt 7's own explicit
# instruction to split the two this way.

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _parse_numbers(value: str) -> list[float] | None:
    matches = _NUMBER_RE.findall(value)
    if not matches:
        return None
    return sorted(float(m) for m in matches)


def _rule_based_conflict(attribute: str, value_a: str, value_b: str) -> bool | None:
    """Returns True/False when a direct value comparison can decide it,
    None when this attribute/value pair needs the LLM-assisted path
    instead (contradiction.py's caller falls through to that)."""
    if attribute in ("quantity", "price"):
        numbers_a, numbers_b = _parse_numbers(value_a), _parse_numbers(value_b)
        if numbers_a is None or numbers_b is None:
            return None
        return numbers_a != numbers_b
    if attribute == "dimension":
        numbers_a, numbers_b = _parse_numbers(value_a), _parse_numbers(value_b)
        # Only decide via rule when both sides give the same count of
        # numbers (e.g. "2040mm x 1040mm" vs "2000mm x 1040mm") — a
        # differently-shaped dimension string (one number vs two) is
        # ambiguous enough to defer to the LLM instead of guessing.
        if numbers_a is None or numbers_b is None or len(numbers_a) != len(numbers_b):
            return None
        return numbers_a != numbers_b
    return None  # 'finish' is always natural language — always LLM-assisted


async def _llm_conflict(
    attribute: str, value_a: str, value_b: str, client: ModelClient | None = None
) -> bool:
    client = client or get_client("reasoning")
    prompt = (
        "Two spec claims describe the same item (same deliverable/location) across a vendor's "
        f"documents. Attribute: {attribute}.\n"
        f"Claim A: {value_a!r}\n"
        f"Claim B: {value_b!r}\n\n"
        "Do these two claims genuinely contradict each other — describe incompatible "
        "specifications for the same item — or are they compatible/consistent (e.g. one is more "
        "specific, or phrased differently but means the same thing)?"
    )
    raw = await client.complete(prompt, CONTRADICTION_JUDGMENT_JSON_SCHEMA)
    try:
        judgment = ContradictionJudgment.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError):
        # A malformed judgment is not evidence of a contradiction — same
        # "never fabricate what you don't have" posture as everywhere else
        # in this codebase; treat as "no conflict detected" rather than
        # guessing, and let the next scan try again.
        return False
    return judgment.contradicts


async def scan_contradictions(
    session: AsyncSession, project: Project, *, client: ModelClient | None = None
) -> list[Risk]:
    """Runs contradiction/spec-drift detection for one project. Groups
    SpecClaim rows by (deliverable_id, location_code) — sharing either
    identifies "the same item" per Prompt 7's own instruction — and compares
    every same-attribute pair within a group that isn't already linked.
    Does not commit — caller controls the transaction boundary."""
    rows = (
        await session.execute(
            select(SpecClaim)
            .join(DocumentVersion, DocumentVersion.id == SpecClaim.document_version_id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(Document.project_id == project.id)
        )
    ).scalars().all()

    groups: dict[tuple, list[SpecClaim]] = {}
    for claim in rows:
        if claim.deliverable_id is None and claim.location_code is None:
            continue  # nothing to group this claim against
        key = (claim.deliverable_id, claim.location_code)
        groups.setdefault(key, []).append(claim)

    touched: list[Risk] = []
    for (deliverable_id, _location_code), claims in groups.items():
        for claim_a, claim_b in itertools.combinations(claims, 2):
            if claim_a.attribute != claim_b.attribute:
                continue
            if claim_a.document_version_id == claim_b.document_version_id:
                continue  # same document version — not a cross-document conflict
            if claim_a.value == claim_b.value:
                continue
            if claim_a.contradicts == claim_b.id or claim_b.contradicts == claim_a.id:
                continue  # already linked by a prior scan

            conflict = _rule_based_conflict(claim_a.attribute, claim_a.value, claim_b.value)
            if conflict is None:
                conflict = await _llm_conflict(claim_a.attribute, claim_a.value, claim_b.value, client)
            if not conflict:
                continue

            older, newer = (claim_a, claim_b) if claim_a.id < claim_b.id else (claim_b, claim_a)
            newer.contradicts = older.id
            await session.flush()

            consequence = await deliverable_downstream_consequence(
                session, project, deliverable_id,
                because=(
                    f"Spec claims disagree on {claim_a.attribute}: {claim_a.value!r} vs "
                    f"{claim_b.value!r}"
                ),
            )
            risk, created = await create_or_supersede_risk(
                session,
                project_id=project.id,
                source="contradiction",
                finding_key=f"contradiction:{older.id}:{newer.id}",
                severity="high" if claim_a.attribute in ("dimension", "quantity", "price") else "medium",
                downstream_consequence=consequence,
                spec_claim_id=newer.id,
                detail={
                    "attribute": claim_a.attribute,
                    "value_a": claim_a.value,
                    "value_b": claim_b.value,
                    "claim_a_id": str(claim_a.id),
                    "claim_b_id": str(claim_b.id),
                },
            )
            touched.append(risk)

            if created:
                await dispatch_event(session, project=project, event_type="risk", risk=risk)
                # FR-DEV-04: a spec contradiction is a qualifying deviation
                # event (scope/spec drift), auto-drafted for a PM to confirm.
                await draft_deviation(
                    session,
                    project=project,
                    class_code="spec_drift",
                    description_en=(
                        f"Spec disagreement on {claim_a.attribute}: {claim_a.value!r} vs "
                        f"{claim_b.value!r}"
                    ),
                    risk=risk,
                    evidence_text=(
                        f"Detected by contradiction scan: claim {claim_a.id} ({claim_a.value!r}) "
                        f"vs claim {claim_b.id} ({claim_b.value!r})"
                    ),
                )

    return touched
