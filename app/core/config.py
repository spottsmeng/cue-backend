from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Database and app-level settings, sourced from environment / .env.

    Two separate DB connections, deliberately not one:

    - `database_url` (cue_app): what the running app uses for every request.
      A genuinely unprivileged Postgres role — RLS does not apply to a
      superuser or to a role with BYPASSRLS regardless of FORCE ROW LEVEL
      SECURITY, so the app must NOT connect as the schema owner.
    - `migration_database_url` (cue): what Alembic uses. Owns the schema and
      needs DDL privileges (CREATE TABLE, CREATE POLICY, ...) that cue_app
      deliberately does not have.

    See db/init/01-create-app-role.sql for how cue_app is provisioned.

    LLM provider settings (Ollama vs Anthropic, per-role model selection)
    live separately in app/llm/config.py — kept apart so the DB layer has no
    dependency on the intelligence layer.
    """

    database_url: str = "postgresql+asyncpg://cue_app:cue_app@localhost:5432/cue"
    migration_database_url: str = "postgresql+asyncpg://cue:cue@localhost:5432/cue"
    sql_echo: bool = False

    # CORS — comma-separated origins allowed to call this API from a browser
    # (the Next.js frontend, per frontend/PROGRESS.md's F0 milestone). No
    # CORSMiddleware existed at all before that milestone; a same-origin-
    # policy-respecting browser could not call this API from any origin,
    # regardless of how correct the bearer-token auth was. Default is the
    # frontend's own local dev port only — widen per-environment via
    # CUE_CORS_ORIGINS, never with a wildcard while allow_credentials=True
    # (browsers reject that combination outright, and it would defeat the
    # point of an allowlist).
    cors_origins: str = "http://localhost:3000"

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    # extra="ignore": .env is shared with docker-compose (CUE_APP_DB_PASSWORD)
    # and db/init/01-create-app-role.sql's shell environment, not just this
    # class — it should pick out its own fields, not reject unknown ones.
    model_config = SettingsConfigDict(env_prefix="CUE_", env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
