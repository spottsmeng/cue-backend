"""FR-ASK-01/02/03/08: natural-language question in, an answer with real
citations out — or the typed 'no source' variant, never prose pretending to
answer (CUE-PRD.md §12.5: "If no source supports an answer, say so — never
assert unsupported," quoted in Prompt 9 as governing this module's design,
not just its prompt wording). Same "verified in code, not trusted"
discipline CLAUDE.md sets for extraction evidence spans: the reasoning model
proposes which retrieved excerpts it used, but every one of those ids is
checked against the retrieval hits actually returned before it's allowed
into the response — a hallucinated or invented id is silently dropped, not
passed through.
"""

import json
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ask.embeddings import EmbeddingClient
from app.ask.intent import classify_intent
from app.ask.models import AskConversation, AskTurn
from app.ask.retrieve import RetrievalHit, hybrid_retrieve
from app.ask.schema import AskAnswerOut, Citation
from app.documents.models import SpecClaim
from app.llm.client import ModelClient
from app.models import Evidence, Project

logger = logging.getLogger("app.ask.answer")

# FR-ASK-08: the most recent turns on the same conversation, folded into the
# prompt so a follow-up question can resolve a pronoun like "it" against
# what was just discussed. Bounded, not the whole conversation history — a
# long-running session shouldn't grow the prompt without limit.
_MAX_FOLLOWUP_TURNS = 3

_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "has_support": {"type": "boolean"},
        "answer": {"type": ["string", "null"]},
        "citation_source_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["has_support", "answer", "citation_source_ids"],
}


def _hit_block(hit: RetrievalHit) -> str:
    return f"[{hit.source_id}] ({hit.source_type}): {hit.text[:1500]}"


def _build_prompt(question: str, hits: list[RetrievalHit], prior_turns: list[AskTurn]) -> str:
    context = "\n\n".join(_hit_block(h) for h in hits)
    history = ""
    if prior_turns:
        lines = [
            f"Q: {t.question}\nA: {t.answer_text or '(no source was found for this question)'}"
            for t in reversed(prior_turns)  # oldest first
        ]
        history = (
            "Previous turns in this conversation (use only to resolve references like "
            '"it" or "that vendor" in the new question — do not treat them as new source '
            "material):\n" + "\n\n".join(lines) + "\n\n"
        )
    return (
        "You are answering a project-management question using ONLY the numbered source "
        "excerpts below, each tagged with its source id in square brackets. If the excerpts "
        "do not support a confident, fully-grounded answer, set has_support to false and "
        "leave answer null — never guess, and never use outside/world knowledge. Every claim "
        "in your answer must be backed by at least one excerpt; list the id of every excerpt "
        "you relied on in citation_source_ids, exactly as given. Answer in the same language "
        "as the question.\n\n"
        f"{history}"
        f"Source excerpts:\n{context}\n\n"
        f"Question: {question}"
    )


async def _resolve_citation(session: AsyncSession, hit: RetrievalHit) -> Citation:
    """Every citation must resolve to a real row (FR-ASK-02) — for a
    document_version/audit_log hit that's just the row retrieve.py already
    found; for an evidence hit, resolved one level further to whichever of
    the five subjects the evidence table's own CHECK constraint guarantees
    is set (app/models/ledger.py's Evidence docstring), since that's the
    more useful thing for a PM to open than the raw captured-message row."""
    if hit.source_type in ("document_version", "audit_log"):
        return Citation(source_type=hit.source_type, source_id=hit.source_id, snippet=hit.text[:300])
    if hit.source_type == "evidence":
        evidence = (
            await session.execute(select(Evidence).where(Evidence.id == hit.source_id))
        ).scalar_one()
        if evidence.commitment_id is not None:
            return Citation(source_type="commitment", source_id=evidence.commitment_id, snippet=hit.text[:300])
        if evidence.budget_id is not None:
            return Citation(source_type="budget", source_id=evidence.budget_id, snippet=hit.text[:300])
        if evidence.document_version_id is not None:
            return Citation(
                source_type="document_version", source_id=evidence.document_version_id, snippet=hit.text[:300]
            )
        if evidence.spec_claim_id is not None:
            claim = (
                await session.execute(select(SpecClaim).where(SpecClaim.id == evidence.spec_claim_id))
            ).scalar_one()
            return Citation(
                source_type="document_version", source_id=claim.document_version_id, snippet=hit.text[:300]
            )
        if evidence.deviation_id is not None:
            return Citation(source_type="deviation", source_id=evidence.deviation_id, snippet=hit.text[:300])
    raise ValueError(f"unresolvable retrieval hit source_type: {hit.source_type!r}")


async def _get_or_create_conversation(
    session: AsyncSession, *, project: Project, user_id: uuid.UUID, conversation_id: uuid.UUID | None
) -> AskConversation:
    if conversation_id is None:
        conversation = AskConversation(project_id=project.id, user_id=user_id)
        session.add(conversation)
        await session.flush()
        return conversation
    conversation = (
        await session.execute(
            select(AskConversation).where(
                AskConversation.id == conversation_id,
                AskConversation.project_id == project.id,
                AskConversation.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if conversation is None:
        # FR-ASK-03 applies to conversation ownership too — a conversation id
        # from another user or project is never silently adopted; it's 404
        # the way a nonexistent one would be, not a 403 that would confirm
        # its existence to the wrong caller.
        raise LookupError(f"conversation {conversation_id} not found for this user/project")
    return conversation


async def _embed_question(embedding_client: EmbeddingClient, question: str) -> list[float] | None:
    try:
        vectors = await embedding_client.embed([question])
    except Exception:  # noqa: BLE001 — any failure of an external embedding service
        logger.warning("embedding the query failed; degrading to lexical-only retrieval", exc_info=True)
        return None
    return vectors[0] if vectors else None


def _action_unavailable_reason(action_summary: str | None) -> str:
    described = (action_summary or "take that action").strip() or "take that action"
    return (
        f"CUE can't {described} yet — outbound actions (messaging a vendor or client, confirming a "
        "date, rescheduling with them) need Write-back, which hasn't been built. Ask again as a "
        "question about what's already on record and I can help with that."
    )


async def answer_query(
    session: AsyncSession,
    *,
    project: Project,
    user_id: uuid.UUID,
    question: str,
    conversation_id: uuid.UUID | None,
    embedding_client: EmbeddingClient,
    reasoning_client: ModelClient,
) -> AskAnswerOut:
    conversation = await _get_or_create_conversation(
        session, project=project, user_id=user_id, conversation_id=conversation_id
    )
    prior_turns = list(
        (
            await session.execute(
                select(AskTurn)
                .where(AskTurn.conversation_id == conversation.id)
                .order_by(AskTurn.asked_at.desc())
                .limit(_MAX_FOLLOWUP_TURNS)
            )
        )
        .scalars()
        .all()
    )

    # FR-ASK-06's structural gate, ahead of anything else: an action-shaped
    # request never reaches retrieval or the answer-generation call below —
    # there is no execution path in which the model that writes `answer`
    # could narrate a fabricated "I've sent a message...", because that call
    # is simply never made for this request. See app/ask/intent.py's module
    # docstring for why this has to be a separate call, not an instruction
    # folded into the answer-generation prompt.
    intent = await classify_intent(question, reasoning_client)

    if intent.is_action_request:
        result = AskAnswerOut(
            available=False,
            answer=None,
            unavailable_reason=_action_unavailable_reason(intent.action_summary),
            refusal_kind="action_not_yet_supported",
            citations=[],
            conversation_id=conversation.id,
        )
    else:
        query_embedding = await _embed_question(embedding_client, question)
        hits = await hybrid_retrieve(
            session, project_id=project.id, query_text=question, query_embedding=query_embedding
        )

        if not hits:
            result = AskAnswerOut(
                available=False,
                answer=None,
                unavailable_reason=(
                    "no source in this project's ledger, documents or communications matches this question"
                ),
                refusal_kind="no_citable_source",
                citations=[],
                conversation_id=conversation.id,
            )
        else:
            prompt = _build_prompt(question, hits, prior_turns)
            raw = await reasoning_client.complete(prompt, _ANSWER_SCHEMA)
            parsed = json.loads(raw)
            hit_by_id = {str(h.source_id): h for h in hits}
            valid_hits = [hit_by_id[i] for i in parsed.get("citation_source_ids", []) if i in hit_by_id]

            if not parsed.get("has_support") or not parsed.get("answer") or not valid_hits:
                result = AskAnswerOut(
                    available=False,
                    answer=None,
                    unavailable_reason=(
                        "the retrieved sources do not support a confident answer to this question"
                    ),
                    refusal_kind="no_citable_source",
                    citations=[],
                    conversation_id=conversation.id,
                )
            else:
                citations: list[Citation] = []
                seen: set[tuple[str, uuid.UUID]] = set()
                for hit in valid_hits:
                    citation = await _resolve_citation(session, hit)
                    key = (citation.source_type, citation.source_id)
                    if key not in seen:
                        seen.add(key)
                        citations.append(citation)
                result = AskAnswerOut(
                    available=True, answer=parsed["answer"], citations=citations,
                    conversation_id=conversation.id,
                )

    session.add(
        AskTurn(
            conversation_id=conversation.id,
            project_id=project.id,
            question=question,
            answer_available=result.available,
            answer_text=result.answer,
            citation_source_types=[c.source_type for c in result.citations],
            citation_source_ids=[c.source_id for c in result.citations],
            asked_by=user_id,
        )
    )
    await session.flush()
    return result
