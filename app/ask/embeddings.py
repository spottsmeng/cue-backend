import hashlib
import struct
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


class FakeEmbeddingClient:
    """CI-only stand-in for a real embedding model (bge-m3 locally, TEI in
    production) — same "real models are local-machine-or-production only,
    never CI" posture app/llm/client.py's FakeClient documents for the
    reasoning/extraction roles. Deterministic, no network call: each text
    hashes to the same fixed-width vector every time (matching
    DocumentVersion.embedding/RetrievalChunk.embedding's hardcoded
    Vector(1024) column width), so inserts succeed and a run is
    reproducible — not a claim of real semantic similarity. No test against
    this fake actually needs real similarity ranking: e2e's own document-
    grounded-answer test is satisfied by lexical retrieval alone (a
    GENERATED tsvector column, unaffected by which embedding client is
    configured); this only has to not crash the embedding-sweep/embed-
    question code paths that call it.
    """

    def __init__(self, dimension: int = 1024):
        self.dimension = dimension

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector_for(text) for text in texts]

    def _vector_for(self, text: str) -> list[float]:
        values: list[float] = []
        counter = 0
        while len(values) < self.dimension:
            digest = hashlib.sha256(f"{text}:{counter}".encode()).digest()
            for offset in range(0, len(digest), 4):
                (raw,) = struct.unpack_from(">I", digest, offset)
                values.append((raw / 0xFFFFFFFF) * 2 - 1)
            counter += 1
        return values[: self.dimension]


def get_embedding_client() -> EmbeddingClient:
    """Switching environments (local Ollama <-> production TEI <-> CI's
    FakeEmbeddingClient) is purely a .env change (CUE_EMBED_PROVIDER etc.) —
    callers never see a provider name, same "role, not provider" shape
    app/llm/factory.py's get_client establishes (there's only one role
    here, so no Role literal is needed). "fake" exists so CI never has to
    run a real embedding model — same reasoning as the reasoning/extraction
    roles' own "fake" provider."""
    settings = get_embedding_settings()
    if settings.provider == "ollama":
        return OllamaEmbeddingClient(settings.ollama_base_url, settings.model)
    if settings.provider == "tei":
        if not settings.tei_base_url:
            raise ValueError("CUE_EMBED_TEI_BASE_URL is not set")
        return TEIEmbeddingClient(settings.tei_base_url)
    if settings.provider == "fake":
        return FakeEmbeddingClient(settings.dimension)
    raise ValueError(f"unknown embedding provider: {settings.provider!r}")
