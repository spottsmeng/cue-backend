"""app/ask/answer.py's answer_query — FR-ASK-02's citation-or-refuse
discipline and FR-ASK-06's fake-action guard, exercised with fake embedding
+ reasoning clients (same tests/test_extractor.py FakeModelClient pattern)
so this never depends on a live Ollama/Anthropic endpoint. What's under test
is everything *around* the model calls: retrieval producing zero hits
short-circuits before the answer-generation model is even invoked, a
model-proposed citation id is verified against the real retrieval hits
before it's trusted, an action-shaped request is refused by the intent gate
before either retrieval or answer-generation ever runs (so there is no
execution path in which a fake "action taken" sentence could be produced),
and FR-ASK-08's follow-up/session handling.
"""

import json
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.ask.answer import answer_query
from app.ask.models import AskTurn
from app.ask.retrieve import RetrievalHit
from app.ledger.extractor import _get_commitment_act_term
from app.models import AskConversation, Commitment, Evidence, Project
from tests.conftest import set_org_context


class FakeEmbeddingClient:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.01] * 1024 for _ in texts]


class FakeReasoningClient:
    """answer_query makes two structurally distinct kinds of calls against
    the same ModelClient — app/ask/intent.py's classification call, always
    first, then (only for a non-action question) app/ask/answer.py's own
    answer-generation call. This fake dispatches on schema shape rather than
    returning one fixed response, so a test can control each independently;
    defaults are "ordinary question" / "no support" so a test that only
    cares about one side doesn't have to specify the other."""

    def __init__(self, *, intent_response: dict | None = None, answer_response: dict | None = None):
        self.intent_response = intent_response or {"is_action_request": False, "action_summary": None}
        self.answer_response = answer_response or {
            "has_support": False, "answer": None, "citation_source_ids": [],
        }
        self.calls: list[tuple[str, dict]] = []

    async def complete(self, prompt: str, schema: dict) -> str:
        self.calls.append((prompt, schema))
        if "is_action_request" in schema.get("properties", {}):
            return json.dumps(self.intent_response)
        return json.dumps(self.answer_response)

    @property
    def answer_calls(self) -> list[tuple[str, dict]]:
        return [(p, s) for p, s in self.calls if "citation_source_ids" in s.get("properties", {})]


class ExplodingReasoningClient:
    """For paths that must never reach *any* model call at all — currently
    only the conversation-ownership checks below, which raise LookupError
    before app/ask/intent.py's classification step is even reached. Not
    reused for "no retrieval hits" cases: the intent gate always makes one
    call before hits are known, so a client that raises unconditionally
    would be swallowed by classify_intent's own fail-open handling rather
    than proving anything — FakeReasoningClient's per-call log is the right
    tool for asserting "the answer-generation call specifically never
    happened" instead.
    """

    async def complete(self, prompt: str, schema: dict) -> str:
        raise AssertionError("reasoning client must not be called on this path at all")


async def _make_commitment_with_evidence(session, project_id, vendor, internal) -> tuple[Commitment, Evidence]:
    act_term = await _get_commitment_act_term(session, "commit")
    commitment = Commitment(
        project_id=project_id, party_id=vendor.id, counterparty_id=internal.id, act_type_id=act_term.id,
        state="committed", deliverable_en="LED screen install", confidence=0.9,
        verification_state="human_verified",
    )
    session.add(commitment)
    await session.flush()
    evidence = Evidence(
        commitment_id=commitment.id, channel="whatsapp", sent_at=datetime.now(timezone.utc),
        language="en", original_text="LED screens confirmed for install on the 24th, no price change",
    )
    session.add(evidence)
    await session.commit()
    return commitment, evidence


@pytest.mark.asyncio
async def test_no_hits_short_circuits_before_answer_generation(app_session, org_and_project, seeded_user):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()

    reasoning = FakeReasoningClient()
    result = await answer_query(
        app_session, project=project, user_id=seeded_user.id, question="what did the vendor say about pricing?",
        conversation_id=None, embedding_client=FakeEmbeddingClient(), reasoning_client=reasoning,
    )

    assert result.available is False
    assert result.answer is None
    assert result.citations == []
    assert result.refusal_kind == "no_citable_source"
    assert result.unavailable_reason is not None
    # The intent gate made its one call; the answer-generation call was
    # never reached, since there was nothing to build an answer prompt from.
    assert not reasoning.answer_calls


@pytest.mark.asyncio
async def test_action_shaped_request_is_refused_without_retrieval_or_answer_generation(
    app_session, org_and_project, seeded_user, monkeypatch
):
    """FR-ASK-06: 'do not fake an action taken response — say it can't do
    that yet.' Proven structurally, not just by the response shape: neither
    retrieval nor the answer-generation call ever runs for this request, so
    there is no execution path by which a fabricated 'I've sent a
    message...' sentence could be produced."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("hybrid_retrieve must not run for an action-shaped request")

    monkeypatch.setattr("app.ask.answer.hybrid_retrieve", fail_if_called)

    reasoning = FakeReasoningClient(
        intent_response={"is_action_request": True, "action_summary": "chase the AV vendor for a price update"}
    )
    result = await answer_query(
        app_session, project=project, user_id=seeded_user.id, question="can you chase the AV vendor about pricing?",
        conversation_id=None, embedding_client=FakeEmbeddingClient(), reasoning_client=reasoning,
    )

    assert result.available is False
    assert result.answer is None
    assert result.citations == []
    assert result.refusal_kind == "action_not_yet_supported"
    assert "chase the AV vendor for a price update" in result.unavailable_reason
    assert not reasoning.answer_calls

    turn = (
        await app_session.execute(select(AskTurn).where(AskTurn.conversation_id == result.conversation_id))
    ).scalar_one()
    assert turn.answer_available is False


@pytest.mark.asyncio
async def test_intent_classification_failure_fails_open_to_a_question(
    app_session, org_and_project, seeded_user
):
    """app/ask/intent.py's classify_intent degrades to "not an action" if
    the reasoning model can't even be reached, same NFR-AVL-03 posture
    _embed_question already has for the embedding client — proven here via
    a client whose classification call raises, confirming the request still
    proceeds to the ordinary (here: no-hits) question path rather than
    erroring out."""

    class RaisingOnIntentOnly:
        async def complete(self, prompt: str, schema: dict) -> str:
            if "is_action_request" in schema.get("properties", {}):
                raise ConnectionError("embedding/reasoning service unreachable")
            raise AssertionError("should not reach answer generation in this no-hits test")

    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()

    result = await answer_query(
        app_session, project=project, user_id=seeded_user.id, question="chase the vendor for pricing",
        conversation_id=None, embedding_client=FakeEmbeddingClient(), reasoning_client=RaisingOnIntentOnly(),
    )

    assert result.available is False
    assert result.refusal_kind == "no_citable_source"


@pytest.mark.asyncio
async def test_grounded_answer_resolves_citation_to_the_commitment(
    app_session, org_and_project, parties, seeded_user, monkeypatch
):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)
    commitment, evidence = await _make_commitment_with_evidence(app_session, project_id, vendor, internal)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()

    async def fake_hybrid_retrieve(session, *, project_id, query_text, query_embedding, limit=8):
        return [
            RetrievalHit(
                source_type="evidence", source_id=evidence.id, project_id=project_id,
                text=evidence.original_text, score=1.0,
            )
        ]

    monkeypatch.setattr("app.ask.answer.hybrid_retrieve", fake_hybrid_retrieve)

    reasoning = FakeReasoningClient(
        answer_response={
            "has_support": True, "answer": "LED screens are confirmed for install on the 24th.",
            "citation_source_ids": [str(evidence.id)],
        }
    )
    result = await answer_query(
        app_session, project=project, user_id=seeded_user.id, question="when are the LED screens going up?",
        conversation_id=None, embedding_client=FakeEmbeddingClient(), reasoning_client=reasoning,
    )

    assert result.available is True
    assert result.refusal_kind is None
    assert result.answer == "LED screens are confirmed for install on the 24th."
    assert len(result.citations) == 1
    assert result.citations[0].source_type == "commitment"
    assert result.citations[0].source_id == commitment.id

    turn = (
        await app_session.execute(select(AskTurn).where(AskTurn.conversation_id == result.conversation_id))
    ).scalar_one()
    assert turn.answer_available is True
    assert turn.citation_source_ids == [commitment.id]


@pytest.mark.asyncio
async def test_hallucinated_citation_id_is_dropped(
    app_session, org_and_project, parties, seeded_user, monkeypatch
):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)
    _commitment, evidence = await _make_commitment_with_evidence(app_session, project_id, vendor, internal)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()

    async def fake_hybrid_retrieve(session, *, project_id, query_text, query_embedding, limit=8):
        return [
            RetrievalHit(
                source_type="evidence", source_id=evidence.id, project_id=project_id,
                text=evidence.original_text, score=1.0,
            )
        ]

    monkeypatch.setattr("app.ask.answer.hybrid_retrieve", fake_hybrid_retrieve)

    # Only a fabricated id — not one of the real retrieval hits — is offered.
    reasoning = FakeReasoningClient(
        answer_response={
            "has_support": True, "answer": "made up answer", "citation_source_ids": [str(uuid.uuid4())],
        }
    )
    result = await answer_query(
        app_session, project=project, user_id=seeded_user.id, question="anything?",
        conversation_id=None, embedding_client=FakeEmbeddingClient(), reasoning_client=reasoning,
    )

    assert result.available is False
    assert result.answer is None
    assert result.citations == []
    assert result.refusal_kind == "no_citable_source"


@pytest.mark.asyncio
async def test_model_reports_no_support(app_session, org_and_project, parties, seeded_user, monkeypatch):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)
    _commitment, evidence = await _make_commitment_with_evidence(app_session, project_id, vendor, internal)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()

    async def fake_hybrid_retrieve(session, *, project_id, query_text, query_embedding, limit=8):
        return [
            RetrievalHit(
                source_type="evidence", source_id=evidence.id, project_id=project_id,
                text=evidence.original_text, score=1.0,
            )
        ]

    monkeypatch.setattr("app.ask.answer.hybrid_retrieve", fake_hybrid_retrieve)

    reasoning = FakeReasoningClient()  # defaults: not an action, has_support=False
    result = await answer_query(
        app_session, project=project, user_id=seeded_user.id, question="how much does it cost in total?",
        conversation_id=None, embedding_client=FakeEmbeddingClient(), reasoning_client=reasoning,
    )

    assert result.available is False
    assert result.answer is None
    assert result.refusal_kind == "no_citable_source"


@pytest.mark.asyncio
async def test_followup_turn_carries_prior_context_and_shares_conversation(
    app_session, org_and_project, seeded_user, monkeypatch
):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()

    first = await answer_query(
        app_session, project=project, user_id=seeded_user.id, question="what is the venue?",
        conversation_id=None, embedding_client=FakeEmbeddingClient(), reasoning_client=FakeReasoningClient(),
    )

    # A retrieval hit for the second turn only — the first turn stays a
    # genuine no-source case (proving the answer-generation call is skipped
    # for it), while the second turn actually reaches the reasoning model,
    # which is what this test needs to inspect the built prompt for
    # prior-turn context.
    async def fake_hybrid_retrieve(session, *, project_id, query_text, query_embedding, limit=8):
        return [
            RetrievalHit(
                source_type="audit_log", source_id=uuid.uuid4(), project_id=project_id,
                text="venue confirmed as Marina Bay Sands Hall B", score=1.0,
            )
        ]

    monkeypatch.setattr("app.ask.answer.hybrid_retrieve", fake_hybrid_retrieve)

    reasoning = FakeReasoningClient()
    second = await answer_query(
        app_session, project=project, user_id=seeded_user.id, question="and what about it?",
        conversation_id=first.conversation_id, embedding_client=FakeEmbeddingClient(), reasoning_client=reasoning,
    )

    assert second.conversation_id == first.conversation_id
    assert reasoning.answer_calls, "the answer-generation call should have been reached for the second turn"
    prompt, _schema = reasoning.answer_calls[0]
    assert "what is the venue?" in prompt

    turns = (
        await app_session.execute(select(AskTurn).where(AskTurn.conversation_id == first.conversation_id))
    ).scalars().all()
    assert len(turns) == 2


@pytest.mark.asyncio
async def test_unknown_conversation_id_is_rejected(app_session, org_and_project, seeded_user):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()

    with pytest.raises(LookupError):
        await answer_query(
            app_session, project=project, user_id=seeded_user.id, question="anything",
            conversation_id=uuid.uuid4(), embedding_client=FakeEmbeddingClient(),
            reasoning_client=ExplodingReasoningClient(),
        )


@pytest.mark.asyncio
async def test_conversation_cannot_be_reused_by_a_different_user(app_session, org_and_project, seeded_user):
    """FR-ASK-03's scoping applies to conversation ownership too — a
    conversation created by one user must not be adoptable by another, even
    within the same project."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()

    conversation = AskConversation(project_id=project_id, user_id=seeded_user.id)
    app_session.add(conversation)
    await app_session.commit()

    other_user_id = uuid.uuid4()
    with pytest.raises(LookupError):
        await answer_query(
            app_session, project=project, user_id=other_user_id, question="anything",
            conversation_id=conversation.id, embedding_client=FakeEmbeddingClient(),
            reasoning_client=ExplodingReasoningClient(),
        )
