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

from types import SimpleNamespace

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


# --- F1 gate (Over- and Under-splitting…pdf) --------------------------------


@pytest.mark.asyncio
async def test_f1_collapse_alerts_even_when_field_accuracy_holds(
    monkeypatch, app_session, org_and_project
):
    """The blind spot this gate exists to close. Field accuracy's denominator
    is the labelled fields, so a commitment the model invented — matching no
    expected commitment at all — adds nothing to it. A model that fabricates
    an extra commitment on every message therefore scores an unchanged field
    accuracy, which is exactly the failure that destroys trust in a ledger.
    """
    monkeypatch.setitem(
        drift.EXTRACTION_BASELINES, "qwen2.5:14b",
        drift.ExtractionBaseline(accuracy_pct=81.9, f1=0.80, measured_on="test"),
    )

    async def fake_run(args, env=None):
        return {
            "overall_field_accuracy": 84.0,  # comfortably above its own gate
            "f1": 0.55, "precision": 0.42, "recall": 0.98, "spurious": 41,
            "by_band": {"easy": 84.0}, "n_cases": 20,
        }

    monkeypatch.setattr(drift, "_run_subprocess_json", fake_run)
    result = await drift.run_extraction_drift_check()

    assert result["accuracy_regressed"] is False
    assert result["f1_regressed"] is True
    assert result["regressed"] is True


@pytest.mark.asyncio
async def test_missing_f1_is_treated_as_unavailable_not_as_zero(
    monkeypatch, app_session, org_and_project
):
    """An older cue-eval revision reports no f1. Reading that absence as a
    score of zero would alert every project on a harness/app version skew."""
    monkeypatch.setitem(
        drift.EXTRACTION_BASELINES, "qwen2.5:14b",
        drift.ExtractionBaseline(accuracy_pct=81.9, f1=0.80, measured_on="test"),
    )

    async def fake_run(args, env=None):
        return {"overall_field_accuracy": 84.0, "by_band": {"easy": 84.0}, "n_cases": 10}

    monkeypatch.setattr(drift, "_run_subprocess_json", fake_run)
    result = await drift.run_extraction_drift_check()

    assert result["f1"] is None
    assert result["f1_regressed"] is False
    assert result["regressed"] is False


@pytest.mark.asyncio
async def test_a_model_with_no_recorded_baseline_reports_the_gap_instead_of_passing(
    monkeypatch, app_session, org_and_project
):
    """The bug that made this gate model-keyed.

    The baseline used to be one hardcoded pair (qwen's 81.9 / 0.93) while the
    gate ran whatever get_llm_settings() had configured. Production extraction
    is claude-haiku-4-5, which measures 90.8 / 0.961 — so the alert floor sat
    18.9 points *below* the real baseline and extraction could have collapsed
    by 21% relative while the check reported healthy.

    Keying by model fixes that pair, but leaves the question of a model with
    no measurement. Borrowing another model's yardstick is the original bug
    wearing a new name, so an unmeasured model reports `baseline_missing`
    rather than quietly returning "not regressed" — a gap in coverage is
    visible, not silently green.
    """
    monkeypatch.setattr(
        drift, "EXTRACTION_BASELINES",
        {"qwen2.5:14b": drift.ExtractionBaseline(81.9, 0.93, "test")},
    )

    real = drift.get_llm_settings()
    stub = SimpleNamespace(
        extraction_provider=real.extraction_provider,
        extraction_model="some-unmeasured-model",
        anthropic_api_key=None,
    )
    monkeypatch.setattr(drift, "get_llm_settings", lambda: stub)

    async def fake_run(args, env=None):
        return {"overall_field_accuracy": 12.0, "f1": 0.05, "n_cases": 20, "by_band": {}}

    monkeypatch.setattr(drift, "_run_subprocess_json", fake_run)
    result = await drift.run_extraction_drift_check()

    assert result["baseline_missing"] is True
    # Catastrophic scores, but not reported as a regression against a baseline
    # that does not exist — the honest answer is "unmeasured", not "failing".
    assert result["regressed"] is False
    assert result["accuracy"] == 12.0


@pytest.mark.asyncio
async def test_drift_check_runs_the_suite_five_times(monkeypatch, app_session, org_and_project):
    """CLAUDE.md: "--runs 5 before trusting a result." The gate took
    run_eval.py's default of 1, deciding whether to raise a production risk
    off a single sample by a standard this repo's own process rejects."""
    seen = {}

    async def fake_run(args, env=None):
        seen["args"] = args
        return {"overall_field_accuracy": 84.0, "f1": 0.93, "n_cases": 20, "by_band": {}}

    monkeypatch.setattr(drift, "_run_subprocess_json", fake_run)
    await drift.run_extraction_drift_check()

    assert "--runs" in seen["args"]
    assert seen["args"][seen["args"].index("--runs") + 1] == "5"
