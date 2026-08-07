from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class ArqSettings(BaseSettings):
    """app/foresight/worker.py's WorkerSettings connects here — the first
    Redis-protocol broker this codebase needs (CUE-Tech-Stack.md §2.4:
    "Lightweight task queue — arq (Valkey-backed)"). Local dev default
    matches docker-compose.yml's `valkey` service exactly, same
    env-driven-settings shape app/documents/config.py's StorageSettings
    already establishes for MinIO."""

    redis_host: str = "localhost"
    redis_port: int = 6379

    model_config = SettingsConfigDict(env_prefix="CUE_ARQ_", env_file=".env", extra="ignore")


@lru_cache
def get_arq_settings() -> ArqSettings:
    return ArqSettings()
