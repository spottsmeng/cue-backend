from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class CaptureSettings(BaseSettings):
    """One global switch for the whole capture package (app/capture/
    adapters/registry.py), not a per-channel provider switch the way
    app/documents/config.py's SharePointSettings.provider is: Prompt 11
    requires FixtureAdapter to be the uniform dev/test backend across every
    channel_types code so the full test suite/CI stays credential-free — a
    per-code switch would let one code accidentally default to "live" and
    break that guarantee. `CUE_CAPTURE_BACKEND=live` does not itself grant
    access to anything; each live adapter still independently validates its
    own credentials at construction and fails loudly if they're missing."""

    backend: str = "fixture"  # "fixture" | "live"

    model_config = SettingsConfigDict(env_prefix="CUE_CAPTURE_", env_file=".env", extra="ignore")


@lru_cache
def get_capture_settings() -> CaptureSettings:
    return CaptureSettings()


class WhatsAppSettings(BaseSettings):
    """PRD §9.1: the companion-session/MDM-handset route, the only route to
    reading consumer group chats. Credential-blocked in this environment
    (Prompt 11's own stated scope) — code-complete, not live-tested."""

    session_endpoint: str | None = None
    api_token: str | None = None

    model_config = SettingsConfigDict(env_prefix="CUE_WHATSAPP_", env_file=".env", extra="ignore")


@lru_cache
def get_whatsapp_settings() -> WhatsAppSettings:
    return WhatsAppSettings()


class WeChatWorkSettings(BaseSettings):
    """PRD §9.2: WeChat Work's Session Archiving API (会话存档). Credential-
    blocked in this environment — code-complete, not live-tested."""

    corp_id: str | None = None
    corp_secret: str | None = None
    session_archive_secret: str | None = None

    model_config = SettingsConfigDict(env_prefix="CUE_WECHAT_", env_file=".env", extra="ignore")


@lru_cache
def get_wechat_work_settings() -> WeChatWorkSettings:
    return WeChatWorkSettings()


class MattermostSettings(BaseSettings):
    """The team_collaboration capability's FOSS substitute for Microsoft
    Teams, built now against a real self-hosted Mattermost instance
    (CUE-PRD.md §9.3a) — not credential-blocked, genuinely live-testable."""

    base_url: str | None = None
    bot_token: str | None = None

    model_config = SettingsConfigDict(env_prefix="CUE_MATTERMOST_", env_file=".env", extra="ignore")


@lru_cache
def get_mattermost_settings() -> MattermostSettings:
    return MattermostSettings()


class ImapSmtpSettings(BaseSettings):
    """The email capability's FOSS substitute for Outlook — a protocol, not
    a vendor, so this adapter never changes even if the backing mail server
    does (CUE-PRD.md §9.3a)."""

    imap_host: str | None = None
    imap_port: int = 993
    smtp_host: str | None = None
    smtp_port: int = 587
    username: str | None = None
    password: str | None = None
    # The AUTH username and the mailbox's own address aren't always the
    # same string — most real IMAP/SMTP servers use the full email address
    # for both, but this isn't universal (confirmed against docker-
    # compose's greenmail: AUTH LOGIN/PLAIN only accepts the bare local
    # part `cue`, while the mailbox address is `cue@cue.test`). Falls back
    # to `username` when unset, which covers the common case.
    from_address: str | None = None
    mailbox: str = "INBOX"
    # Secure by default (implicit TLS / STARTTLS) — real mail infrastructure
    # (Outlook, a self-hosted production server) both requires and expects
    # this. Only a plain local test server (e.g. docker-compose's greenmail,
    # which has no TLS support on its plain SMTP/IMAP ports) should ever set
    # these false — never in anything resembling production.
    imap_use_ssl: bool = True
    smtp_use_starttls: bool = True

    model_config = SettingsConfigDict(env_prefix="CUE_IMAP_SMTP_", env_file=".env", extra="ignore")


@lru_cache
def get_imap_smtp_settings() -> ImapSmtpSettings:
    return ImapSmtpSettings()
