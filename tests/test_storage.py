"""app/documents/storage.py's MinioStorageBackend — through the real MinIO
(docker-compose.yml's `minio` service), same "no mocks" testing philosophy
tests/test_documents_api.py's own header comment already states for this
module.
"""

import uuid

import pytest

from app.documents.storage import MinioStorageBackend


def _backend(bucket: str) -> MinioStorageBackend:
    return MinioStorageBackend(
        endpoint="localhost:9000",
        access_key="cue",
        secret_key="cue_minio_secret",
        bucket=bucket,
        secure=False,
    )


@pytest.mark.asyncio
async def test_signed_url_succeeds_against_a_bucket_that_was_never_written_to():
    """A real, root-caused bug this session found via a CI failure, not
    inferred: `signed_url` never called `_ensure_bucket` — only `put` did.
    Harmless against a long-lived local MinIO whose named volume already
    has the bucket from some earlier real upload, but a genuinely fresh
    bucket (a brand-new CI MinIO container, or — as here — one this test
    guarantees was never touched via a random name) 500s with
    `minio.error.S3Error: NoSuchBucket`. This is exactly the shape
    `scripts/seed_dev_data.py`'s two documents hit for real: their
    `storage_ref` points at no real object, by that script's own comment,
    but F5's Ask citation resolution still calls `signed_url` on them
    regardless. Fixed by ensuring the bucket in `signed_url` too, the same
    idempotent check `put` already makes."""
    backend = _backend(f"cue-test-{uuid.uuid4().hex[:12]}")

    url = backend.signed_url("documents/does-not-matter/v1/seed")

    assert url.startswith("http")


@pytest.mark.asyncio
async def test_put_then_signed_url_still_works_on_a_real_object():
    """The ordinary path — an object that was actually uploaded — stays
    correct: `signed_url` doesn't skip ensuring the bucket just because
    `put` already did, it's the same idempotent check either way."""
    backend = _backend(f"cue-test-{uuid.uuid4().hex[:12]}")
    key = f"documents/{uuid.uuid4()}/v1/real"
    await backend.put(key, b"hello", "text/plain")

    url = backend.signed_url(key)

    assert url.startswith("http")
    assert key in url or "X-Amz-Credential" in url
