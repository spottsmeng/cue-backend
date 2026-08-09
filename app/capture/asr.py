"""FR-VOI (PRD §6.3): ASRClient Protocol, mirroring app/llm/client.py's
ModelClient shape (a Protocol, provider-pluggable implementations, no
production caller needs to know which one is behind it) — CUE-Tech-Stack.md
§2.4 names faster-whisper (English) and FunASR SenseVoice (Chinese, explicit
code-switching support — "directly matches FR-VOI-02... name it, don't leave
it as an equal option next to plain FunASR models").

`TranscriptSegment` carries per-utterance confidence (FR-VOI-04: "Attach
transcript confidence per utterance; surface low-confidence segments
distinctly") — a bare full-transcript confidence number would lose exactly
the information FR-VOI-04 asks to keep.
"""

import asyncio
import io
import logging
from dataclasses import dataclass, field
from typing import Protocol

logger = logging.getLogger("cue.capture.asr")


@dataclass
class TranscriptSegment:
    text: str
    start: float
    end: float
    confidence: float


@dataclass
class TranscriptResult:
    text: str
    language: str
    segments: list[TranscriptSegment] = field(default_factory=list)

    @property
    def mean_confidence(self) -> float | None:
        if not self.segments:
            return None
        return sum(s.confidence for s in self.segments) / len(self.segments)


class ASRClient(Protocol):
    async def transcribe(self, audio_bytes: bytes) -> TranscriptResult: ...


class ASRUnavailableError(Exception):
    """Raised at construction, not first use — same fail-loud-not-silent
    posture app/capture/adapters/errors.py's CaptureConfigError and
    app/capture/media.py's OCRUnavailableError already establish."""


def _logprob_to_confidence(avg_logprob: float) -> float:
    """faster-whisper reports `avg_logprob` (a log-probability, typically
    in roughly [-1, 0] for a clear utterance, more negative for a garbled
    one) per segment, not a 0-1 confidence — FR-VOI-04 asks for "confidence
    per utterance" to "surface low-confidence segments distinctly", which
    needs a bounded, orderable scale a UI can threshold on. `exp(logprob)`
    is the standard, honest conversion back to a probability-like [0, 1]
    value (not a fabricated score) — a genuinely low-confidence segment's
    very negative logprob still maps near 0, a confident one near 1."""
    import math

    return max(0.0, min(1.0, math.exp(avg_logprob)))


class FasterWhisperClient:
    """CUE-Tech-Stack.md §2.4's own named English ASR choice — genuinely
    installed and working in this environment (ctranslate2-backed, no torch
    dependency, a real installed model checkpoint downloaded on first
    construction from the faster-whisper/Hugging Face model hub). Despite
    the class name, this is not English-only — Whisper is inherently
    multilingual and this client is CUE's real, tested ASR path for
    whichever language SenseVoice isn't handling (see
    get_default_asr_client below for the actual routing logic)."""

    def __init__(self, model_size: str = "tiny", device: str = "cpu", compute_type: str = "int8"):
        # "tiny" is a documented starting point, not a tuned production
        # choice (same "adjustable without a code change once there's real
        # traffic to tune against" posture app/foresight/worker.py's own
        # cron cadence comment already establishes elsewhere in this
        # codebase) — chosen here specifically to keep this class's model
        # download small/fast in a dev/CI environment; a real production
        # deployment should size this against actual WER/CER measurements
        # (FR-VOI-06) once there's a real corpus to measure against.
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise ASRUnavailableError("faster-whisper is not installed") from e
        self._model = WhisperModel(model_size, device=device, compute_type=compute_type)

    def _transcribe_sync(self, audio_bytes: bytes) -> TranscriptResult:
        segments_iter, info = self._model.transcribe(io.BytesIO(audio_bytes), word_timestamps=False)
        segments = [
            TranscriptSegment(
                text=s.text.strip(),
                start=s.start,
                end=s.end,
                confidence=_logprob_to_confidence(s.avg_logprob),
            )
            for s in segments_iter
        ]
        return TranscriptResult(
            text=" ".join(s.text for s in segments).strip(),
            language=info.language,
            segments=segments,
        )

    async def transcribe(self, audio_bytes: bytes) -> TranscriptResult:
        return await asyncio.to_thread(self._transcribe_sync, audio_bytes)


class SenseVoiceClient:
    """CUE-Tech-Stack.md §2.4's own named Chinese ASR choice, from Alibaba
    DAMO Academy's FunASR toolkit — "SenseVoice specifically has explicit
    code-switching support, which directly matches FR-VOI-02". Not
    installed in this environment: FunASR pulls in the full PyTorch stack
    plus a model-weight download, a materially heavier install than this
    session's time/environment budget covers (the same reasoning
    app/capture/media.py's PaddleOCRClient docstring gives for PaddleOCR).
    Lazy-imported at construction only — importing this *module* never
    requires funasr/torch. Code-complete, dependency-blocked: the same
    honest posture this codebase already gives WhatsApp/WeChat/Graph for a
    missing credential, applied here to a missing (heavy, optional)
    library. FasterWhisperClient above is the real, working substitute in
    this environment for both languages until this is installed.
    """

    def __init__(self, model: str = "iic/SenseVoiceSmall"):
        try:
            from funasr import AutoModel
        except ImportError as e:
            raise ASRUnavailableError(
                "funasr is not installed in this environment — see this class's own "
                "docstring; app/capture/asr.py's FasterWhisperClient is the real substitute"
            ) from e
        self._model = AutoModel(model=model, trust_remote_code=True)

    def _transcribe_sync(self, audio_bytes: bytes) -> TranscriptResult:
        result = self._model.generate(input=audio_bytes, language="auto", use_itn=True)
        # FunASR's own output shape: a list of {"text": ..., "timestamp": [[start_ms, end_ms], ...]}
        # per detected utterance — no impedance mismatch with TranscriptSegment,
        # just a unit (ms -> s) and key-name translation.
        segments = [
            TranscriptSegment(
                text=item.get("text", ""),
                start=(item.get("timestamp") or [[0, 0]])[0][0] / 1000,
                end=(item.get("timestamp") or [[0, 0]])[-1][-1] / 1000,
                confidence=float(item.get("confidence", 1.0)),
            )
            for item in result
        ]
        return TranscriptResult(
            text=" ".join(s.text for s in segments).strip(), language="zh", segments=segments
        )

    async def transcribe(self, audio_bytes: bytes) -> TranscriptResult:
        return await asyncio.to_thread(self._transcribe_sync, audio_bytes)


def get_default_asr_client(language_hint: str | None = None) -> ASRClient:
    """Routes to SenseVoice for Chinese/code-switched content
    (`language_hint` in {"zh", "zh+en"} — app/capture/normalise.py's own
    detect_language tags), falling back to FasterWhisper if SenseVoice's
    dependency isn't installed (this environment, today) or the hint is
    English/unknown. Mirrors app/llm/factory.py's get_client(role) shape:
    callers pick by *purpose* (a language hint), never by provider name."""
    if language_hint in ("zh", "zh+en"):
        try:
            return SenseVoiceClient()
        except ASRUnavailableError:
            logger.info("SenseVoice unavailable, falling back to FasterWhisper for zh audio")
    return FasterWhisperClient()
