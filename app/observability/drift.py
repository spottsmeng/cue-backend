"""NFR-OBS-05 (drift detection) + FR-VOI-06 (ASR WER/CER, measured "the
same way extraction gets measured against cue-eval's cases", per Prompt
13's own explicit instruction not to build a second, unrelated measurement
mechanism for the second capability).

Both jobs below: run the relevant cue-eval harness as a subprocess (
cue-eval/run_eval.py --json / cue-eval/asr_eval.py --json), compare the
result against a documented baseline, and on regression raise (or
supersede) a Risk — source="model_drift" (app/foresight/models.py's
RiskSource, extended by migration 0a76bb463d69) — then dispatch_event fans
that out through the existing Notification pipeline (collapsing,
quiet-hours, webhook delivery — all "for free", nothing new to build).

"model_drift" is a deliberately new RiskSource, not a reuse of
app/documents/drift.py's own "drift" (source="contradiction") — that
module's drift is a circulated file differing from its approved
DocumentVersion by hash, an unrelated FR-DOC-09 concept; reusing its name
or source value would collide two genuinely different findings.

A model-accuracy regression is a platform-wide condition (every project
extracting through the same pipeline is affected), but this codebase has
no platform-level Risk/Notification concept anywhere — Silence Radar,
contradiction detection, forecasting and capture-health are all
project-scoped. Rather than invent a one-off "global Risk" exception,
these jobs raise/supersede a Risk for *every active project*
(_discover_active_projects below, the same RLS-bypass discovery pattern
app/foresight/worker.py already uses, for the same reason: no
service-account/agent identity exists yet). create_or_supersede_risk's own
per-(project, source, finding_key) dedup means a sustained regression
doesn't re-notify every single day/month — only the first detection and
any material change (severity escalating) does.

`_discover_active_projects` is a deliberate duplicate of
app/foresight/worker.py's private helper of the same name, not an import
from it — app/foresight/worker.py imports this module (to register these
jobs on its cron schedule), so importing back would be circular. Small
enough (a dozen lines) that a shared-module extraction isn't worth it for
one duplicate.
"""

import asyncio
import json
import logging
import os
import sys
import uuid
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.core.db import async_session_factory
from app.foresight.notification import dispatch_event
from app.foresight.risk import create_or_supersede_risk
from app.llm.config import get_llm_settings
from app.models import Project
from app.observability.otel import get_tracer

logger = logging.getLogger("cue.observability.drift")

CUE_EVAL_DIR = Path(__file__).resolve().parents[2] / "cue-eval"

# CLAUDE.md's own recorded baseline (qwen2.5:14b via Ollama, 20 Aug 2026,
# --runs 5 over the full 20-case suite). CLAUDE.md's "never cite a
# local-model accuracy figure outside this repo" governs *external* use of
# this number — comparing against it internally, to detect a regression
# against this repo's own recorded state, is exactly what that baseline is
# for.
#
# Was 83.3 (6 Aug, ten cases). Re-measured rather than carried forward: the
# suite has since gained the singlish, internal-channel-vendor,
# consequence-discussion and merged-vendor bands, so the old figure was no
# longer a number this suite could produce — leaving it in place meant the
# gate was calibrated against a corpus that no longer exists.
EXTRACTION_ACCURACY_BASELINE_PCT = 81.9
EXTRACTION_ACCURACY_REGRESSION_THRESHOLD_PCT = 10.0  # points below baseline before alerting

# F1 over the returned commitment *set*, the second half of what cue-eval now
# reports (run_eval.py's score()). Gating on field accuracy alone was a real
# blind spot rather than a conservative choice: that number's denominator is
# the labelled fields, so a returned commitment matching nothing at all adds
# nothing to it — a model that invents an extra commitment on every message
# scores an unchanged field accuracy and would never have tripped this check.
# Inventing commitments is the failure mode that most directly destroys trust
# in the ledger, so it gets its own gate.
EXTRACTION_F1_BASELINE = 0.93  # measured, qwen2.5:14b, 20 Aug 2026, --runs 5
EXTRACTION_F1_REGRESSION_THRESHOLD = 0.10  # absolute F1 below baseline before alerting

# PRD §8.1 targets.
ASR_WER_TARGET = 0.08
ASR_CER_TARGET = 0.10


async def _discover_active_projects() -> list[tuple[uuid.UUID, uuid.UUID]]:
    engine = create_async_engine(get_settings().migration_database_url)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(text("SELECT organisation_id, id FROM projects WHERE archived_at IS NULL"))
            ).all()
            return [(row.organisation_id, row.id) for row in rows]
    finally:
        await engine.dispose()


async def _run_subprocess_json(args: list[str], *, env: dict | None = None, timeout: float = 1800) -> dict:
    """Runs a cue-eval script as a subprocess and parses its `JSON_SUMMARY:`
    line. Raises on any failure (non-zero exit, timeout, no summary line) —
    callers treat "the eval couldn't run at all" as a job-level failure to
    log, not evidence of a model regression to alert on."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, *args, cwd=str(CUE_EVAL_DIR), env=env or dict(os.environ),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{args[0]} exited {proc.returncode}: {stderr.decode(errors='replace')[-2000:]}"
        )
    for line in stdout.decode(errors="replace").splitlines():
        if line.startswith("JSON_SUMMARY:"):
            return json.loads(line[len("JSON_SUMMARY:"):])
    raise RuntimeError(f"{args[0]} produced no JSON_SUMMARY line")


async def _notify_all_projects(
    *, source: str, finding_key: str, severity: str, downstream_consequence: str, detail: dict
) -> int:
    targets = await _discover_active_projects()
    notified = 0
    for organisation_id, project_id in targets:
        async with async_session_factory() as session:
            # is_local=true (transaction-scoped, not session-scoped) —
            # unlike app/foresight/worker.py's own _set_org_context, this
            # is one unit of work (one project, one commit) per session,
            # the same shape app/capture/health.py's run_capture_health_sweep
            # already uses true for: the setting resets at COMMIT/ROLLBACK,
            # so it can never survive on a connection returned to the pool
            # and leak into an unrelated later session/org — foresight/
            # worker.py needs false specifically because it does several
            # sequential commits per project within one session; this
            # function doesn't.
            await session.execute(
                text("SELECT set_config('app.current_org_id', :oid, true)"), {"oid": str(organisation_id)}
            )
            project = (
                await session.execute(select(Project).where(Project.id == project_id))
            ).scalar_one_or_none()
            if project is None:
                continue
            try:
                risk, created = await create_or_supersede_risk(
                    session, project_id=project_id, source=source, finding_key=finding_key,
                    severity=severity, downstream_consequence=downstream_consequence, detail=detail,
                )
                if created:
                    await dispatch_event(session, project=project, event_type="risk", risk=risk)
                await session.commit()
                notified += 1
            except Exception:
                await session.rollback()
                logger.exception("model-drift notification failed for project %s", project_id)
    return notified


async def run_extraction_drift_check(ctx: dict | None = None) -> dict:
    """Arq cron job body — also directly callable without a live worker
    (same convention as app/foresight/worker.py's run_foresight_sweep).

    Derives provider/model from get_llm_settings() at call time, not
    hardcoded to Anthropic — today that's ollama/qwen2.5:14b by default, so
    this runs at zero Anthropic cost until production config actually
    switches at go-live (this repo's own "zero Anthropic spend until
    go-live" posture), and automatically starts covering the real
    production model the day that config flips, no code change needed.
    """
    tracer = get_tracer("cue.observability.drift")
    with tracer.start_as_current_span("drift.extraction_accuracy_check"):
        settings = get_llm_settings()
        provider, model = settings.extraction_provider, settings.extraction_model

        run_args = [
            str(CUE_EVAL_DIR / "run_eval.py"), "--provider", provider, "--model", model, "--json",
        ]
        env = dict(os.environ)
        if provider == "anthropic" and settings.anthropic_api_key:
            # run_eval.py's own env var name (ANTHROPIC_API_KEY), distinct
            # from the app's CUE_LLM_ANTHROPIC_API_KEY.
            env["ANTHROPIC_API_KEY"] = settings.anthropic_api_key

        try:
            summary = await _run_subprocess_json(run_args, env=env)
        except Exception:
            logger.exception("extraction drift check could not run cue-eval")
            return {"ran": False}

        accuracy = summary["overall_field_accuracy"]
        # Older cue-eval revisions did not report f1; treat its absence as
        # "this signal is unavailable", never as a score of zero, which would
        # alert every project on a harness/app version skew.
        f1 = summary.get("f1")
        accuracy_regressed = accuracy < (
            EXTRACTION_ACCURACY_BASELINE_PCT - EXTRACTION_ACCURACY_REGRESSION_THRESHOLD_PCT
        )
        f1_regressed = (
            f1 is not None
            and EXTRACTION_F1_BASELINE > 0
            and f1 < (EXTRACTION_F1_BASELINE - EXTRACTION_F1_REGRESSION_THRESHOLD)
        )
        regressed = accuracy_regressed or f1_regressed
        result = {
            "ran": True, "provider": provider, "model": model, "accuracy": accuracy,
            "f1": f1, "precision": summary.get("precision"), "recall": summary.get("recall"),
            "spurious": summary.get("spurious"),
            "regressed": regressed,
            "accuracy_regressed": accuracy_regressed, "f1_regressed": f1_regressed,
        }

        if regressed:
            if f1_regressed and not accuracy_regressed:
                measured = (
                    f"Extraction F1 for {provider}/{model} measured {f1:.3f} against cue-eval's "
                    f"labelled corpus, down from a {EXTRACTION_F1_BASELINE:.3f} baseline, while "
                    f"field accuracy held at {accuracy:.1f}% — the signature of the model "
                    "returning commitments nobody made rather than filling existing ones in wrongly"
                )
            else:
                measured = (
                    f"Extraction accuracy for {provider}/{model} measured {accuracy:.1f}% against "
                    f"cue-eval's labelled corpus, down from an "
                    f"{EXTRACTION_ACCURACY_BASELINE_PCT:.1f}% baseline"
                )
            result["projects_notified"] = await _notify_all_projects(
                source="model_drift",
                finding_key=f"model_drift:extraction:{provider}:{model}",
                severity="high",
                downstream_consequence=(
                    f"{measured} — commitments extracted from vendor messages during this period "
                    "may be less reliable (wrong dates, amounts, missed commitments, or commitments "
                    "that were never made) until this is investigated."
                ),
                detail={
                    "provider": provider, "model": model, "accuracy": accuracy,
                    "baseline": EXTRACTION_ACCURACY_BASELINE_PCT,
                    "f1": f1, "f1_baseline": EXTRACTION_F1_BASELINE,
                    "precision": summary.get("precision"), "recall": summary.get("recall"),
                    "spurious": summary.get("spurious"), "missed": summary.get("missed"),
                    "by_band": summary.get("by_band"), "n_cases": summary.get("n_cases"),
                },
            )
        return result


async def run_asr_drift_check(ctx: dict | None = None) -> dict:
    """FR-VOI-06, monthly cadence (app/foresight/worker.py's cron_jobs) —
    same mechanism as run_extraction_drift_check above, applied to
    cue-eval/asr_eval.py's WER/CER measurement of FasterWhisperClient (the
    only real, installed ASRClient) against the small synthetic labelled
    set in cue-eval/asr_cases.json. Skips cleanly (ran=False, not a
    regression alert) on a non-macOS host, where asr_eval.py can't
    synthesize its own labelled audio — see that script's own docstring.
    """
    tracer = get_tracer("cue.observability.drift")
    with tracer.start_as_current_span("drift.asr_check"):
        try:
            summary = await _run_subprocess_json([str(CUE_EVAL_DIR / "asr_eval.py"), "--json"])
        except Exception:
            logger.exception("ASR drift check could not run cue-eval/asr_eval.py")
            return {"ran": False}

        wer, cer = summary.get("wer"), summary.get("cer")
        regressed = (wer is not None and wer > ASR_WER_TARGET) or (cer is not None and cer > ASR_CER_TARGET)
        result = {"ran": True, "wer": wer, "cer": cer, "regressed": regressed}

        if regressed:
            result["projects_notified"] = await _notify_all_projects(
                source="model_drift",
                finding_key="model_drift:asr:faster_whisper_tiny",
                severity="medium",
                downstream_consequence=(
                    f"Voice-note transcription (FasterWhisperClient, PRD §8.1 targets: WER<=8%, "
                    f"CER<=10%) measured WER={wer if wer is not None else 'n/a'}, "
                    f"CER={cer if cer is not None else 'n/a'} against the held-out labelled set — "
                    "voice-note commitments during this period may carry more transcription errors "
                    "than usual until this is investigated."
                ),
                detail={"wer": wer, "cer": cer, "n_cases": summary.get("n_cases")},
            )
        return result
