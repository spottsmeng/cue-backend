import asyncio
import io
from functools import lru_cache
from typing import Protocol

from minio import Minio
from minio.error import S3Error

from app.documents.config import get_storage_settings

# No object store existed anywhere in this codebase before this session
# (confirmed: no S3/MinIO client anywhere) — this Protocol is the seam that
# keeps a specific backend out of app/api/documents.py, the same shape
# app/llm/client.py's ModelClient establishes for provider-pluggable LLM
# calls. MinioStorageBackend below is the one real implementation
# (local dev/test, per docker-compose.yml's `minio` service); tests override
# the FastAPI dependency with an in-memory fake rather than requiring a live
# MinIO container, the same "fake client, not a mock" posture
# tests/test_extractor.py's FakeModelClient already established for
# ModelClient.


class StorageBackend(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...
    async def get(self, key: str) -> bytes: ...
    def signed_url(self, key: str, expires_seconds: int = 3600) -> str: ...


class MinioStorageBackend:
    """Original file bytes are never mutated after upload (WHAT TO BUILD
    #2's own instruction) — `put` is only ever called once per object key
    (app/documents/service.py's create_version mints a fresh key per
    version); thumbnails/derived text are separate rows/objects, never an
    overwrite of this one. The minio SDK is synchronous; every call is
    pushed through asyncio.to_thread so it doesn't block the event loop the
    way a bare synchronous call would in an async FastAPI handler.
    """

    def __init__(self, endpoint: str, access_key: str, secret_key: str, bucket: str, secure: bool):
        self._client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
        self._bucket = bucket

    def _ensure_bucket_sync(self) -> None:
        if not self._client.bucket_exists(self._bucket):
            self._client.make_bucket(self._bucket)

    async def _ensure_bucket(self) -> None:
        await asyncio.to_thread(self._ensure_bucket_sync)

    def _put_sync(self, key: str, data: bytes, content_type: str) -> None:
        self._client.put_object(
            self._bucket, key, io.BytesIO(data), length=len(data), content_type=content_type
        )

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        await self._ensure_bucket()
        await asyncio.to_thread(self._put_sync, key, data, content_type)

    def _get_sync(self, key: str) -> bytes:
        response = self._client.get_object(self._bucket, key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    async def get(self, key: str) -> bytes:
        try:
            return await asyncio.to_thread(self._get_sync, key)
        except S3Error as e:
            raise FileNotFoundError(f"no such object: {key!r}") from e

    def signed_url(self, key: str, expires_seconds: int = 3600) -> str:
        from datetime import timedelta

        # A real, root-caused bug (not flakiness) surfaced by a CI run
        # against a fresh MinIO with an empty volume: `put()` is the only
        # method that ever called `_ensure_bucket` — fine for a real
        # upload, but `scripts/seed_dev_data.py` seeds two DocumentVersion
        # rows with a `storage_ref` pointing at no real MinIO object at all
        # (by design, its own comment says so — "this fixture never
        # exercises download/approve on these two rows"). F5's Ask feature
        # later added a code path that *does* touch these seeded rows'
        # signed_url regardless (citation resolution, `GET .../documents/
        # versions/{id}`), which nobody anticipated when that seed comment
        # was written. Locally this was invisible — a long-lived dev MinIO
        # container's named volume already has the bucket from a real
        # upload some earlier session did — but a fresh/ephemeral MinIO
        # (CI's own `docker compose up -d --wait`, no prior volume) 500s
        # with `minio.error.S3Error: NoSuchBucket` the instant any test
        # resolves a seeded document's citation before any real upload has
        # happened to create the bucket first. `presigned_get_object`
        # itself never checks the *object* exists (a wrong/expired key
        # 404s only when the URL is actually fetched, the correct existing
        # behaviour for a seeded fake storage_ref) — only the *bucket*
        # needs to exist for signing to succeed at all, so ensuring it
        # here, the same idempotent check `put()` already makes, is
        # sufficient and doesn't hide a genuinely missing object.
        self._ensure_bucket_sync()
        return self._client.presigned_get_object(
            self._bucket, key, expires=timedelta(seconds=expires_seconds)
        )


@lru_cache
def get_storage_backend() -> StorageBackend:
    """FastAPI dependency default — a live MinIO connection is only opened
    (bucket_exists/make_bucket) on the first real `put`, not at import time,
    so importing this module never requires MinIO to be reachable."""
    settings = get_storage_settings()
    return MinioStorageBackend(
        endpoint=settings.endpoint,
        access_key=settings.access_key,
        secret_key=settings.secret_key,
        bucket=settings.bucket,
        secure=settings.secure,
    )
