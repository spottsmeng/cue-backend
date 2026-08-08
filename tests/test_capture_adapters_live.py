"""Real-adapter integration tests — Prompt 11's own testing expectation:
"Mark real-adapter integration tests pytest.mark.skipif on the relevant
credential env var being unset — never let them silently report green
without actually exercising real infrastructure." Every test here makes a
genuine network call against the docker-compose services this session
added (`nextcloud`, `mattermost`/`mattermost_postgres`, `greenmail`) — not
mocks, not a sandbox pretending to be Pico's own tenant (WhatsApp/WeChat/
Graph stay code-complete/credential-blocked, untouched by this file).

Requires `docker compose up -d nextcloud mattermost_postgres mattermost
greenmail` to be running, and backend/.env's CUE_MATTERMOST_*/
CUE_SHAREPOINT_NEXTCLOUD_*/CUE_NEXTCLOUD_*/CUE_IMAP_SMTP_* populated with
this session's real local credentials (see PROGRESS.md's M8 notes for how
they were minted — Mattermost bot token via `mmctl`, Nextcloud app
password via `occ user:add-app-password`). Skips cleanly (not a failure) on
a machine where those services/credentials aren't present; if they *are*
configured but unreachable (e.g. containers stopped), tests fail for real
— a genuine signal, not something to hide behind a skip.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.capture.adapters.imap_smtp import ImapSmtpAdapter
from app.capture.adapters.mattermost import MattermostAdapter
from app.capture.adapters.nextcloud import NextcloudAdapter, NextcloudCaptureSettings
from app.capture.config import ImapSmtpSettings, MattermostSettings
from app.documents.config import SharePointSettings
from app.documents.models import DocumentVersion
from app.documents.sharepoint import NextcloudWriteBackAdapter
from app.models import Channel

# Credentials can live in backend/.env, not just real os.environ — and
# pydantic-settings' env_file loading reads the dotenv file directly
# without ever populating os.environ as a side effect (confirmed:
# os.getenv(...) sees none of it). The skip condition has to construct the
# actual Settings object to see what the adapters themselves would see.
_mattermost_settings = MattermostSettings()
_MATTERMOST_CONFIGURED = bool(_mattermost_settings.base_url and _mattermost_settings.bot_token)

# Write-back (SharePointSettings) and capture (NextcloudCaptureSettings)
# are architecturally separate settings blocks (app/capture/adapters/
# nextcloud.py's own docstring on why) that happen to point at the same
# local instance in this session's setup — checked independently here too.
_nextcloud_writeback_settings = SharePointSettings()
_NEXTCLOUD_WRITEBACK_CONFIGURED = bool(
    _nextcloud_writeback_settings.nextcloud_base_url and _nextcloud_writeback_settings.nextcloud_app_password
)
_nextcloud_capture_settings = NextcloudCaptureSettings()
_NEXTCLOUD_CAPTURE_CONFIGURED = bool(
    _nextcloud_capture_settings.base_url and _nextcloud_capture_settings.app_password
)

_imap_smtp_settings = ImapSmtpSettings()
_IMAP_SMTP_CONFIGURED = bool(_imap_smtp_settings.imap_host and _imap_smtp_settings.username)

# The vendor-updates channel this session's own mmctl bootstrap created
# (PROGRESS.md's M8 notes) — a fixed id, not discovered at test time, same
# as this file's other fixed test-instance identifiers (the greenmail
# mailbox, the Nextcloud CUE-Capture folder).
_MATTERMOST_TEST_CHANNEL_ID = "ttjme9pe83d19geior9qbf5k3c"


def _channel(channel_type: str, external_ref: str | None = None) -> Channel:
    return Channel(
        id=uuid.uuid4(), project_id=uuid.uuid4(), type=channel_type, external_ref=external_ref, healthy=True
    )


@pytest.mark.skipif(not _MATTERMOST_CONFIGURED, reason="CUE_MATTERMOST_BASE_URL/_BOT_TOKEN not configured")
@pytest.mark.asyncio
async def test_mattermost_send_and_fetch_backlog_round_trip():
    adapter = MattermostAdapter(MattermostSettings())
    channel = _channel("mattermost", _MATTERMOST_TEST_CHANNEL_ID)
    marker = f"live-test-{uuid.uuid4().hex[:8]}"

    await adapter.send(channel, to_external_id="", text=f"Delayed 2hrs, {marker}")

    since = datetime.now(timezone.utc) - timedelta(minutes=5)
    matched = [m async for m in adapter.fetch_backlog(channel, since=since) if marker in (m.text or "")]

    assert len(matched) == 1
    assert matched[0].sender_external_id
    assert matched[0].raw_payload_hash

    health = await adapter.health(channel)
    assert health.healthy is True


@pytest.mark.skipif(not _NEXTCLOUD_WRITEBACK_CONFIGURED, reason="CUE_SHAREPOINT_NEXTCLOUD_* not configured")
@pytest.mark.asyncio
async def test_nextcloud_write_back_creates_real_file():
    settings = SharePointSettings(provider="nextcloud")
    adapter = NextcloudWriteBackAdapter(settings)
    version = DocumentVersion(
        id=uuid.uuid4(), document_id=uuid.uuid4(), version_no=1, storage_ref="documents/live-test/v1/x",
    )
    content = f"live test content {uuid.uuid4().hex}".encode()

    await adapter.write_back(version, f"live-test-{uuid.uuid4().hex[:8]}.txt", content)
    # write_back raises on any HTTP error (response.raise_for_status()) —
    # reaching this line at all is the assertion.


@pytest.mark.skipif(not _NEXTCLOUD_CAPTURE_CONFIGURED, reason="CUE_NEXTCLOUD_* not configured")
@pytest.mark.asyncio
async def test_nextcloud_capture_sees_externally_uploaded_file():
    import httpx

    settings = NextcloudCaptureSettings()
    filename = f"live-test-{uuid.uuid4().hex[:8]}.txt"
    async with httpx.AsyncClient(auth=(settings.username, settings.app_password)) as client:
        response = await client.put(
            f"{settings.base_url}/remote.php/dav/files/{settings.username}/CUE-Capture/{filename}",
            content=b"live integration test upload",
        )
        response.raise_for_status()

    adapter = NextcloudAdapter(settings)
    channel = _channel("nextcloud", "CUE-Capture")
    since = datetime.now(timezone.utc) - timedelta(minutes=5)

    matched = [
        m async for m in adapter.fetch_backlog(channel, since=since)
        if m.media and m.media[0].filename == filename
    ]

    assert len(matched) == 1
    assert matched[0].text is None
    assert matched[0].media[0].kind == "document"

    health = await adapter.health(channel)
    assert health.healthy is True


@pytest.mark.skipif(not _IMAP_SMTP_CONFIGURED, reason="CUE_IMAP_SMTP_* not configured")
@pytest.mark.asyncio
async def test_imap_smtp_send_and_fetch_backlog_round_trip():
    adapter = ImapSmtpAdapter(ImapSmtpSettings())
    channel = _channel("imap_smtp")
    marker = f"live-test-{uuid.uuid4().hex[:8]}"

    await adapter.send(channel, to_external_id="cue@cue.test", text=f"Quote ready, {marker}")

    since = datetime.now(timezone.utc) - timedelta(minutes=5)
    matched = [m async for m in adapter.fetch_backlog(channel, since=since) if marker in (m.text or "")]

    assert len(matched) == 1
    assert matched[0].sender_external_id
    assert matched[0].raw_payload_hash

    health = await adapter.health(channel)
    assert health.healthy is True
