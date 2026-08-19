from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class LayerAAdminSettings(BaseSettings):
    """Credentials for the CUE backend to call Layer A's admin API
    (HTTP Basic, layer-A/src/api/admin/index.ts) — held only here,
    server-side, never returned to any frontend caller (see app/layer_a/
    client.py and app/api/layer_a_admin.py). All three optional: the
    poller (app/layer_a/poller.py) no-ops cleanly and logs when unset,
    same defensive shape WhatsAppSettings' own credential-blocked posture
    establishes — a fresh checkout/CI never needs a live Layer A."""

    base_url: str | None = None
    username: str | None = None
    password: str | None = None

    model_config = SettingsConfigDict(env_prefix="CUE_LAYERA_ADMIN_", env_file=".env", extra="ignore")


@lru_cache
def get_layer_a_admin_settings() -> LayerAAdminSettings:
    return LayerAAdminSettings()


class LayerAEmailSettings(BaseSettings):
    """Outbound SMTP for Layer A alert delivery (app/layer_a/notification.py)
    — genuinely new infrastructure, confirmed nothing general-purpose
    existed before this (app/capture/adapters/imap_smtp.py's send() is a
    capture-reply adapter, not an alerting primitive). Same stdlib-smtplib-
    via-asyncio.to_thread shape as that adapter, not a new async SMTP
    dependency."""

    host: str | None = None
    port: int = 587
    username: str | None = None
    password: str | None = None
    from_address: str | None = None
    use_tls: bool = True

    model_config = SettingsConfigDict(env_prefix="CUE_LAYERA_SMTP_", env_file=".env", extra="ignore")


@lru_cache
def get_layer_a_email_settings() -> LayerAEmailSettings:
    return LayerAEmailSettings()
