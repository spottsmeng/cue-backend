"""app/observability/drift.py — NFR-OBS-05/FR-VOI-06's "continuous accuracy
vs. baseline" jobs. The cue-eval/asr_eval.py subprocesses themselves aren't
invoked here (slow, and asr_eval.py needs macOS `say`) — _run_subprocess_json
is monkeypatched with a canned JSON_SUMMARY-shaped dict, the same way
tests/test_capture_health.py's own unit-level tests monkeypatch get_adapter
rather than hitting a real adapter. run_capture_health_sweep/
run_foresight_sweep's own directly-callable-without-a-worker convention
(tests/test_foresight_worker.py:24-30) is reused here too.
"""

import uuid

import pytest
from sqlalchemy import select

from app.foresight.models import Risk
from app.models import Notification
from app.observability import drift
from tests.conftest import set_org_context


@pytest.mark.asyncio
async def test_extraction_drift_regression_raises_a_risk_and_notification(
    monkeypatch, app_session, authed_org_and_project
):
    org_id, project_id, admin, _token = authed_org_and_project
    await set_org_context(app_session, org_id)
    await app_session.commit()

    async def fake_run(args, **kwargs):
        return {"overall_field_accuracy": 40.0, "by_band": {"easy": 40.0}, "n_cases": 10}

    monkeypatch.setattr(drift, "_run_subprocess_json", fake_run)

    result = await drift.run_extraction_drift_check()

    assert result["ran"] is True
    assert result["regressed"] is True
    assert result["projects_notified"] >= 1

    risk = (
        await app_session.execute(
            select(Risk).where(Risk.project_id == project_id, Risk.source == "model_drift")
        )
    ).scalar_one()
    assert risk.status == "open"
    assert "40.0%" in risk.downstream_consequence

    notification = (
        await app_session.execute(
            select(Notification).where(Notification.risk_id == risk.id)
        )
    ).scalar_one()
    assert notification.severity == "high"


@pytest.mark.asyncio
async def test_extraction_drift_no_regression_raises_nothing(
    monkeypatch, app_session, authed_org_and_project
):
    org_id, project_id, admin, _token = authed_org_and_project
    await set_org_context(app_session, org_id)
    await app_session.commit()

    async def fake_run(args, **kwargs):
        return {"overall_field_accuracy": 84.0, "by_band": {"easy": 84.0}, "n_cases": 10}

    monkeypatch.setattr(drift, "_run_subprocess_json", fake_run)

    result = await drift.run_extraction_drift_check()

    assert result["ran"] is True
    assert result["regressed"] is False
    assert "projects_notified" not in result

    risks = (
        await app_session.execute(select(Risk).where(Risk.project_id == project_id))
    ).scalars().all()
    assert risks == []


@pytest.mark.asyncio
async def test_extraction_drift_infra_failure_does_not_raise_a_risk(monkeypatch, app_session):
    async def fake_run(args, **kwargs):
        raise RuntimeError("ollama unreachable")

    monkeypatch.setattr(drift, "_run_subprocess_json", fake_run)

    result = await drift.run_extraction_drift_check()

    assert result == {"ran": False}


@pytest.mark.asyncio
async def test_asr_drift_regression_raises_a_risk(monkeypatch, app_session, authed_org_and_project):
    org_id, project_id, admin, _token = authed_org_and_project
    await set_org_context(app_session, org_id)
    await app_session.commit()

    async def fake_run(args, **kwargs):
        return {"wer": 0.5, "cer": 0.6, "n_cases": 6}

    monkeypatch.setattr(drift, "_run_subprocess_json", fake_run)

    result = await drift.run_asr_drift_check()

    assert result["ran"] is True
    assert result["regressed"] is True

    risk = (
        await app_session.execute(
            select(Risk).where(
                Risk.project_id == project_id, Risk.source == "model_drift",
                Risk.finding_key == "model_drift:asr:faster_whisper_tiny",
            )
        )
    ).scalar_one()
    assert risk.severity == "medium"


@pytest.mark.asyncio
async def test_repeated_unchanged_regression_does_not_duplicate_the_risk(
    monkeypatch, app_session, authed_org_and_project
):
    """create_or_supersede_risk's own dedup (app/foresight/risk.py) — a
    sustained regression shouldn't create a fresh Risk (and cascading
    Notification) on every single scheduled tick."""
    org_id, project_id, admin, _token = authed_org_and_project
    await set_org_context(app_session, org_id)
    await app_session.commit()

    async def fake_run(args, **kwargs):
        return {"overall_field_accuracy": 40.0, "by_band": {}, "n_cases": 10}

    monkeypatch.setattr(drift, "_run_subprocess_json", fake_run)

    await drift.run_extraction_drift_check()
    await drift.run_extraction_drift_check()

    risks = (
        await app_session.execute(
            select(Risk).where(Risk.project_id == project_id, Risk.source == "model_drift")
        )
    ).scalars().all()
    assert len(risks) == 1
