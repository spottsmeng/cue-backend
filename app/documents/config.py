from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class StorageSettings(BaseSettings):
    """MinIO connection settings for app/documents/storage.py's
    StorageBackend Protocol — same env-driven, provider-pluggable shape
    app/llm/config.py and app/identity/config.py already establish. Local
    dev default matches docker-compose.yml's `minio` service exactly."""

    endpoint: str = "localhost:9000"
    access_key: str = "cue"
    secret_key: str = "cue_minio_secret"
    bucket: str = "cue-documents"
    secure: bool = False

    model_config = SettingsConfigDict(env_prefix="CUE_STORAGE_", env_file=".env", extra="ignore")


@lru_cache
def get_storage_settings() -> StorageSettings:
    return StorageSettings()


class SharePointSettings(BaseSettings):
    """app/documents/sharepoint.py's SharePointAdapter Protocol — same
    env-driven, provider-pluggable shape app/llm/factory.py's ModelClient
    switch already establishes (get_client("extraction") picks Ollama vs
    Anthropic purely off .env; this is the same pattern for "noop" vs
    "graph" vs "nextcloud"). Defaults to "noop" so a fresh checkout, CI, and
    the test suite never need real Microsoft 365 or Nextcloud credentials —
    FR-DOC-07's real write-back is opt-in, not required to run this
    codebase at all.

    "graph" points at a real Microsoft Graph-backed SharePoint site —
    credentials for it live in app/core/graph_auth.py's GraphSettings
    (CUE_GRAPH_*), not here, since the same Entra ID app registration also
    backs the capture-side GraphAdapter's Teams/Outlook reads once those
    exist; only the site-specific fields below stay on this settings class.
    In production, Pico's own tenant (once Pico grants an app registration
    inside it); for demo/development before that access exists, a free
    Microsoft 365 Developer Program sandbox tenant works identically.

    "nextcloud" is the FOSS substitute built ahead of that credential —
    genuinely functional against a real self-hosted or Nextcloud-provided
    instance, not a sandbox pretending to be Pico's, per this session's own
    product-strategy note (CUE-PRD.md §9.3a).
    """

    provider: str = "noop"  # "noop" | "graph" | "nextcloud"

    # "graph" provider only.
    # Either site_id directly (if already known, e.g. looked up once via
    # Graph Explorer), or hostname + site-relative-path so the adapter
    # resolves it itself on first use and caches the result.
    site_id: str | None = None
    site_hostname: str | None = None  # e.g. "yourtenant.sharepoint.com"
    site_path: str | None = None  # e.g. "/sites/CUEDemo"

    # Folder within the site's default document library CUE writes
    # canonical versions into — created on first write if it doesn't exist.
    library_folder: str = "CUE"

    # "nextcloud" provider only.
    nextcloud_base_url: str | None = None
    nextcloud_username: str | None = None
    nextcloud_app_password: str | None = None
    nextcloud_remote_folder: str = "CUE"

    model_config = SettingsConfigDict(env_prefix="CUE_SHAREPOINT_", env_file=".env", extra="ignore")


@lru_cache
def get_sharepoint_settings() -> SharePointSettings:
    return SharePointSettings()
