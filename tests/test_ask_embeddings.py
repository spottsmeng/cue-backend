"""No DB, no network — provider resolution is pure config logic. Mirrors
tests/test_llm_factory.py exactly, for app/ask/embeddings.py's own
env-driven provider switch (Ollama local vs. TEI hosted).
"""

import pytest

from app.ask.config import get_embedding_settings
from app.ask.embeddings import OllamaEmbeddingClient, TEIEmbeddingClient, get_embedding_client


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_embedding_settings.cache_clear()
    yield
    get_embedding_settings.cache_clear()


def test_default_resolves_to_ollama(monkeypatch):
    monkeypatch.delenv("CUE_EMBED_PROVIDER", raising=False)
    monkeypatch.delenv("CUE_EMBED_MODEL", raising=False)

    client = get_embedding_client()

    assert isinstance(client, OllamaEmbeddingClient)
    assert client.model == "bge-m3"


def test_env_switches_to_tei(monkeypatch):
    monkeypatch.setenv("CUE_EMBED_PROVIDER", "tei")
    monkeypatch.setenv("CUE_EMBED_TEI_BASE_URL", "http://tei.internal:8080")

    client = get_embedding_client()

    assert isinstance(client, TEIEmbeddingClient)
    assert client.base_url == "http://tei.internal:8080"


def test_tei_without_base_url_raises(monkeypatch):
    monkeypatch.setenv("CUE_EMBED_PROVIDER", "tei")
    monkeypatch.delenv("CUE_EMBED_TEI_BASE_URL", raising=False)

    with pytest.raises(ValueError, match="CUE_EMBED_TEI_BASE_URL"):
        get_embedding_client()


def test_unknown_provider_raises(monkeypatch):
    monkeypatch.setenv("CUE_EMBED_PROVIDER", "openai")

    with pytest.raises(ValueError, match="unknown embedding provider"):
        get_embedding_client()
