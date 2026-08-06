from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class IdentitySettings(BaseSettings):
    """Auth/JWT settings, provider-pluggable — same shape as app/llm/config.py's
    LLMSettings (env-driven provider switch, no code change to move environments).

    - "local": HS256, a shared secret this deployment controls. Self-issued
      tokens (app/identity/tokens.py's mint_local_token, used by tests/dev) —
      never valid against a real IdP. This is the default so a fresh checkout
      runs without any external identity broker.
    - "oidc": RS256 verified against a real OIDC issuer's published JWKS — the
      shape Keycloak (or Zitadel) presents, per CUE-Tech-Stack.md §2.6.
      Standing up a real Keycloak/Entra ID tenant is a deployment decision,
      not something this module fabricates (CUE-PRD.md's explicit
      out-of-scope note for this build slice) — this provider is the pluggable
      slot that makes it "a config change away, not a code change" once one
      exists.
    """

    auth_provider: str = "local"  # "local" | "oidc"

    # "local" provider only. Overridden in any shared environment — the
    # default is intentionally recognisable as a dev placeholder, not a
    # secret meant to survive contact with a real deployment.
    local_jwt_secret: str = "dev-only-insecure-secret-change-me"
    local_issuer: str = "cue-local"

    # "oidc" provider only.
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    # Defaults to f"{oidc_issuer}/.well-known/openid-configuration"'s
    # advertised jwks_uri if unset — see tokens.py's OIDCJWTVerifier.
    oidc_jwks_url: str | None = None

    model_config = SettingsConfigDict(env_prefix="CUE_AUTH_", env_file=".env", extra="ignore")


@lru_cache
def get_identity_settings() -> IdentitySettings:
    return IdentitySettings()
