#!/usr/bin/env python3
"""
CUE ASR eval — FR-VOI-06's WER/CER measurement, the same "continuous
accuracy vs. baseline" idea run_eval.py already applies to extraction,
applied to the second capability Prompt 13 names for this milestone.

Unlike run_eval.py, this is NOT stdlib-only: it exercises the real
app/capture/asr.py ASRClient implementations (FasterWhisperClient is the
only one actually installed in this environment; SenseVoiceClient is
dependency-blocked, same as everywhere else in this codebase), so it needs
the backend's own venv — run it with `uv run python3 cue-eval/asr_eval.py`
from backend/, not a bare `python3` the way run_eval.py's own docstring
advertises. run_eval.py's stdlib-only posture was about not depending on
app/llm/client.py's httpx-based clients specifically (portability for a
prompt-tuning smoke test); there's no equivalent lightweight way to call a
real ASR model, so this script doesn't attempt to preserve that property.

Audio is synthesized locally via macOS `say`/`afconvert` (same mechanism
tests/test_capture_asr.py and test_capture_voice_note_pipeline.py already
use) rather than committed as binary WAV files — cue-eval/asr_cases.json
holds only the reference text. Skips cleanly with a clear message on a
non-macOS runner (documented limitation, not a silent false pass) — see
that file's own "_comment" key for why this is a small synthetic corpus,
not real Pico vendor audio.

  uv run python3 cue-eval/asr_eval.py
  uv run python3 cue-eval/asr_eval.py --json
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
BACKEND_DIR = HERE.parent
sys.path.insert(0, str(BACKEND_DIR))

CASES = json.loads((HERE / "asr_cases.json").read_text(encoding="utf-8"))["cases"]

_SAY_AVAILABLE = shutil.which("say") is not None and shutil.which("afconvert") is not None
_ZH_VOICE = "Tingting"  # zh_CN voice (`say -v '?'`) — confirmed non-silent, unlike "Eddy"


def _synthesize_wav(text: str, *, lang: str) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        aiff_path = Path(tmp) / "speech.aiff"
        wav_path = Path(tmp) / "speech.wav"
        say_cmd = ["say", "-o", str(aiff_path)]
        if lang == "zh":
            say_cmd += ["-v", _ZH_VOICE]
        say_cmd.append(text)
        subprocess.run(say_cmd, check=True)
        subprocess.run(
            ["afconvert", "-f", "WAVE", "-d", "LEI16", str(aiff_path), str(wav_path)], check=True
        )
        return wav_path.read_bytes()


def _edit_distance(a: list, b: list) -> int:
    """Plain Levenshtein distance over a sequence of tokens (words for WER,
    characters for CER) — stdlib only, no jiwer/editdistance dependency for
    something this small."""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        prev = cur
    return prev[-1]


def word_error_rate(reference: str, hypothesis: str) -> float:
    ref_words = reference.split()
    hyp_words = hypothesis.split()
    if not ref_words:
        return 0.0 if not hyp_words else 1.0
    return _edit_distance(ref_words, hyp_words) / len(ref_words)


def character_error_rate(reference: str, hypothesis: str) -> float:
    ref_chars = list(reference.replace(" ", ""))
    hyp_chars = list(hypothesis.replace(" ", ""))
    if not ref_chars:
        return 0.0 if not hyp_chars else 1.0
    return _edit_distance(ref_chars, hyp_chars) / len(ref_chars)


async def _run(cases) -> list[dict]:
    from app.capture.asr import FasterWhisperClient

    client = FasterWhisperClient(model_size="tiny")
    results = []
    for case in cases:
        audio = _synthesize_wav(case["text"], lang=case["lang"])
        transcript = await client.transcribe(audio)
        if case["lang"] == "zh":
            rate = character_error_rate(case["text"], transcript.text)
            metric = "cer"
        else:
            rate = word_error_rate(case["text"].lower(), transcript.text.lower())
            metric = "wer"
        results.append(
            {
                "id": case["id"], "lang": case["lang"], "metric": metric, "rate": rate,
                "reference": case["text"], "hypothesis": transcript.text,
            }
        )
    return results


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="also print a JSON_SUMMARY: line")
    args = ap.parse_args()

    if not _SAY_AVAILABLE:
        # Documented limitation, not a silent false pass — same posture
        # tests/test_capture_asr.py's own skipif establishes.
        print("macOS `say`/`afconvert` not available — cannot synthesize the labelled set on this "
              "machine. This eval only runs where it was designed to (see this file's own docstring).")
        sys.exit(0)

    import asyncio

    results = asyncio.run(_run(CASES))

    print("\n  {:<5} {:<5} {:<7} {:>8}   reference / hypothesis".format("id", "lang", "metric", "rate"))
    print("  " + "-" * 70)
    for r in results:
        print("  {:<5} {:<5} {:<7} {:>7.1%}   {!r} / {!r}".format(
            r["id"], r["lang"], r["metric"], r["rate"], r["reference"], r["hypothesis"]))

    en = [r["rate"] for r in results if r["lang"] == "en"]
    zh = [r["rate"] for r in results if r["lang"] == "zh"]
    wer = sum(en) / len(en) if en else None
    cer = sum(zh) / len(zh) if zh else None
    print("\n  overall WER (en)   {}".format("{:.1%}".format(wer) if wer is not None else "n/a"))
    print("  overall CER (zh)   {}".format("{:.1%}".format(cer) if cer is not None else "n/a"))
    # PRD §8.1 targets: WER <= 8%, CER <= 10%. First real run of this
    # harness (2026-08): CER measured far above target, but a real,
    # named confound, not "wrong content" — FasterWhisperClient's `tiny`
    # model outputs Traditional Chinese characters even for zh_CN speech,
    # so a per-character diff against this file's Simplified reference
    # text counts every script-variant character as an error. A fair CER
    # needs Simplified/Traditional normalization before comparison
    # (e.g. OpenCC) — deliberately not added here (one more dependency
    # for a 6-case harness); flag this, don't let the raw number alone
    # stand in as "Chinese ASR is broken."
    print()

    if args.json:
        summary = {"wer": wer, "cer": cer, "n_cases": len(results), "cases": results}
        print("JSON_SUMMARY:" + json.dumps(summary))


if __name__ == "__main__":
    main()
