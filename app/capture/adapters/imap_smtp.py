import asyncio
import email
import email.utils
import imaplib
import logging
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import AsyncIterator

from app.capture.adapters.errors import CaptureConfigError
from app.capture.config import ImapSmtpSettings, get_imap_smtp_settings
from app.capture.schema import ChannelHealthResult, RawCapturedMessage, compute_payload_hash
from app.models import Channel

logger = logging.getLogger("cue.capture.imap_smtp")

# imaplib has no async IDLE support in the standard library — polling the
# same IMAP SEARCH fetch_backlog uses is a deliberate simplification for
# stream(), same posture as MattermostAdapter's poll loop, named here
# rather than silently passed off as push-based.
_POLL_INTERVAL_SECONDS = 30


class ImapSmtpAdapter:
    """The email capability's FOSS substitute for Outlook (CUE-PRD.md
    §9.3a) — plain IMAP/SMTP against `channel.external_ref` as the mailbox
    address, so this adapter never changes even if the backing mail server
    does (a self-hosted server, or any real inbox with IMAP enabled). The
    IMAP/SMTP clients are synchronous (stdlib) — every call is pushed
    through asyncio.to_thread, the same pattern app/documents/storage.py
    uses for the synchronous minio SDK, so it doesn't block the event loop.
    """

    def __init__(self, settings: ImapSmtpSettings | None = None):
        settings = settings or get_imap_smtp_settings()
        if not (settings.imap_host and settings.smtp_host and settings.username and settings.password):
            raise CaptureConfigError(
                "channel_types code 'imap_smtp' requires CUE_IMAP_SMTP_IMAP_HOST, _SMTP_HOST, "
                "_USERNAME and _PASSWORD"
            )
        self._settings = settings

    def _imap_connect(self) -> imaplib.IMAP4:
        s = self._settings
        # imap_use_ssl=False only for a plain local test server (e.g.
        # docker-compose's greenmail — no TLS on its plain IMAP port);
        # real mail infrastructure always uses implicit TLS here.
        if s.imap_use_ssl:
            return imaplib.IMAP4_SSL(s.imap_host, s.imap_port)
        return imaplib.IMAP4(s.imap_host, s.imap_port)

    def _fetch_backlog_sync(self, since: datetime | None) -> list[RawCapturedMessage]:
        s = self._settings
        with self._imap_connect() as conn:
            conn.login(s.username, s.password)
            conn.select(s.mailbox)
            criteria = "ALL"
            if since is not None:
                criteria = f'(SINCE "{since.strftime("%d-%b-%Y")}")'
            status, data = conn.search(None, criteria)
            if status != "OK":
                raise RuntimeError(f"IMAP SEARCH failed: {status}")
            messages: list[RawCapturedMessage] = []
            for uid in data[0].split():
                status, msg_data = conn.fetch(uid, "(RFC822)")
                if status != "OK" or not msg_data or msg_data[0] is None:
                    continue
                raw_bytes = msg_data[0][1]
                parsed = email.message_from_bytes(raw_bytes)
                sent_at = _parse_date(parsed) or datetime.now(timezone.utc)
                if since is not None and sent_at <= since:
                    continue
                messages.append(
                    RawCapturedMessage(
                        external_id=parsed.get("Message-ID", uid.decode()),
                        sender_external_id=email.utils.parseaddr(parsed.get("From", ""))[1],
                        sent_at=sent_at,
                        text=_extract_body(parsed),
                        raw_payload_hash=compute_payload_hash(
                            "imap_smtp", parsed.get("Message-ID", uid.decode()), raw_bytes
                        ),
                    )
                )
            return messages

    async def fetch_backlog(
        self, channel: Channel, since: datetime | None = None
    ) -> AsyncIterator[RawCapturedMessage]:
        for message in await asyncio.to_thread(self._fetch_backlog_sync, since):
            yield message

    async def stream(self, channel: Channel) -> AsyncIterator[RawCapturedMessage]:
        last_seen = datetime.now(timezone.utc)
        while True:
            async for message in self.fetch_backlog(channel, since=last_seen):
                last_seen = max(last_seen, message.sent_at)
                yield message
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)

    def _send_sync(self, to_external_id: str, text: str) -> None:
        s = self._settings
        msg = EmailMessage()
        msg["From"] = s.from_address or s.username
        msg["To"] = to_external_id
        msg["Subject"] = "CUE"
        msg.set_content(text)
        with smtplib.SMTP(s.smtp_host, s.smtp_port) as conn:
            # smtp_use_starttls=False only for a plain local test server
            # (see imap_use_ssl above) — real submission (port 587) always
            # upgrades via STARTTLS.
            if s.smtp_use_starttls:
                conn.starttls()
            # initial_response_ok=False: the RFC 4954 "initial response"
            # optimization is ambiguous for the LOGIN mechanism specifically
            # (unlike PLAIN) — some real servers, and confirmed greenmail,
            # reject the combined "AUTH LOGIN <base64>" form outright.
            # Costs one extra round trip, safe against every server.
            conn.login(s.username, s.password, initial_response_ok=False)
            conn.send_message(msg)

    async def send(self, channel: Channel, to_external_id: str, text: str) -> None:
        await asyncio.to_thread(self._send_sync, to_external_id, text)

    def _health_sync(self) -> None:
        s = self._settings
        with self._imap_connect() as conn:
            conn.login(s.username, s.password)
            conn.select(s.mailbox)

    async def health(self, channel: Channel) -> ChannelHealthResult:
        try:
            await asyncio.to_thread(self._health_sync)
            return ChannelHealthResult(healthy=True)
        except (imaplib.IMAP4.error, OSError) as e:
            logger.warning("imap_smtp health check failed for channel=%s: %s", channel.id, e)
            return ChannelHealthResult(healthy=False, detail={"error": str(e)})


def _parse_date(parsed: email.message.Message) -> datetime | None:
    date_header = parsed.get("Date")
    if not date_header:
        return None
    parsed_date = email.utils.parsedate_to_datetime(date_header)
    if parsed_date.tzinfo is None:
        parsed_date = parsed_date.replace(tzinfo=timezone.utc)
    return parsed_date


def _extract_body(parsed: email.message.Message) -> str | None:
    if parsed.is_multipart():
        for part in parsed.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        return None
    payload = parsed.get_payload(decode=True)
    if not payload:
        return None
    return payload.decode(parsed.get_content_charset() or "utf-8", errors="replace")
