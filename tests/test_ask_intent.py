"""app/ask/intent.py's classify_intent in isolation — no DB, no network.
The structural guarantee app/ask/answer.py relies on (an action-shaped
request never reaches answer-generation) is only as good as this function's
own two properties: it parses a well-formed model response correctly, and
it fails open (never raises, never silently defaults to "is an action") if
the model call itself breaks.
"""

import json

import pytest

from app.ask.intent import classify_intent


class FakeReasoningClient:
    def __init__(self, response: dict):
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    async def complete(self, prompt: str, schema: dict) -> str:
        self.calls.append((prompt, schema))
        return json.dumps(self.response)


class RaisingReasoningClient:
    async def complete(self, prompt: str, schema: dict) -> str:
        raise ConnectionError("model service unreachable")


class GarbageReasoningClient:
    async def complete(self, prompt: str, schema: dict) -> str:
        return "not valid json"


@pytest.mark.asyncio
async def test_classifies_an_action_request():
    client = FakeReasoningClient({"is_action_request": True, "action_summary": "chase the vendor"})
    result = await classify_intent("please chase the vendor for an update", client)
    assert result.is_action_request is True
    assert result.action_summary == "chase the vendor"


@pytest.mark.asyncio
async def test_classifies_an_ordinary_question():
    client = FakeReasoningClient({"is_action_request": False, "action_summary": None})
    result = await classify_intent("what did the vendor say about pricing?", client)
    assert result.is_action_request is False
    assert result.action_summary is None


@pytest.mark.asyncio
async def test_fails_open_when_the_model_is_unreachable():
    result = await classify_intent("chase the vendor", RaisingReasoningClient())
    assert result.is_action_request is False


@pytest.mark.asyncio
async def test_fails_open_on_malformed_model_output():
    result = await classify_intent("chase the vendor", GarbageReasoningClient())
    assert result.is_action_request is False


@pytest.mark.asyncio
async def test_question_names_itself_in_the_prompt():
    client = FakeReasoningClient({"is_action_request": False, "action_summary": None})
    await classify_intent("what is the venue?", client)
    prompt, schema = client.calls[0]
    assert "what is the venue?" in prompt
    assert "is_action_request" in schema["properties"]
