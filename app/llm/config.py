from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMSettings(BaseSettings):
    """Two roles, not four models. CLAUDE.md's model table has four rows, but
    only 'extraction' (routine) and 'reasoning' (consequential) are things a
    running service routes to — matching the PRD §5.3 routing diagram.
    `qwen2.5:32b` ("diagnostic only") is a manual comparison run by hand via
    cue-eval's --model flag, not a deployed role; it deliberately has no slot
    here.

    Local dev default: both roles point at Ollama. Production overrides via
    .env to point both at Anthropic — see CUE-Tech-Stack.md §2.4.
    """

    extraction_provider: str = "ollama"  # "ollama" | "anthropic" | "fake"
    extraction_model: str = "qwen2.5:14b"
    reasoning_provider: str = "ollama"
    reasoning_model: str = "qwen2.5:32b"

    ollama_base_url: str = "http://localhost:11434"
    anthropic_api_key: str | None = None

    model_config = SettingsConfigDict(env_prefix="CUE_LLM_", env_file=".env", extra="ignore")


@lru_cache
def get_llm_settings() -> LLMSettings:
    return LLMSettings()
