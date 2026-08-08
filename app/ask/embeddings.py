from typing import Protocol

import httpx

from app.ask.config import get_embedding_settings

# Mirrors app/llm/client.py + app/llm/factory.py's shape exactly (env-driven
# provider switch, local option always available for dev/test) — a *new*
# Protocol, not a reuse of ModelClient, since embedding is text-in/vector-out
# with no prompt/schema at all, a genuinely different interface (Prompt 9's
# own instruction: "don't couple it to the existing extraction/reasoning
# ModelClient — embeddings are a different capability with a different
# interface").


class EmbeddingClient(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class OllamaEmbeddingClient:
    def __init__(self, base_url: str, model: str):
        self.base_url = base_url
        self.model = model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        async with httpx.AsyncClient(timeout=180) as client:
            r = await client.post(
                f"{self.base_url}/api/embed",
                json={"model": self.model, "input": texts},
            )
            r.raise_for_status()
            return r.json()["embeddings"]


class TEIEmbeddingClient:
    """Text Embeddings Inference (CUE-Tech-Stack.md §2.4's named production
    choice for serving BGE-M3) — a self-hosted HTTP service. `/embed` in, a
    bare list of vectors out (no wrapping envelope the way Ollama's response
    has one)."""

    def __init__(self, base_url: str):
        self.base_url = base_url

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        async with httpx.AsyncClient(timeout=180) as client:
            r = await client.post(f"{self.base_url}/embed", json={"inputs": texts})
            r.raise_for_status()
            return r.json()


def get_embedding_client() -> EmbeddingClient:
    """Switching environments (local Ollama <-> production TEI) is purely a
    .env change (CUE_EMBED_PROVIDER etc.) — callers never see a provider
    name, same "role, not provider" shape app/llm/factory.py's get_client
    establishes (there's only one role here, so no Role literal is needed)."""
    settings = get_embedding_settings()
    if settings.provider == "ollama":
        return OllamaEmbeddingClient(settings.ollama_base_url, settings.model)
    if settings.provider == "tei":
        if not settings.tei_base_url:
            raise ValueError("CUE_EMBED_TEI_BASE_URL is not set")
        return TEIEmbeddingClient(settings.tei_base_url)
    raise ValueError(f"unknown embedding provider: {settings.provider!r}")
