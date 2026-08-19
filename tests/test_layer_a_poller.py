"""app/layer_a/poller.py against a real Layer A admin API — spawns
layer-A/test/admin-contract-server.ts (real Node/Express, real
FixtureConnector-backed SessionManager, real reconnect/backoff timing) as a
subprocess, the Admin API sibling of test_layer_a_contract.py's own
Machine API contract test, for the same reason: "prove the polling contract
against the real caller, not by inspection." run_layer_a_poll_sweep() is
called directly (no arq broker needed — the same "also directly callable by
tests" shape app/foresight/worker.py's run_foresight_sweep establishes).

Same skipif convention as test_layer_a_contract.py: skips cleanly (not a
failure) when Node/pnpm/the layer-A checkout aren't present; if they are,
the server must actually start and answer, or the test fails for real.
"""

import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.layer_a.client import LayerAAdminClient
from app.layer_a.config import LayerAAdminSettings
from app.layer_a.models import LayerAAlert, LayerAAlertConfig, LayerAConflictEvent, LayerAHealthSnapshot
from app.layer_a.poller import _poll_one_organisation
from tests.conftest import set_org_context

_LAYER_A_DIR = Path(__file__).resolve().parents[2] / "layer-A"
_NODE = shutil.which("node")
_PNPM = shutil.which("pnpm")
_LAYER_A_CONFIGURED = bool(
    _NODE
    and _PNPM
    and (_LAYER_A_DIR / "node_modules").is_dir()
    and (_LAYER_A_DIR / "test" / "admin-contract-server.ts").is_file()
)

_USERNAME = "poller-contract-admin"
_PASSWORD = "poller-contract-secret"
_HEALTHY_ACCOUNT_ID = "poller-contract-healthy"
_CONFLICT_ACCOUNT_ID = "poller-contract-conflict"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def admin_contract_server():
    if not _LAYER_A_CONFIGURED:
        pytest.skip("Node/pnpm/layer-A checkout not available — see _LAYER_A_CONFIGURED")

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        ["pnpm", "exec", "tsx", "test/admin-contract-server.ts", str(port), _USERNAME, _PASSWORD],
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
                raise RuntimeError(f"admin-contract-server exited early (code {proc.returncode}):\n{output}")
            try:
                response = httpx.get(
                    f"{base_url}/admin/accounts", auth=(_USERNAME, _PASSWORD), timeout=1,
                )
                if response.status_code == 200:
                    break
            except httpx.HTTPError as e:
                last_error = e
            time.sleep(0.25)
        else:
            raise RuntimeError(f"admin-contract-server never became ready on {base_url}") from last_error
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


async def _enabled_config(app_session, org_id, **overrides) -> LayerAAlertConfig:
    await set_org_context(app_session, org_id)
    defaults = dict(
        enabled=True, sustained_disconnect_minutes=5,
        reconnect_attempt_threshold=2, reconnect_attempt_window_minutes=60,
        webhook_enabled=False, email_enabled=False,
    )
    defaults.update(overrides)
    config = LayerAAlertConfig(organisation_id=org_id, **defaults)
    app_session.add(config)
    await app_session.commit()
    return config


@pytest.mark.asyncio
async def test_poll_sweep_persists_live_account_snapshots(app_session, org_and_project, admin_contract_server):
    org_id, _ = org_and_project
    config = await _enabled_config(app_session, org_id)
    settings = LayerAAdminSettings(base_url=admin_contract_server, username=_USERNAME, password=_PASSWORD)
    client = LayerAAdminClient(settings)

    await set_org_context(app_session, org_id)
    await _poll_one_organisation(app_session, org_id, client)
    await app_session.commit()

    await set_org_context(app_session, org_id)
    rows = (
        await app_session.execute(
            select(LayerAHealthSnapshot).where(
                LayerAHealthSnapshot.organisation_id == org_id, LayerAHealthSnapshot.source == "poll"
            )
        )
    ).scalars().all()
    account_ids = {row.account_id for row in rows}
    assert account_ids == {_HEALTHY_ACCOUNT_ID, _CONFLICT_ACCOUNT_ID}
    healthy_row = next(r for r in rows if r.account_id == _HEALTHY_ACCOUNT_ID)
    assert healthy_row.status == "connected"
    assert healthy_row.healthy is True


@pytest.mark.asyncio
async def test_poll_sweep_backfills_transition_history_from_layer_as_ring_buffer(
    app_session, org_and_project, admin_contract_server
):
    org_id, _ = org_and_project
    await _enabled_config(app_session, org_id)
    settings = LayerAAdminSettings(base_url=admin_contract_server, username=_USERNAME, password=_PASSWORD)
    client = LayerAAdminClient(settings)

    await set_org_context(app_session, org_id)
    await _poll_one_organisation(app_session, org_id, client)
    await app_session.commit()

    await set_org_context(app_session, org_id)
    transition_rows = (
        await app_session.execute(
            select(LayerAHealthSnapshot).where(
                LayerAHealthSnapshot.organisation_id == org_id,
                LayerAHealthSnapshot.account_id == _HEALTHY_ACCOUNT_ID,
                LayerAHealthSnapshot.source == "transition",
            )
        )
    ).scalars().all()
    # Layer A's own SessionManager records at least connecting -> connected
    # for a real successful startup — real history, not fabricated by this
    # test.
    assert len(transition_rows) >= 2
    assert any(r.status == "connected" for r in transition_rows)


@pytest.mark.asyncio
async def test_poll_sweep_detects_session_conflict_from_poll_sourced_disconnect_code(
    app_session, org_and_project, admin_contract_server
):
    org_id, _ = org_and_project
    await _enabled_config(app_session, org_id)
    settings = LayerAAdminSettings(base_url=admin_contract_server, username=_USERNAME, password=_PASSWORD)
    client = LayerAAdminClient(settings)

    # First sweep establishes a "no conflict yet" baseline for this account.
    await set_org_context(app_session, org_id)
    await _poll_one_organisation(app_session, org_id, client)
    await app_session.commit()

    httpx.post(
        f"{admin_contract_server}/__test__/session-conflict-detail",
        json={"accountId": _CONFLICT_ACCOUNT_ID},
    ).raise_for_status()

    await set_org_context(app_session, org_id)
    await _poll_one_organisation(app_session, org_id, client)
    await app_session.commit()

    await set_org_context(app_session, org_id)
    alerts = (
        await app_session.execute(
            select(LayerAAlert).where(
                LayerAAlert.organisation_id == org_id,
                LayerAAlert.alert_type == "session_conflict",
                LayerAAlert.account_id == _CONFLICT_ACCOUNT_ID,
            )
        )
    ).scalars().all()
    assert len(alerts) == 1
    assert alerts[0].state == "open"
    assert alerts[0].condition_detail == {"status_code": 440}


@pytest.mark.asyncio
async def test_poll_sweep_detects_pid_lock_conflict_event(app_session, org_and_project, admin_contract_server):
    org_id, _ = org_and_project
    await _enabled_config(app_session, org_id)
    settings = LayerAAdminSettings(base_url=admin_contract_server, username=_USERNAME, password=_PASSWORD)
    client = LayerAAdminClient(settings)

    httpx.post(
        f"{admin_contract_server}/__test__/conflict", json={"refusedPid": 5001, "ownerPid": 5000}
    ).raise_for_status()

    await set_org_context(app_session, org_id)
    await _poll_one_organisation(app_session, org_id, client)
    await app_session.commit()

    await set_org_context(app_session, org_id)
    conflict_events = (
        await app_session.execute(
            select(LayerAConflictEvent).where(LayerAConflictEvent.organisation_id == org_id)
        )
    ).scalars().all()
    assert len(conflict_events) == 1
    assert conflict_events[0].refused_pid == 5001
    assert conflict_events[0].owner_pid == 5000

    alerts = (
        await app_session.execute(
            select(LayerAAlert).where(
                LayerAAlert.organisation_id == org_id,
                LayerAAlert.alert_type == "session_conflict",
                LayerAAlert.account_id.is_(None),
            )
        )
    ).scalars().all()
    assert len(alerts) == 1
    assert alerts[0].condition_detail == {"refused_pid": 5001, "owner_pid": 5000}


@pytest.mark.asyncio
async def test_poll_sweep_detects_reconnect_flapping_from_real_backoff_history(
    app_session, org_and_project, admin_contract_server
):
    org_id, _ = org_and_project
    await _enabled_config(app_session, org_id, reconnect_attempt_threshold=2, reconnect_attempt_window_minutes=60)
    settings = LayerAAdminSettings(base_url=admin_contract_server, username=_USERNAME, password=_PASSWORD)
    client = LayerAAdminClient(settings)

    httpx.post(
        f"{admin_contract_server}/__test__/force-unhealthy", json={"accountId": _CONFLICT_ACCOUNT_ID}
    ).raise_for_status()

    await set_org_context(app_session, org_id)
    await _poll_one_organisation(app_session, org_id, client)
    await app_session.commit()

    await set_org_context(app_session, org_id)
    alerts = (
        await app_session.execute(
            select(LayerAAlert).where(
                LayerAAlert.organisation_id == org_id,
                LayerAAlert.alert_type == "reconnect_flapping",
                LayerAAlert.account_id == _CONFLICT_ACCOUNT_ID,
            )
        )
    ).scalars().all()
    assert len(alerts) == 1
    assert alerts[0].condition_detail["connect_attempts_in_window"] >= 2


@pytest.mark.asyncio
async def test_poll_sweep_is_idempotent_against_a_repeated_tick(app_session, org_and_project, admin_contract_server):
    """A sweep re-run against unchanged live state must not duplicate rows
    — the ON CONFLICT DO NOTHING dedup keys (app/layer_a/poller.py) proven
    against a real Layer A response, not synthetic data."""
    org_id, _ = org_and_project
    await _enabled_config(app_session, org_id)
    settings = LayerAAdminSettings(base_url=admin_contract_server, username=_USERNAME, password=_PASSWORD)
    client = LayerAAdminClient(settings)

    await set_org_context(app_session, org_id)
    await _poll_one_organisation(app_session, org_id, client)
    await app_session.commit()

    await set_org_context(app_session, org_id)
    transition_count_first = (
        await app_session.execute(
            select(LayerAHealthSnapshot).where(
                LayerAHealthSnapshot.organisation_id == org_id, LayerAHealthSnapshot.source == "transition"
            )
        )
    ).scalars().all()

    # A second sweep tick against the same (unchanged) live state.
    await set_org_context(app_session, org_id)
    await _poll_one_organisation(app_session, org_id, client)
    await app_session.commit()

    await set_org_context(app_session, org_id)
    transition_count_second = (
        await app_session.execute(
            select(LayerAHealthSnapshot).where(
                LayerAHealthSnapshot.organisation_id == org_id, LayerAHealthSnapshot.source == "transition"
            )
        )
    ).scalars().all()

    assert len(transition_count_second) == len(transition_count_first)
