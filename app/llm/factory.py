from typing import Literal

from app.llm.client import AnthropicClient, FakeClient, ModelClient, OllamaClient
from app.llm.config import get_llm_settings

Role = Literal["extraction", "reasoning"]


def get_client(role: Role) -> ModelClient:
    """Switching environments (local Ollama <-> production Anthropic <-> CI's
    FakeClient) is purely a .env change (CUE_LLM_EXTRACTION_PROVIDER etc.) —
    callers never see a provider name, only a role. "fake" exists
    specifically so CI never has to run a real model at all — Ollama is
    scoped to the developer's own local machine only (CLAUDE.md's Models
    table), never a remote CI runner, regardless of that runner's own
    GPU/timeout budget."""
    settings = get_llm_settings()
    provider = getattr(settings, f"{role}_provider")
    model = getattr(settings, f"{role}_model")

    if provider == "ollama":
        return OllamaClient(settings.ollama_base_url, model)
    if provider == "anthropic":
        return AnthropicClient(settings.anthropic_api_key, model)
    if provider == "fake":
        return FakeClient(model)
    raise ValueError(f"unknown LLM provider: {provider!r}")
