"""`FakeClient`/`FakeEmbeddingClient` (app/llm/client.py, app/ask/embeddings.py)
— CI's own stand-in for a real model, never Ollama/Anthropic/TEI (see
FakeClient's own docstring for why). No DB, no network — pure schema-driven
response logic, the same shape tests/test_llm_factory.py already tests for
provider resolution.
"""

import json

import pytest

from app.ask.answer import _ANSWER_SCHEMA
from app.ask.embeddings import FakeEmbeddingClient
from app.ask.intent import _INTENT_SCHEMA, IntentClassification
from app.llm.client import FakeClient
from app.writeback.compose import ComposedDraft
from app.writeback.schema import COMPOSE_DRAFT_JSON_SCHEMA


@pytest.mark.asyncio
async def test_fake_client_intent_schema_detects_action_shaped_questions():
    fake = FakeClient()
    prompt = (
        "You are a gatekeeper...\n\n"
        "Message: Please chase the vendor for an update on the LED wall rental."
    )

    raw, usage = await fake.complete(prompt, _INTENT_SCHEMA)
    parsed = IntentClassification(**json.loads(raw))

    assert parsed.is_action_request is True
    assert parsed.action_summary
    assert usage.provider == "fake"


@pytest.mark.asyncio
async def test_fake_client_intent_schema_leaves_ordinary_questions_alone():
    fake = FakeClient()
    prompt = "You are a gatekeeper...\n\nMessage: What is the LED wall rental commitment about?"

    raw, _ = await fake.complete(prompt, _INTENT_SCHEMA)
    parsed = IntentClassification(**json.loads(raw))

    assert parsed.is_action_request is False
    assert parsed.action_summary is None


@pytest.mark.asyncio
async def test_fake_client_intent_schema_only_reads_the_trailing_message_field():
    """The real prompt's own static instructional text names "chase the
    vendor" as an example — a naive whole-prompt keyword search would
    always classify as an action request regardless of the real question.
    Confirms the fake only looks at the trailing `Message: ` field."""
    fake = FakeClient()
    prompt = (
        'Examples that ARE an action request: "chase the vendor for an update"...\n\n'
        "Message: What did the vendor say about pricing?"
    )

    raw, _ = await fake.complete(prompt, _INTENT_SCHEMA)
    parsed = IntentClassification(**json.loads(raw))

    assert parsed.is_action_request is False


@pytest.mark.asyncio
async def test_fake_client_answer_schema_cites_excerpts_that_share_real_words_with_the_question():
    fake = FakeClient()
    ids = ["11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"]
    prompt = (
        f"[{ids[0]}] (document_version): Location H: 2000mm x 1040mm LED wall panel.\n\n"
        f"[{ids[1]}] (evidence): Rigging safety cert confirmed on site walkthrough.\n\n"
        "Question: What size is the LED wall panel at location H?"
    )

    raw, _ = await fake.complete(prompt, _ANSWER_SCHEMA)
    parsed = json.loads(raw)

    assert parsed["has_support"] is True
    assert parsed["answer"]
    # Only the excerpt that actually shares topical words with the question
    # — never the unrelated rigging-safety one, and never an unconditional
    # "cite everything retrieved."
    assert parsed["citation_source_ids"] == [ids[0]]


@pytest.mark.asyncio
async def test_fake_client_answer_schema_refuses_when_no_excerpt_is_actually_relevant():
    """A fake embedding client (CUE_EMBED_PROVIDER=fake) always returns
    *some* semantic hit, regardless of true relevance — so "a hit exists"
    can no longer stand in for "the excerpts answer this question" the way
    it effectively could with only lexical retrieval. This is what keeps
    the no_citable_source refusal path testable at all once fake embeddings
    are in play."""
    fake = FakeClient()
    prompt = (
        "[11111111-1111-1111-1111-111111111111] (document_version): "
        "Location H: 2000mm x 1040mm LED wall panel.\n\n"
        "Question: What is the capital of France?"
    )

    raw, _ = await fake.complete(prompt, _ANSWER_SCHEMA)
    parsed = json.loads(raw)

    assert parsed["has_support"] is False
    assert parsed["answer"] is None
    assert parsed["citation_source_ids"] == []


@pytest.mark.asyncio
async def test_fake_client_compose_draft_schema_returns_a_real_closed_question():
    fake = FakeClient()
    raw, _ = await fake.complete("draft a confirmation message...", COMPOSE_DRAFT_JSON_SCHEMA)
    parsed = ComposedDraft.model_validate(json.loads(raw))

    assert parsed.question.strip().endswith("?")


@pytest.mark.asyncio
async def test_fake_client_unknown_schema_synthesizes_a_valid_stub_instead_of_crashing():
    fake = FakeClient()
    schema = {
        "type": "object",
        "properties": {
            "verdict": {"type": "boolean"},
            "note": {"type": ["string", "null"]},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["verdict", "note", "tags"],
    }

    raw, _ = await fake.complete("some prompt this fake has never seen the shape of", schema)
    parsed = json.loads(raw)

    assert parsed == {"verdict": False, "note": None, "tags": []}


@pytest.mark.asyncio
async def test_fake_embedding_client_returns_the_configured_dimension():
    client = FakeEmbeddingClient(dimension=1024)
    vectors = await client.embed(["hello", "world"])

    assert len(vectors) == 2
    assert all(len(v) == 1024 for v in vectors)


@pytest.mark.asyncio
async def test_fake_embedding_client_is_deterministic_per_text():
    client = FakeEmbeddingClient(dimension=32)
    first = await client.embed(["the same text"])
    second = await client.embed(["the same text"])

    assert first == second


@pytest.mark.asyncio
async def test_fake_embedding_client_differs_across_distinct_texts():
    client = FakeEmbeddingClient(dimension=32)
    a, b = await client.embed(["text A", "text B"])

    assert a != b


@pytest.mark.asyncio
async def test_fake_embedding_client_empty_input_returns_empty_list():
    client = FakeEmbeddingClient()
    assert await client.embed([]) == []
