"""app/layer_a/notification.py — webhook delivery against a genuine local
HTTP server (same real-TCP, no-mocking-library posture
tests/test_webhooks_api.py's own _CapturingWebhookServer establishes for
app/foresight/notification.py's deliver_webhook), and email delivery
against docker-compose's real greenmail SMTP/IMAP test server — no mocks
anywhere in this file, per this codebase's own testing discipline.
"""

import hashlib
import hmac
import imaplib
import json
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import pytest_asyncio

from app.layer_a.config import LayerAEmailSettings
from app.layer_a.models import LayerAAlert, LayerAAlertConfig
from app.layer_a.notification import deliver_layer_a_email, deliver_layer_a_webhook

GREENMAIL_HOST = "localhost"
GREENMAIL_SMTP_PORT = 3025
GREENMAIL_IMAP_PORT = 3143
GREENMAIL_USER = "cue"
GREENMAIL_PASSWORD = "cue_greenmail_secret"
GREENMAIL_ADDRESS = "cue@cue.test"


class _CapturingWebhookServer:
    """Same real-TCP pattern as tests/test_webhooks_api.py's own
    _CapturingWebhookServer — deliberately not shared/imported across test
    files, per this codebase's own "duplicate a small per-surface fixture
    rather than reach across files for one" convention (mirrors the
    frontend's analogous hooks.ts convention, applied here to test
    fixtures)."""

    def __init__(self):
        self.received: list[dict] = []
        self.next_status = 200
        handler = self._make_handler()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _make_handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("content-length", 0))
                body = self.rfile.read(length)
                outer.received.append({"headers": dict(self.headers), "body": body})
                self.send_response(outer.next_status)
                self.end_headers()

            def log_message(self, *args):
                pass

        return Handler

    @property
    def url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}/hook"

    def shutdown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)


@pytest_asyncio.fixture
async def webhook_server():
    server = _CapturingWebhookServer()
    yield server
    server.shutdown()


def _alert(**overrides) -> LayerAAlert:
    defaults = dict(
        id=uuid.uuid4(), organisation_id=uuid.uuid4(), alert_type="sustained_disconnect",
        account_id="acct-1", severity="serious", state="open",
        opened_at=datetime.now(timezone.utc), condition_detail={"duration_minutes": 7},
    )
    defaults.update(overrides)
    return LayerAAlert(**defaults)


def _config(**overrides) -> LayerAAlertConfig:
    defaults = dict(
        enabled=True, sustained_disconnect_minutes=5, reconnect_attempt_threshold=5,
        reconnect_attempt_window_minutes=10, webhook_enabled=False, email_enabled=False,
    )
    defaults.update(overrides)
    return LayerAAlertConfig(**defaults)


@pytest.mark.asyncio
async def test_deliver_layer_a_webhook_signs_the_payload_with_hmac_sha256(webhook_server):
    config = _config(webhook_enabled=True, webhook_url=webhook_server.url, webhook_secret="s3cret")
    alert = _alert()

    ok = await deliver_layer_a_webhook(config, alert)

    assert ok is True
    assert len(webhook_server.received) == 1
    received = webhook_server.received[0]
    expected_signature = hmac.new(b"s3cret", received["body"], hashlib.sha256).hexdigest()
    assert received["headers"]["x-cue-signature"] == expected_signature
    payload = json.loads(received["body"])
    assert payload["alert_type"] == "sustained_disconnect"
    assert payload["account_id"] == "acct-1"
    assert payload["severity"] == "serious"
    assert payload["condition_detail"] == {"duration_minutes": 7}


@pytest.mark.asyncio
async def test_deliver_layer_a_webhook_returns_false_on_failure_without_raising(webhook_server):
    webhook_server.next_status = 500
    config = _config(webhook_enabled=True, webhook_url=webhook_server.url, webhook_secret="s3cret")

    ok = await deliver_layer_a_webhook(config, _alert())

    assert ok is False


@pytest.mark.asyncio
async def test_deliver_layer_a_webhook_returns_false_when_disabled():
    config = _config(webhook_enabled=False, webhook_url="http://localhost:1/unused", webhook_secret="s3cret")
    assert await deliver_layer_a_webhook(config, _alert()) is False


@pytest.mark.asyncio
async def test_deliver_layer_a_email_delivers_via_real_smtp_and_is_readable_over_imap(monkeypatch):
    settings = LayerAEmailSettings(
        host=GREENMAIL_HOST, port=GREENMAIL_SMTP_PORT, username=GREENMAIL_USER,
        password=GREENMAIL_PASSWORD, from_address=GREENMAIL_ADDRESS, use_tls=False,
    )
    monkeypatch.setattr("app.layer_a.notification.get_layer_a_email_settings", lambda: settings)
    config = _config(email_enabled=True, email_recipients=[GREENMAIL_ADDRESS])
    alert = _alert(alert_type="session_conflict", account_id=None, condition_detail={"refused_pid": 4821, "owner_pid": 4809})

    ok = await deliver_layer_a_email(config, alert)
    assert ok is True

    conn = imaplib.IMAP4(GREENMAIL_HOST, GREENMAIL_IMAP_PORT)
    try:
        conn.login(GREENMAIL_USER, GREENMAIL_PASSWORD)
        conn.select("INBOX")
        _, data = conn.search(None, "ALL")
        message_ids = data[0].split()
        assert len(message_ids) >= 1
        _, msg_data = conn.fetch(message_ids[-1], "(RFC822)")
        raw = msg_data[0][1].decode()
        assert "CUE Layer A alert: session_conflict" in raw
        assert "refused_pid" in raw
        conn.store(",".join(m.decode() for m in message_ids), "+FLAGS", r"(\Deleted)")
        conn.expunge()
    finally:
        conn.logout()


@pytest.mark.asyncio
async def test_deliver_layer_a_email_returns_false_when_disabled(monkeypatch):
    settings = LayerAEmailSettings(
        host=GREENMAIL_HOST, port=GREENMAIL_SMTP_PORT, username=GREENMAIL_USER,
        password=GREENMAIL_PASSWORD, from_address=GREENMAIL_ADDRESS, use_tls=False,
    )
    monkeypatch.setattr("app.layer_a.notification.get_layer_a_email_settings", lambda: settings)
    config = _config(email_enabled=False, email_recipients=[GREENMAIL_ADDRESS])
    assert await deliver_layer_a_email(config, _alert()) is False


@pytest.mark.asyncio
async def test_deliver_layer_a_email_returns_false_on_unreachable_host(monkeypatch):
    settings = LayerAEmailSettings(host="127.0.0.1", port=1, username=None, password=None, from_address="cue@cue.test", use_tls=False)
    monkeypatch.setattr("app.layer_a.notification.get_layer_a_email_settings", lambda: settings)
    config = _config(email_enabled=True, email_recipients=["someone@example.test"])
    assert await deliver_layer_a_email(config, _alert()) is False
