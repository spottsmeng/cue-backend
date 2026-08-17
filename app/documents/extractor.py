import json
from datetime import datetime, timezone as dt_timezone
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.documents.models import DocumentVersion, SpecClaim
from app.documents.schema import SpecClaimExtractionResult, load_spec_claim_json_schema
from app.llm.client import ModelClient
from app.llm.cost import record_llm_usage
from app.llm.factory import get_client
from app.models import Evidence

# Mirrors app/ledger/extractor.py's established pattern for LLM-based
# structured extraction (build_prompt, schema-constrained output via
# app/llm/factory.py's get_client("extraction"), code-verified-not-trusted
# evidence validation before persisting) — per Prompt 6's own instruction to
# follow that "exact shape". OCR/document-parsing is out of scope this
# session (CUE-Tech-Stack.md §2.4 names PaddleOCR/Docling as the real
# production choice); this runs against DocumentVersion.extracted_text, the
# caller-supplied plain-text stand-in for what a real parsing pipeline would
# produce.


class RejectedSpecClaim(Exception):
    """Mirrors app/ledger/extractor.py's RejectedExtraction. Raised when a
    model-returned spec claim fails code-level verification — CLAUDE.md's
    'evidence_span must be an exact substring of the source message,
    verified in code, not trusted' applies identically here, with the
    source document's extracted text standing in for the source message
    (Prompt 6's own NON-OBVIOUS note: 'don't relax that bar just because the
    source is a document instead of a chat message'). Never persisted, never
    silently dropped — the caller decides what to do with the rejection."""


def build_prompt(document_version: DocumentVersion) -> str:
    text = document_version.extracted_text or ""
    return (
        "You are extracting structured spec claims (dimensions, finishes, quantities, prices) "
        "from a vendor document's text, for an event-production project. The document text is "
        "modelled on Annex A's Graphic List: Location, Description, Dimension (mm), Finishing, "
        "Qty, Status.\n\n"
        "Rules:\n"
        "- Each claim's evidence_span MUST be an exact, verbatim substring of the document text "
        "below — it is checked programmatically against that text, not trusted.\n"
        "- attribute must be exactly one of: dimension, finish, quantity, price.\n"
        "- value is the claim itself, e.g. '2040mm x 1040mm', 'Graphic print on plywood', '1 set'.\n"
        "- location_code is a position reference if the document gives one (e.g. a floor-plan or "
        "graphic-list row label like 'H', 'I'), otherwise omit it.\n"
        "- confidence is your own 0-1 confidence in this claim.\n"
        "- Only extract claims that are explicitly stated in the text — never infer a value that "
        "isn't written down.\n\n"
        f"Document text:\n{text}\n"
    )


async def extract_spec_claims(
    session: AsyncSession,
    *,
    project_id: UUID,
    organisation_id: UUID,
    document_version: DocumentVersion,
    client: ModelClient | None = None,
) -> list[SpecClaim]:
    """Prompt -> model -> schema-validate -> code-verify evidence -> write
    SpecClaim + Evidence — exactly app/ledger/extractor.py's extract_case
    shape, applied to a DocumentVersion instead of a chat message. Does not
    commit — caller controls the transaction boundary, since the deferred
    spec_claim_requires_evidence trigger only fires at COMMIT, and a batch
    of claims may want to commit per-claim or all together.

    `client` defaults to the configured extraction-role client but can be
    overridden — tests pass a fake client, same as
    tests/test_extractor.py's FakeModelClient.
    """
    text = document_version.extracted_text or ""
    prompt = build_prompt(document_version)
    schema = load_spec_claim_json_schema()
    client = client or get_client("extraction")

    raw, usage = await client.complete(prompt, schema)
    await record_llm_usage(
        session, organisation_id=organisation_id, project_id=project_id,
        role="extraction", purpose="document_extraction", usage=usage,
    )
    try:
        result = SpecClaimExtractionResult.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as e:
        raise RejectedSpecClaim(f"model output failed schema validation: {e}") from e

    created: list[SpecClaim] = []
    for item in result.claims:
        if item.evidence_span not in text:
            raise RejectedSpecClaim(
                f"evidence_span not found verbatim in document text: {item.evidence_span!r}"
            )

        claim = SpecClaim(
            document_version_id=document_version.id,
            location_code=item.location_code,
            attribute=item.attribute,
            value=item.value,
            confidence=item.confidence,
        )
        session.add(claim)
        await session.flush()  # need claim.id for the evidence row below

        span_start = text.index(item.evidence_span)
        evidence = Evidence(
            spec_claim_id=claim.id,
            channel="manual",
            sent_at=datetime.now(dt_timezone.utc),
            language="en",
            original_text=text,
            span_start=span_start,
            span_end=span_start + len(item.evidence_span),
        )
        session.add(evidence)
        await session.flush()
        created.append(claim)

    return created
