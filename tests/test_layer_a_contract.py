"""Layer A Machine API contract test — Implementation Prompt item 6's own
instruction: "Write a test that imports backend/app/capture/adapters/
whatsapp.py's actual WhatsAppAdapter class, points its session_endpoint at
your running local service, and calls fetch_backlog/send/health against it
for real. Matching the contract 'by reading both sides carefully' is not
the same guarantee as running the real Python class against the real Node
service and watching it work."

This spawns `layer-A/test/contract-server.ts` (tsx, real Node/Express, real
HTTP) as a subprocess — a real Machine API server backed by the same
FixtureConnector Layer A's own vitest suite uses (in-memory, seeded
backlog/media, no live WhatsApp account required — that's the one
genuinely human-gated step, exercised separately once a real account is
linked, not by this contract test). `WhatsAppAdapter` itself is imported
completely unmodified from its real production module.

Same skipif convention as `test_capture_adapters_live.py`: skips cleanly
(not a failure) when Node/pnpm/the layer-A checkout aren't present on this
machine; if they *are* present, the server must actually start and answer,
or the test fails for real.
"""

import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from app.capture.adapters.whatsapp import WhatsAppAdapter
from app.capture.config import WhatsAppSettings
from app.models import Channel

_LAYER_A_DIR = Path(__file__).resolve().parents[2] / "layer-A"
_NODE = shutil.which("node")
_PNPM = shutil.which("pnpm")
_LAYER_A_CONFIGURED = bool(
    _NODE
    and _PNPM
    and (_LAYER_A_DIR / "node_modules").is_dir()
    and (_LAYER_A_DIR / "test" / "contract-server.ts").is_file()
)

_TOKEN = "backend-contract-test-token"
_ACCOUNT_ID = "backend-contract-account"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def contract_server():
    if not _LAYER_A_CONFIGURED:
        pytest.skip("Node/pnpm/layer-A checkout not available — see _LAYER_A_CONFIGURED")

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        ["pnpm", "exec", "tsx", "test/contract-server.ts", str(port), _TOKEN, _ACCOUNT_ID],
        cwd=_LAYER_A_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                output = proc.stdout.read() if proc.stdout else ""
                raise RuntimeError(f"contract-server exited early (code {proc.returncode}):\n{output}")
            try:
                response = httpx.get(
                    f"{base_url}/health",
                    params={"group_id": _ACCOUNT_ID},
                    headers={"Authorization": f"Bearer {_TOKEN}"},
                    timeout=1,
                )
                if response.status_code == 200:
                    break
            except httpx.HTTPError as e:
                last_error = e
            time.sleep(0.25)
        else:
            raise RuntimeError(f"contract-server never became ready on {base_url}") from last_error

        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _channel(external_ref: str = _ACCOUNT_ID) -> Channel:
    import uuid

    return Channel(id=uuid.uuid4(), project_id=uuid.uuid4(), type="whatsapp", external_ref=external_ref, healthy=True)


@pytest.mark.asyncio
async def test_health_against_real_layer_a(contract_server):
    adapter = WhatsAppAdapter(WhatsAppSettings(session_endpoint=contract_server, api_token=_TOKEN))
    result = await adapter.health(_channel())

    assert result.healthy is True
    # riskTier travels inside `detail` since whatsapp.py's own ChannelHealthResult
    # has no dedicated risk-tier field — the real, unmodified adapter contract.
    assert result.detail["riskTier"] == "unofficial"


@pytest.mark.asyncio
async def test_fetch_backlog_against_real_layer_a(contract_server):
    adapter = WhatsAppAdapter(WhatsAppSettings(session_endpoint=contract_server, api_token=_TOKEN))
    messages = [m async for m in adapter.fetch_backlog(_channel())]

    assert len(messages) == 3
    ids = {m.external_id for m in messages}
    assert ids == {"contract-msg-1", "contract-msg-2", "contract-msg-3"}
    first = next(m for m in messages if m.external_id == "contract-msg-1")
    assert first.sender_external_id == "+6591234567"
    assert first.text == "Delivery confirmed for tomorrow"
    assert first.raw_payload_hash  # FR-CAP-11's dedup key, computed client-side by the real adapter
    assert first.media == []


@pytest.mark.asyncio
async def test_fetch_backlog_media_against_real_layer_a(contract_server):
    """`_to_raw_message`'s own media wiring — contract-msg-3 is seeded with
    a real `NormalisedMediaRef` (contract-server.ts), so this proves the
    adapter actually parses `media` off the wire into `RawCapturedMedia`
    (it silently dropped this field entirely before), not just that
    `fetch_media` can resolve a hand-typed uri in isolation (the older test
    below)."""
    adapter = WhatsAppAdapter(WhatsAppSettings(session_endpoint=contract_server, api_token=_TOKEN))
    messages = [m async for m in adapter.fetch_backlog(_channel())]

    with_media = next(m for m in messages if m.external_id == "contract-msg-3")
    assert with_media.text == "Photo of the mockup"
    assert len(with_media.media) == 1
    assert with_media.media[0].kind == "image"
    assert with_media.media[0].uri == f"{_ACCOUNT_ID}:contract-media-1"
    assert with_media.media[0].filename == "mockup.jpg"

    # Closes the loop end to end: the uri parsed off a real message is the
    # same uri fetch_media resolves to real bytes, not just a shape match.
    content = await adapter.fetch_media(_channel(), with_media.media[0].uri)
    assert content == b"contract test media bytes"


@pytest.mark.asyncio
async def test_fetch_backlog_since_filter_against_real_layer_a(contract_server):
    from datetime import datetime, timezone

    adapter = WhatsAppAdapter(WhatsAppSettings(session_endpoint=contract_server, api_token=_TOKEN))
    since = datetime.fromtimestamp(1700000050, tz=timezone.utc)
    messages = [m async for m in adapter.fetch_backlog(_channel(), since=since)]

    assert [m.external_id for m in messages] == ["contract-msg-2"]


@pytest.mark.asyncio
async def test_send_against_real_layer_a(contract_server):
    adapter = WhatsAppAdapter(WhatsAppSettings(session_endpoint=contract_server, api_token=_TOKEN))
    await adapter.send(_channel(), to_external_id="+6591234567", text="Confirmed, thanks")
    # send() raises on any HTTP error (response.raise_for_status()) — reaching
    # here is half the assertion; the debug route proves the connector really
    # received it, not just that Layer A returned 200 for an unrelated reason.

    debug = httpx.get(f"{contract_server}/__test__/sent", timeout=5)
    debug.raise_for_status()
    sent = debug.json()["sent"]
    assert {"accountId": _ACCOUNT_ID, "to": "+6591234567", "text": "Confirmed, thanks"} in sent


@pytest.mark.asyncio
async def test_fetch_media_against_real_layer_a(contract_server):
    adapter = WhatsAppAdapter(WhatsAppSettings(session_endpoint=contract_server, api_token=_TOKEN))
    uri = f"{_ACCOUNT_ID}:contract-media-1"
    content = await adapter.fetch_media(_channel(), uri)

    assert content == b"contract test media bytes"


@pytest.mark.asyncio
async def test_wrong_token_rejected_via_health(contract_server):
    # health()'s own try/except HTTPError (whatsapp.py, unmodified) swallows
    # the 401 and reports healthy=False rather than raising — real behaviour
    # of the already-built adapter, not something this contract test can or
    # should change.
    adapter = WhatsAppAdapter(WhatsAppSettings(session_endpoint=contract_server, api_token="wrong-token"))
    result = await adapter.health(_channel())
    assert result.healthy is False


@pytest.mark.asyncio
async def test_list_conversations_against_real_layer_a(contract_server):
    """'Layer B Channel Picker' prompt, item 1: WhatsAppAdapter.
    list_conversations() against the real Machine API GET /conversations —
    contract-server.ts seeds two known conversations (one group, one
    contact) via FixtureConnector.setKnownConversations, neither of them
    designated yet."""
    adapter = WhatsAppAdapter(WhatsAppSettings(session_endpoint=contract_server, api_token=_TOKEN))
    conversations = await adapter.list_conversations()

    by_jid = {c["jid"]: c for c in conversations}
    assert by_jid["120363297803566811@g.us"] == {
        "jid": "120363297803566811@g.us",
        "name": "LED Wall Install — ABC Expo",
        "kind": "group",
        "designated": False,
    }
    assert by_jid["+6591234567"]["kind"] == "contact"


@pytest.mark.asyncio
async def test_allowlist_add_grants_without_disturbing_others_against_real_layer_a(contract_server):
    """The exact bug this prompt exists to prevent: add_to_allowlist for
    one conversation must not disturb another already-designated one.
    Grants the contact first (simulating an operator's prior designation
    via Layer A's own ops console), then grants the group via the same
    Machine API path attach_channel uses, and confirms both — not just the
    new one — come back designated."""
    adapter = WhatsAppAdapter(WhatsAppSettings(session_endpoint=contract_server, api_token=_TOKEN))
    await adapter.add_to_allowlist("+6591234567")
    channel_refs = await adapter.add_to_allowlist("120363297803566811@g.us")

    assert set(channel_refs) == {"+6591234567", "120363297803566811@g.us"}

    conversations = await adapter.list_conversations()
    by_jid = {c["jid"]: c for c in conversations}
    assert by_jid["+6591234567"]["designated"] is True
    assert by_jid["120363297803566811@g.us"]["designated"] is True

    # Cleanup: leave the fixture's allowlist state as this test found it,
    # since contract_server is module-scoped and shared across every test
    # in this file.
    await adapter.remove_from_allowlist("+6591234567")
    await adapter.remove_from_allowlist("120363297803566811@g.us")


@pytest.mark.asyncio
async def test_allowlist_remove_revokes_only_the_named_conversation_against_real_layer_a(contract_server):
    adapter = WhatsAppAdapter(WhatsAppSettings(session_endpoint=contract_server, api_token=_TOKEN))
    await adapter.add_to_allowlist("+6591234567")
    await adapter.add_to_allowlist("120363297803566811@g.us")

    channel_refs = await adapter.remove_from_allowlist("120363297803566811@g.us")
    assert channel_refs == ["+6591234567"]

    conversations = await adapter.list_conversations()
    by_jid = {c["jid"]: c for c in conversations}
    assert by_jid["+6591234567"]["designated"] is True
    assert by_jid["120363297803566811@g.us"]["designated"] is False

    await adapter.remove_from_allowlist("+6591234567")


@pytest.mark.asyncio
async def test_wrong_token_rejected_via_fetch_backlog(contract_server):
    # fetch_backlog() has no such try/except — raise_for_status() propagates,
    # so this is where a 401 is actually observable as a real error.
    adapter = WhatsAppAdapter(WhatsAppSettings(session_endpoint=contract_server, api_token="wrong-token"))
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        _ = [m async for m in adapter.fetch_backlog(_channel())]
    assert exc_info.value.response.status_code == 401
