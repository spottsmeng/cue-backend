"""FR-VOI: app/capture/asr.py's ASRClient Protocol. FasterWhisperClient is
genuinely installed and exercised for real here — a real WAV file
synthesized locally (macOS `say` + `afconvert`, both system tools, no new
dependency), transcribed by a real faster-whisper model (no mocking the ASR
call itself, same "against real infrastructure" posture as every other
integration test in this suite). SenseVoiceClient's own test only proves the
fail-loudly-at-construction contract (funasr genuinely isn't installed in
this environment — see that class's own docstring for why).
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from app.capture.asr import (
    ASRUnavailableError,
    FasterWhisperClient,
    SenseVoiceClient,
    get_default_asr_client,
)

_SAY_AVAILABLE = shutil.which("say") is not None and shutil.which("afconvert") is not None


def _synthesize_wav(text: str) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        aiff_path = Path(tmp) / "speech.aiff"
        wav_path = Path(tmp) / "speech.wav"
        subprocess.run(["say", "-o", str(aiff_path), text], check=True)
        subprocess.run(
            ["afconvert", "-f", "WAVE", "-d", "LEI16", str(aiff_path), str(wav_path)], check=True
        )
        return wav_path.read_bytes()


@pytest.mark.skipif(not _SAY_AVAILABLE, reason="macOS `say`/`afconvert` not available to synthesize test audio")
@pytest.mark.asyncio
async def test_faster_whisper_transcribes_real_synthesized_speech():
    audio = _synthesize_wav("Confirm the LED screen delivery by Friday afternoon")
    client = FasterWhisperClient(model_size="tiny")

    result = await client.transcribe(audio)

    assert "led screen" in result.text.lower() or "delivery" in result.text.lower()
    assert result.language == "en"
    assert len(result.segments) >= 1
    for segment in result.segments:
        assert 0.0 <= segment.confidence <= 1.0
        assert segment.end >= segment.start
    assert result.mean_confidence is not None
    assert 0.0 <= result.mean_confidence <= 1.0


@pytest.mark.asyncio
async def test_transcript_result_mean_confidence_none_when_no_segments():
    from app.capture.asr import TranscriptResult

    assert TranscriptResult(text="", language="en", segments=[]).mean_confidence is None


def test_sensevoice_raises_unavailable_error_in_this_environment():
    """funasr genuinely isn't installed here (see SenseVoiceClient's own
    docstring) — this proves the fail-loud-at-construction contract, the
    same "code-complete, dependency-blocked" posture
    app/capture/media.py's PaddleOCRClient test coverage would give if it
    had one (that class's own module has no dedicated test, since
    OCRUnavailableError there is exercised implicitly by
    get_default_ocr_client already falling back successfully — this
    module's default failure path is what's under test here directly)."""
    with pytest.raises(ASRUnavailableError):
        SenseVoiceClient()


def test_get_default_asr_client_falls_back_to_faster_whisper_for_chinese():
    """SenseVoice is unavailable in this environment, so even a Chinese
    language hint must still land on a working client, not raise."""
    client = get_default_asr_client(language_hint="zh+en")
    assert isinstance(client, FasterWhisperClient)


def test_get_default_asr_client_routes_english_directly_to_faster_whisper():
    client = get_default_asr_client(language_hint="en")
    assert isinstance(client, FasterWhisperClient)
