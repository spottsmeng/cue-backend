"""FR-VOI end to end: a voice-note-only message (no text at all) goes
through app/capture/pipeline.py's ingest_raw_message, gets transcribed by
the real FasterWhisperClient (item 7), has its transcript copied onto
Message.text (so extraction has something to read), and the resulting
Commitment's Evidence gets a signed media_ref plus transcript_confidence
(FR-VOI-04/05). Real audio (macOS `say`), real ASR, real MinIO — only the
LLM extraction call itself is scripted (same reasoning as every other
pipeline test in this suite: cue-eval's own README already excludes the
live-Ollama path from this automated suite).
"""

import json
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.capture.models import Message, MessageMedia
from app.capture.pipeline import ingest_raw_message
from app.capture.schema import RawCapturedMedia, RawCapturedMessage, compute_payload_hash
from app.documents.storage import get_storage_backend
from app.models import Channel, Commitment, Evidence, Project
from tests.conftest import set_org_context

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


class _VoiceNoteAdapter:
    """Only fetch_media is exercised — this fake stands in for a real
    ChannelAdapter (WhatsApp/WeChat's real ones are credential-blocked;
    this test isn't about proving any one adapter's own fetch_media, that's
    tests/test_capture_adapters_live.py's job) purely to hand back a real
    voice note's bytes."""

    def __init__(self, audio_bytes: bytes):
        self._audio_bytes = audio_bytes

    async def fetch_media(self, channel: Channel, uri: str) -> bytes:
        return self._audio_bytes


class ScriptedModelClient:
    def __init__(self, rules: list[tuple[str, dict]]):
        self.rules = rules

    async def complete(self, prompt: str, schema: dict) -> str:
        for substring, response in self.rules:
            if substring in prompt:
                return json.dumps(response)
        return json.dumps({"commitments": []})


@pytest.mark.skipif(not _SAY_AVAILABLE, reason="macOS `say`/`afconvert` not available to synthesize test audio")
@pytest.mark.asyncio
async def test_voice_note_transcript_flows_into_extraction_and_evidence(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    channel = Channel(project_id=project_id, type="whatsapp", external_ref="g1", healthy=True)
    app_session.add(channel)
    await app_session.commit()

    audio = _synthesize_wav("Confirming the LED screen delivery for Friday")
    raw = RawCapturedMessage(
        external_id="voice-1",
        sender_external_id="+6591112222",
        sent_at=datetime(2026, 6, 22, tzinfo=dt_timezone.utc),
        text=None,
        raw_payload_hash=compute_payload_hash("whatsapp", "voice-1", b"voice-note-marker"),
        media=[RawCapturedMedia(kind="voice_note", uri="audio-1", filename="note.wav")],
    )

    client = ScriptedModelClient(
        rules=[("LED screen delivery", {
            "commitments": [{
                "act_type": "confirm",
                "deliverable_en": "LED screen delivery",
                "deliverable_original": "LED screen delivery",
                "evidence_span": "LED screen delivery",
                "confidence": 0.9,
            }]
        })]
    )

    message, is_new, created, media_processed = await ingest_raw_message(
        app_session, project=project, channel=channel, adapter=_VoiceNoteAdapter(audio), raw=raw,
        client=client, storage=get_storage_backend(),
    )
    await app_session.commit()

    assert is_new is True
    assert media_processed == 1
    assert message.text is not None
    assert "led screen" in message.text.lower() or "delivery" in message.text.lower()

    media = (
        await app_session.execute(select(MessageMedia).where(MessageMedia.message_id == message.id))
    ).scalar_one()
    assert media.storage_key is not None
    assert media.transcript_confidence is not None
    assert 0.0 <= media.transcript_confidence <= 1.0

    assert created == 1
    commitment = (
        await app_session.execute(select(Commitment).where(Commitment.project_id == project_id))
    ).scalar_one()
    evidence = (
        await app_session.execute(select(Evidence).where(Evidence.commitment_id == commitment.id))
    ).scalar_one()
    assert evidence.message_id == message.id
    assert evidence.media_ref is not None
    assert evidence.transcript_confidence == media.transcript_confidence
