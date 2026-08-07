"""REST endpoints for /projects/{id}/webhooks (item 8's subscription
endpoint) and app/foresight/notification.py's deliver_webhook/
deliver_due_notifications — the one real notification delivery adapter this
session builds. RLS and role-gating tested as two independent properties.
"""

import hashlib
import hmac
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.foresight.models import Notification, Risk, WebhookSubscription
from app.foresight.notification import deliver_due_notifications, deliver_webhook
from app.identity.config import get_identity_settings
from app.identity.models import Membership, User
from app.models import Project
from main import app
from tests.conftest import mint_token, set_org_context


class _CapturingWebhookServer:
    """A genuine local HTTP server (real TCP, real HTTP, no mocking
    library) — same "real infrastructure over mocks" posture
    tests/conftest.py's own module docstring establishes for the database,
    applied here to app/foresight/notification.py's deliver_webhook, which
    makes a real outbound httpx call with no injectable transport."""

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
                pass  # keep test output quiet

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


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _member(app_session, org_id, project_id, role, granted_by):
    await set_org_context(app_session, org_id)
    subject = f"{role}-{uuid.uuid4()}"
    user = User(
        organisation_id=org_id, issuer=get_identity_settings().local_issuer,
        external_subject=subject, email=f"{subject}@example.test",
    )
    app_session.add(user)
    await app_session.flush()
    app_session.add(Membership(user_id=user.id, project_id=project_id, role=role, granted_by=granted_by))
    await app_session.commit()
    return user, mint_token(org_id, subject=subject, email=user.email)


@pytest.mark.asyncio
async def test_create_webhook_returns_secret_once(authed_org_and_project):
    org_id, project_id, user, token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            f"/projects/{project_id}/webhooks",
            headers=_headers(token),
            json={"url": "https://example.test/hook", "event_types": ["risk", "deviation"]},
        )
        listing = await client.get(f"/projects/{project_id}/webhooks", headers=_headers(token))

    assert created.status_code == 201, created.text
    assert "secret" in created.json() and len(created.json()["secret"]) > 0
    assert created.json()["created_by"] == str(user.id)

    assert listing.status_code == 200
    assert "secret" not in listing.json()[0]  # never re-exposed after creation


@pytest.mark.asyncio
async def test_delete_webhook(authed_org_and_project):
    org_id, project_id, _user, token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            f"/projects/{project_id}/webhooks",
            headers=_headers(token),
            json={"url": "https://example.test/hook", "event_types": ["commitment"]},
        )
        subscription_id = created.json()["id"]

        deleted = await client.delete(
            f"/projects/{project_id}/webhooks/{subscription_id}", headers=_headers(token)
        )
        listing = await client.get(f"/projects/{project_id}/webhooks", headers=_headers(token))

    assert deleted.status_code == 204
    assert listing.json() == []


@pytest.mark.asyncio
async def test_read_only_member_can_list_but_not_create(app_session, authed_org_and_project):
    org_id, project_id, admin, _admin_token = authed_org_and_project
    _viewer, viewer_token = await _member(app_session, org_id, project_id, "read_only", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        listing = await client.get(f"/projects/{project_id}/webhooks", headers=_headers(viewer_token))
        create = await client.post(
            f"/projects/{project_id}/webhooks",
            headers=_headers(viewer_token),
            json={"url": "https://example.test/hook", "event_types": ["risk"]},
        )
    assert listing.status_code == 200
    assert create.status_code == 403


@pytest.mark.asyncio
async def test_webhooks_are_isolated_via_project_join_rls(app_session, authed_org_and_project):
    org_id, project_id, _user, token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            f"/projects/{project_id}/webhooks",
            headers=_headers(token),
            json={"url": "https://example.test/hook", "event_types": ["risk"]},
        )
    subscription_id = created.json()["id"]

    await set_org_context(app_session, uuid.uuid4())  # a different, unrelated org
    result = await app_session.execute(
        select(WebhookSubscription).where(WebhookSubscription.id == uuid.UUID(subscription_id))
    )
    assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_deliver_webhook_signs_the_payload_with_hmac_sha256(webhook_server):
    subscription = WebhookSubscription(
        id=uuid.uuid4(), project_id=uuid.uuid4(), url=webhook_server.url,
        event_types=["risk"], secret="test-secret", created_by=uuid.uuid4(),
    )
    payload = {"event": "risk", "severity": "high"}

    ok = await deliver_webhook(subscription, payload)

    assert ok is True
    assert len(webhook_server.received) == 1
    received = webhook_server.received[0]
    expected_signature = hmac.new(
        b"test-secret", json.dumps(payload, sort_keys=True).encode(), hashlib.sha256
    ).hexdigest()
    assert received["headers"]["x-cue-signature"] == expected_signature
    assert json.loads(received["body"]) == payload


@pytest.mark.asyncio
async def test_deliver_webhook_returns_false_on_failure_without_raising(webhook_server):
    webhook_server.next_status = 500
    subscription = WebhookSubscription(
        id=uuid.uuid4(), project_id=uuid.uuid4(), url=webhook_server.url,
        event_types=["risk"], secret="test-secret", created_by=uuid.uuid4(),
    )

    ok = await deliver_webhook(subscription, {"event": "risk"})
    assert ok is False


@pytest.mark.asyncio
async def test_deliver_due_notifications_marks_sent_only_on_success(app_session, org_and_project, webhook_server):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()

    risk = Risk(
        project_id=project_id, source="silence", finding_key="silence:z", severity="high",
        status="open", downstream_consequence="test",
    )
    app_session.add(risk)
    await app_session.flush()
    recipient = User(
        organisation_id=org_id, issuer=get_identity_settings().local_issuer,
        external_subject=f"recipient-{uuid.uuid4()}", email=f"recipient-{uuid.uuid4()}@example.test",
    )
    app_session.add(recipient)
    await app_session.flush()
    notification = Notification(
        project_id=project_id, recipient_id=recipient.id, risk_id=risk.id, severity="high",
        downstream_consequence="test", deliverable_at=risk.created_at,
    )
    app_session.add(notification)
    app_session.add(
        WebhookSubscription(
            project_id=project_id, url=webhook_server.url, event_types=["risk"],
            secret="s", created_by=recipient.id,
        )
    )
    await app_session.commit()

    delivered = await deliver_due_notifications(app_session, project)
    await app_session.commit()

    assert len(webhook_server.received) == 1
    assert len(delivered) == 1
    await app_session.refresh(notification)
    assert notification.sent_at is not None
    assert notification.delivered_via == "webhook"
