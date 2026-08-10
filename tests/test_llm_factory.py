"""No DB, no network — provider resolution is pure config logic. Codifies the
manual check from earlier ('get_client(\"extraction\") -> OllamaClient
qwen2.5:14b') plus the cases never actually exercised: switching to Anthropic
via env, and the missing-API-key failure.

get_llm_settings() is @lru_cache'd (deliberately — the real app shouldn't
re-read .env on every request), so tests must clear that cache themselves
around each monkeypatch, or they'd see whichever settings happened to be
cached first.
"""

import pytest

from app.llm.client import AnthropicClient, FakeClient, OllamaClient
from app.llm.config import get_llm_settings
from app.llm.factory import get_client


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_llm_settings.cache_clear()
    yield
    get_llm_settings.cache_clear()


def test_default_resolves_to_ollama(monkeypatch):
    monkeypatch.delenv("CUE_LLM_EXTRACTION_PROVIDER", raising=False)
    monkeypatch.delenv("CUE_LLM_EXTRACTION_MODEL", raising=False)

    client = get_client("extraction")

    assert isinstance(client, OllamaClient)
    assert client.model == "qwen2.5:14b"


def test_env_switches_extraction_to_anthropic(monkeypatch):
    monkeypatch.setenv("CUE_LLM_EXTRACTION_PROVIDER", "anthropic")
    monkeypatch.setenv("CUE_LLM_EXTRACTION_MODEL", "claude-haiku-4-5")
    monkeypatch.setenv("CUE_LLM_ANTHROPIC_API_KEY", "test-key-123")

    client = get_client("extraction")

    assert isinstance(client, AnthropicClient)
    assert client.model == "claude-haiku-4-5"
    assert client.api_key == "test-key-123"


def test_reasoning_role_is_independent_of_extraction_role(monkeypatch):
    monkeypatch.setenv("CUE_LLM_EXTRACTION_PROVIDER", "ollama")
    monkeypatch.setenv("CUE_LLM_REASONING_PROVIDER", "anthropic")
    monkeypatch.setenv("CUE_LLM_ANTHROPIC_API_KEY", "test-key-123")

    extraction_client = get_client("extraction")
    reasoning_client = get_client("reasoning")

    assert isinstance(extraction_client, OllamaClient)
    assert isinstance(reasoning_client, AnthropicClient)


def test_anthropic_without_api_key_raises(monkeypatch):
    monkeypatch.setenv("CUE_LLM_EXTRACTION_PROVIDER", "anthropic")
    monkeypatch.delenv("CUE_LLM_ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        get_client("extraction")


def test_env_switches_reasoning_to_fake(monkeypatch):
    """CI's own provider — no real model, ever (see app/llm/client.py's
    FakeClient docstring for why: Ollama is scoped to the developer's own
    local machine only, never a remote CI runner)."""
    monkeypatch.setenv("CUE_LLM_REASONING_PROVIDER", "fake")

    client = get_client("reasoning")

    assert isinstance(client, FakeClient)


def test_unknown_provider_raises(monkeypatch):
    monkeypatch.setenv("CUE_LLM_EXTRACTION_PROVIDER", "openai")

    with pytest.raises(ValueError, match="unknown LLM provider"):
        get_client("extraction")
