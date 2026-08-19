"""app/layer_a/alerts.py — the three alert-evaluation algorithms, against
directly-seeded LayerAHealthSnapshot/LayerAConflictEvent rows (no HTTP
needed here, per this codebase's own "sub-scan independently testable"
posture — see app/foresight/worker.py's run_project_sweep docstring for the
same reasoning applied to Foresight's own sub-scans).
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.layer_a.alerts import evaluate_alerts
from app.layer_a.models import LayerAAlert, LayerAAlertConfig, LayerAHealthSnapshot
from tests.conftest import set_org_context

def _now() -> datetime:
    # Computed fresh at each call site, not once at module import — the
    # evaluator under test compares against its own live datetime.now(), and
    # a full-suite run can take minutes between this module's import and an
    # individual test's execution, which would otherwise silently drift a
    # snapshot dated "9 minutes ago" outside a 10-minute window by the time
    # the test actually runs.
    return datetime.now(timezone.utc)


def _config(**overrides) -> LayerAAlertConfig:
    defaults = dict(
        enabled=True,
        sustained_disconnect_minutes=5,
        reconnect_attempt_threshold=5,
        reconnect_attempt_window_minutes=10,
        webhook_enabled=False,
        email_enabled=False,
    )
    defaults.update(overrides)
    return LayerAAlertConfig(**defaults)


async def _add_snapshot(
    app_session, org_id, account_id, *, source, status, recorded_at, **extra
) -> None:
    app_session.add(
        LayerAHealthSnapshot(
            organisation_id=org_id,
            account_id=account_id,
            source=source,
            recorded_at=recorded_at,
            status=status,
            connect_attempts=extra.pop("connect_attempts", 0),
            **extra,
        )
    )
    await app_session.flush()


async def _open_alerts(app_session, org_id) -> list[LayerAAlert]:
    rows = (
        await app_session.execute(
            select(LayerAAlert).where(LayerAAlert.organisation_id == org_id, LayerAAlert.state == "open")
        )
    ).scalars().all()
    return list(rows)


@pytest.mark.asyncio
async def test_sustained_disconnect_opens_once_threshold_crossed(app_session, org_and_project):
    org_id, _ = org_and_project
    await set_org_context(app_session, org_id)
    await _add_snapshot(
        app_session, org_id, "acct-1", source="poll", status="disconnected",
        recorded_at=_now() - timedelta(minutes=10),
    )
    await app_session.commit()

    await set_org_context(app_session, org_id)
    await evaluate_alerts(
        app_session, org_id, _config(),
        account_ids=["acct-1"], newly_conflicted_accounts=[], new_conflict_events=[],
    )
    await app_session.commit()

    open_alerts = await _open_alerts(app_session, org_id)
    assert len(open_alerts) == 1
    assert open_alerts[0].alert_type == "sustained_disconnect"
    assert open_alerts[0].account_id == "acct-1"
    assert open_alerts[0].severity == "serious"
    assert open_alerts[0].condition_detail["duration_minutes"] > 5


@pytest.mark.asyncio
async def test_sustained_disconnect_does_not_open_before_threshold(app_session, org_and_project):
    org_id, _ = org_and_project
    await set_org_context(app_session, org_id)
    await _add_snapshot(
        app_session, org_id, "acct-1", source="poll", status="disconnected",
        recorded_at=_now() - timedelta(minutes=2),
    )
    await app_session.commit()

    await set_org_context(app_session, org_id)
    await evaluate_alerts(
        app_session, org_id, _config(sustained_disconnect_minutes=5),
        account_ids=["acct-1"], newly_conflicted_accounts=[], new_conflict_events=[],
    )
    await app_session.commit()

    assert await _open_alerts(app_session, org_id) == []


@pytest.mark.asyncio
async def test_sustained_disconnect_resolves_on_recovery(app_session, org_and_project):
    org_id, _ = org_and_project
    await set_org_context(app_session, org_id)
    await _add_snapshot(
        app_session, org_id, "acct-1", source="poll", status="disconnected",
        recorded_at=_now() - timedelta(minutes=10),
    )
    await app_session.commit()
    await set_org_context(app_session, org_id)
    await evaluate_alerts(
        app_session, org_id, _config(),
        account_ids=["acct-1"], newly_conflicted_accounts=[], new_conflict_events=[],
    )
    await app_session.commit()
    assert len(await _open_alerts(app_session, org_id)) == 1

    await set_org_context(app_session, org_id)
    await _add_snapshot(
        app_session, org_id, "acct-1", source="poll", status="connected", recorded_at=_now(),
    )
    await app_session.commit()
    await set_org_context(app_session, org_id)
    await evaluate_alerts(
        app_session, org_id, _config(),
        account_ids=["acct-1"], newly_conflicted_accounts=[], new_conflict_events=[],
    )
    await app_session.commit()

    assert await _open_alerts(app_session, org_id) == []
    resolved = (
        await app_session.execute(
            select(LayerAAlert).where(
                LayerAAlert.organisation_id == org_id, LayerAAlert.alert_type == "sustained_disconnect"
            )
        )
    ).scalar_one()
    assert resolved.state == "resolved"
    assert resolved.resolved_at is not None


@pytest.mark.asyncio
async def test_reconnect_flapping_opens_when_attempt_count_crosses_threshold(app_session, org_and_project):
    org_id, _ = org_and_project
    await set_org_context(app_session, org_id)
    for i in range(6):
        await _add_snapshot(
            app_session, org_id, "acct-1", source="transition", status="reconnecting",
            recorded_at=_now() - timedelta(minutes=9) + timedelta(minutes=i),
            connect_attempts=i + 1,
        )
    # A connected row in between shouldn't be counted as an attempt, and its
    # presence confirms the count is a status filter, not a row count.
    await _add_snapshot(
        app_session, org_id, "acct-1", source="transition", status="connected",
        recorded_at=_now() - timedelta(minutes=1),
    )
    await app_session.commit()

    await set_org_context(app_session, org_id)
    await evaluate_alerts(
        app_session, org_id, _config(reconnect_attempt_threshold=5, reconnect_attempt_window_minutes=10),
        account_ids=["acct-1"], newly_conflicted_accounts=[], new_conflict_events=[],
    )
    await app_session.commit()

    open_alerts = await _open_alerts(app_session, org_id)
    assert len(open_alerts) == 1
    assert open_alerts[0].alert_type == "reconnect_flapping"
    assert open_alerts[0].condition_detail["connect_attempts_in_window"] == 6


@pytest.mark.asyncio
async def test_reconnect_flapping_does_not_open_below_threshold(app_session, org_and_project):
    org_id, _ = org_and_project
    await set_org_context(app_session, org_id)
    # A "connected" row anchors this account as currently connected, so
    # only reconnect_flapping (not sustained_disconnect, which only fires
    # while *currently* disconnected) is under test here.
    for i in range(2):
        await _add_snapshot(
            app_session, org_id, "acct-1", source="transition", status="reconnecting",
            recorded_at=_now() - timedelta(minutes=2) + timedelta(seconds=i), connect_attempts=i + 1,
        )
    await _add_snapshot(
        app_session, org_id, "acct-1", source="transition", status="connected", recorded_at=_now(),
    )
    await app_session.commit()

    await set_org_context(app_session, org_id)
    await evaluate_alerts(
        app_session, org_id, _config(reconnect_attempt_threshold=5, reconnect_attempt_window_minutes=10),
        account_ids=["acct-1"], newly_conflicted_accounts=[], new_conflict_events=[],
    )
    await app_session.commit()

    assert await _open_alerts(app_session, org_id) == []


@pytest.mark.asyncio
async def test_session_conflict_opens_for_poll_sourced_disconnect_status_code(app_session, org_and_project):
    org_id, _ = org_and_project
    await set_org_context(app_session, org_id)
    await evaluate_alerts(
        app_session, org_id, _config(),
        account_ids=["acct-1"], newly_conflicted_accounts=["acct-1"], new_conflict_events=[],
    )
    await app_session.commit()

    open_alerts = await _open_alerts(app_session, org_id)
    assert len(open_alerts) == 1
    assert open_alerts[0].alert_type == "session_conflict"
    assert open_alerts[0].account_id == "acct-1"
    assert open_alerts[0].severity == "critical"
    assert open_alerts[0].condition_detail == {"status_code": 440}


@pytest.mark.asyncio
async def test_session_conflict_opens_for_pid_lock_refusal_event(app_session, org_and_project):
    org_id, _ = org_and_project
    await set_org_context(app_session, org_id)
    await evaluate_alerts(
        app_session, org_id, _config(),
        account_ids=[], newly_conflicted_accounts=[],
        new_conflict_events=[{"refusedPid": 4821, "ownerPid": 4809}],
    )
    await app_session.commit()

    open_alerts = await _open_alerts(app_session, org_id)
    assert len(open_alerts) == 1
    assert open_alerts[0].alert_type == "session_conflict"
    assert open_alerts[0].account_id is None
    assert open_alerts[0].condition_detail == {"refused_pid": 4821, "owner_pid": 4809}


@pytest.mark.asyncio
async def test_session_conflict_pid_lock_events_do_not_double_open(app_session, org_and_project):
    """The partial unique index's nulls_not_distinct — two account_id=NULL
    session_conflict events for the same org must collapse to one open
    alert, not two (Postgres's default "every NULL is distinct" semantics
    would otherwise let this slip through)."""
    org_id, _ = org_and_project
    await set_org_context(app_session, org_id)
    await evaluate_alerts(
        app_session, org_id, _config(),
        account_ids=[], newly_conflicted_accounts=[],
        new_conflict_events=[{"refusedPid": 111, "ownerPid": 222}],
    )
    await app_session.commit()

    await set_org_context(app_session, org_id)
    await evaluate_alerts(
        app_session, org_id, _config(),
        account_ids=[], newly_conflicted_accounts=[],
        new_conflict_events=[{"refusedPid": 333, "ownerPid": 444}],
    )
    await app_session.commit()

    open_alerts = await _open_alerts(app_session, org_id)
    assert len(open_alerts) == 1
    # First-write-wins under ON CONFLICT DO NOTHING — the second event's
    # detail never overwrites the first's, which is correct: the alert
    # already exists and stays open until acknowledged.
    assert open_alerts[0].condition_detail == {"refused_pid": 111, "owner_pid": 222}


@pytest.mark.asyncio
async def test_evaluate_alerts_is_idempotent_across_repeated_sweeps(app_session, org_and_project):
    org_id, _ = org_and_project
    await set_org_context(app_session, org_id)
    await _add_snapshot(
        app_session, org_id, "acct-1", source="poll", status="disconnected",
        recorded_at=_now() - timedelta(minutes=10),
    )
    await app_session.commit()

    for _ in range(3):
        await set_org_context(app_session, org_id)
        await evaluate_alerts(
            app_session, org_id, _config(),
            account_ids=["acct-1"], newly_conflicted_accounts=[], new_conflict_events=[],
        )
        await app_session.commit()

    assert len(await _open_alerts(app_session, org_id)) == 1


@pytest.mark.asyncio
async def test_evaluate_alerts_isolates_by_organisation(app_session, org_and_project):
    """RLS proof, not application-level filtering — the query issued under
    the *other* org's session context carries no organisation_id predicate
    of its own at all, so an empty result here can only come from RLS
    itself silently excluding org_id's row, not from this test's own SQL
    happening to exclude it."""
    org_id, _ = org_and_project
    other_org_id = uuid.uuid4()
    from app.models import Organisation

    await set_org_context(app_session, other_org_id)
    app_session.add(Organisation(id=other_org_id, name="Other Org"))
    await app_session.commit()

    await set_org_context(app_session, org_id)
    await _add_snapshot(
        app_session, org_id, "acct-1", source="poll", status="disconnected",
        recorded_at=_now() - timedelta(minutes=10),
    )
    await app_session.commit()

    await set_org_context(app_session, org_id)
    await evaluate_alerts(
        app_session, org_id, _config(),
        account_ids=["acct-1"], newly_conflicted_accounts=[], new_conflict_events=[],
    )
    await app_session.commit()

    await set_org_context(app_session, org_id)
    org_visible = (await app_session.execute(select(LayerAAlert).where(LayerAAlert.state == "open"))).scalars().all()
    assert len(org_visible) == 1

    await set_org_context(app_session, other_org_id)
    other_org_visible = (
        await app_session.execute(select(LayerAAlert).where(LayerAAlert.state == "open"))
    ).scalars().all()
    assert other_org_visible == []
