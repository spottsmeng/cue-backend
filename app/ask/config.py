from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class EmbeddingSettings(BaseSettings):
    """Embeddings are a separate capability from app/llm/'s extraction/
    reasoning ModelClient — text-in, vector-out, no prompt/schema at all —
    so this is its own settings class rather than an extension of
    LLMSettings, per Prompt 9's own instruction not to couple the two. Same
    env-driven, provider-pluggable shape app/llm/config.py's LLMSettings and
    app/documents/config.py's SharePointSettings already establish.

    "ollama" (default): a local embedding model (BGE-M3, pulled the same way
    qwen2.5:14b is for extraction) served by Ollama's /api/embed — always
    available for dev/test, same posture app/llm/config.py's Ollama default
    has for extraction/reasoning.

    "tei" is the production choice CUE-Tech-Stack.md §2.4 names explicitly:
    BGE-M3 served via Hugging Face's Text Embeddings Inference. Not
    "anthropic" — Anthropic has no embeddings endpoint at all, so unlike the
    LLM roles' local-Ollama/hosted-Anthropic split, the hosted side here is a
    self-hosted HTTP service the same way SharePointSettings' "graph"
    provider is, not a third-party API.
    """

    provider: str = "ollama"  # "ollama" | "tei" | "fake"
    model: str = "bge-m3"
    # Must match DocumentVersion.embedding's Vector(1024) column
    # (app/documents/models.py) and this new module's own RetrievalChunk.
    # embedding column — BGE-M3's own output width, not independently
    # configurable per provider (both providers below serve the same model).
    dimension: int = 1024

    ollama_base_url: str = "http://localhost:11434"
    # "tei" provider only.
    tei_base_url: str | None = None

    model_config = SettingsConfigDict(env_prefix="CUE_EMBED_", env_file=".env", extra="ignore")


@lru_cache
def get_embedding_settings() -> EmbeddingSettings:
    return EmbeddingSettings()
