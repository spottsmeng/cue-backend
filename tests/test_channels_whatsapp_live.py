"""Real end-to-end test for 'Layer B Channel Picker — Implementation
Prompt.txt' item 5: a PM/admin-role user picks a real WhatsApp conversation
by name (via the new `.../channels/whatsapp/conversations` picker endpoint),
attaches it, and the resulting `Channel` row's `external_ref` is the correct
underlying JID *and* Layer A's own Level C allowlist actually flips to
`designated: true` for it — not just the `Channel` row in isolation, which
is the exact gap this whole prompt exists to close (a PM attaches a channel,
the UI reports success, and nothing is ever actually captured). Detaching
reverses both halves, confirmed the same way.

Same skipif convention as test_capture_adapters_live.py / test_layer_a_
contract.py: skips cleanly (not a failure) when CUE_WHATSAPP_SESSION_
ENDPOINT/CUE_WHATSAPP_API_TOKEN aren't configured; if they *are* configured
but Layer A is unreachable, this test fails for real, per that file's own
convention — not something to hide behind a skip.

This talks to whichever real WhatsApp account is linked to this
environment's real, running Layer A instance (as of this session, the
developer's own personal account — see layer-A/PROGRESS.md's "WhatsApp
account linking" section). Every mutation this test makes is a single
conversation's Level C allowlist entry, reversed in a `finally` block
regardless of test outcome, so a failed assertion never leaves that
account's real capture scoping changed.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.capture.adapters.whatsapp import WhatsAppAdapter
from app.capture.config import WhatsAppSettings
from main import app

_whatsapp_settings = WhatsAppSettings()
_WHATSAPP_CONFIGURED = bool(_whatsapp_settings.session_endpoint and _whatsapp_settings.api_token)

pytestmark = pytest.mark.skipif(
    not _WHATSAPP_CONFIGURED,
    reason="CUE_WHATSAPP_SESSION_ENDPOINT/CUE_WHATSAPP_API_TOKEN not configured — see _WHATSAPP_CONFIGURED",
)


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_whatsapp_channel_picker_end_to_end_against_real_layer_a(authed_org_and_project):
    """No `app_session` fixture here, deliberately — `authed_org_and_project`
    already sets this test's org context on it (is_local=False, session-
    scoped per tests/conftest.py's own `set_org_context` default); a second,
    redundant call from this test body was found (during this session) to
    leave a stray, never-committed transaction on that connection at
    fixture teardown, which then leaked a stale `app.current_org_id` onto
    the pool for whatever later test drew the same physical connection
    next — a real, reproducible RLS failure in unrelated test files
    (test_parties_compute.py) several files later in the suite. Everything
    this test needs is reachable through the real ASGI app + real Layer A
    alone."""
    org_id, project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        picker = await client.get(
            f"/projects/{project_id}/channels/whatsapp/conversations", headers=_headers(admin_token)
        )
        assert picker.status_code == 200, picker.text
        conversations = picker.json()
        assert len(conversations) > 0, (
            "expected at least one real, discovered conversation from the linked account "
            "(layer-A/PROGRESS.md's own 'Level C capture scoping' section names 94)"
        )

        # Pick a conversation that isn't already designated, so this test
        # proves the grant (attach) and the revoke (detach) actually flip
        # Layer A's own allowlist state, not just leave something already
        # true untouched.
        target = next((c for c in conversations if not c["designated"]), None)
        assert target is not None, "expected at least one non-designated conversation to test against"
        jid = target["jid"]

        channel_id = None
        try:
            attach = await client.post(
                f"/projects/{project_id}/channels",
                headers=_headers(admin_token),
                json={"type": "whatsapp", "external_ref": jid, "display_name": target["name"]},
            )
            assert attach.status_code == 201, attach.text
            body = attach.json()
            channel_id = body["id"]
            assert body["external_ref"] == jid
            assert body["display_name"] == target["name"]

            # Confirmed against Layer A directly, not just trusting the
            # local Channel row — the exact half of this flow a prior
            # session would have silently skipped.
            after_attach = await client.get(
                f"/projects/{project_id}/channels/whatsapp/conversations", headers=_headers(admin_token)
            )
            after_attach_by_jid = {c["jid"]: c for c in after_attach.json()}
            assert after_attach_by_jid[jid]["designated"] is True

            detach = await client.delete(
                f"/projects/{project_id}/channels/{channel_id}", headers=_headers(admin_token)
            )
            assert detach.status_code == 204
            channel_id = None

            after_detach = await client.get(
                f"/projects/{project_id}/channels/whatsapp/conversations", headers=_headers(admin_token)
            )
            after_detach_by_jid = {c["jid"]: c for c in after_detach.json()}
            assert after_detach_by_jid[jid]["designated"] is False
        finally:
            # Belt-and-braces: guarantee this real account's allowlist
            # state is restored even if an assertion above failed partway
            # through (e.g. the detach-side assertion, after the real
            # grant already happened).
            await WhatsAppAdapter().remove_from_allowlist(jid)
