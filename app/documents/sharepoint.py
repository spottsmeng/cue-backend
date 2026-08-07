import asyncio
import logging
from functools import lru_cache
from pathlib import PurePosixPath
from typing import Protocol

import httpx
import msal

from app.documents.config import SharePointSettings, get_sharepoint_settings
from app.documents.models import DocumentVersion

logger = logging.getLogger("cue.documents.sharepoint")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
# Graph's simple "upload or replace content" endpoint tops out at 4 MiB —
# above that it requires a resumable upload session, which this adapter
# does not implement (see GraphSharePointAdapter's own docstring).
_SIMPLE_UPLOAD_LIMIT_BYTES = 4 * 1024 * 1024

# FR-DOC-07: "write canonical versions back to Pico's existing SharePoint
# library structure". The real Microsoft Graph-backed implementation
# (GraphSharePointAdapter, below) was originally deferred to the
# real-capture milestone (Prompt 11) — brought forward ahead of that
# schedule because a competition demo needs to show a real write, not a
# logged no-op. It targets whatever tenant CUE_SHAREPOINT_* points at:
# Pico's own tenant once real access exists, or a free Microsoft 365
# Developer Program sandbox tenant for demo/development before that — the
# adapter itself has no Pico-specific logic, only the configured
# tenant/site differs.


class SharePointAdapter(Protocol):
    async def write_back(self, version: DocumentVersion, document_name: str, content: bytes) -> None: ...


class NoOpSharePointAdapter:
    """Logs the write-back that would happen and returns — no Graph API
    call, no real SharePoint library touched. Still the default (CUE_
    SHAREPOINT_PROVIDER unset or "noop"): a fresh checkout, CI, and the
    test suite must never require live Microsoft 365 credentials to run."""

    async def write_back(self, version: DocumentVersion, document_name: str, content: bytes) -> None:
        logger.info(
            "sharepoint write-back (no-op): document_version=%s document_name=%r "
            "storage_ref=%s (%d bytes) — set CUE_SHAREPOINT_PROVIDER=graph for a real write",
            version.id,
            document_name,
            version.storage_ref,
            len(content),
        )


class SharePointConfigError(Exception):
    """Raised at adapter construction, not at write_back time — a missing
    tenant_id/client_id/client_secret/site should fail loudly and
    immediately (e.g. at app startup or first dependency resolution), not
    three steps into a live demo."""


class GraphSharePointAdapter:
    """Real Microsoft Graph write-back, via app-only (client-credentials)
    auth — CUE is a backend service acting on its own behalf, not a
    signed-in user, so this is Sites.ReadWrite.All as an *application*
    permission (admin-consented once per tenant), not a delegated/
    interactive login flow.

    Writes to the *same path* (`{library_folder}/{document_name}`) on every
    call, one file per Document — not a new file per version. That's a
    deliberate reading of FR-DOC-07's "write canonical versions": each
    approval overwrites the canonical file, and SharePoint's own built-in
    version history (a standard document-library setting, on by default in
    most tenants) then shows the approval trail natively inside SharePoint
    itself, which is both the more useful behaviour for anyone opening the
    library directly and the more convincing thing to point at in a demo.

    Known limitation, named rather than silently hit: files over 4 MiB need
    Graph's resumable upload-session API, not implemented here — this
    adapter is sized for a demo/early-production document set (specs,
    drawings, quotations), not arbitrarily large media. Raises a clear
    error rather than a confusing failure if a file exceeds the limit.
    """

    def __init__(self, settings: SharePointSettings):
        if not (settings.tenant_id and settings.client_id and settings.client_secret):
            raise SharePointConfigError(
                "CUE_SHAREPOINT_PROVIDER=graph requires CUE_SHAREPOINT_TENANT_ID, "
                "_CLIENT_ID and _CLIENT_SECRET"
            )
        if not settings.site_id and not (settings.site_hostname and settings.site_path):
            raise SharePointConfigError(
                "CUE_SHAREPOINT_PROVIDER=graph requires either CUE_SHAREPOINT_SITE_ID, or both "
                "CUE_SHAREPOINT_SITE_HOSTNAME and CUE_SHAREPOINT_SITE_PATH"
            )
        self._settings = settings
        self._msal_app: msal.ConfidentialClientApplication | None = None
        self._resolved_site_id: str | None = settings.site_id

    def _get_msal_app(self) -> msal.ConfidentialClientApplication:
        # Built lazily, not in __init__: MSAL's ConfidentialClientApplication
        # unconditionally performs a live OIDC tenant-discovery call against
        # the configured tenant as part of construction (there is no flag
        # to defer it — validate_authority only controls a *separate*
        # instance-discovery check). Constructing this adapter must stay a
        # pure, local operation — the same "construction is pure, real
        # validation happens on first actual use" shape AnthropicClient
        # (app/llm/client.py) already has for its own api_key — so the MSAL
        # app, and the network call building it triggers, is deferred to
        # the first token acquisition instead.
        if self._msal_app is None:
            self._msal_app = msal.ConfidentialClientApplication(
                self._settings.client_id,
                authority=f"https://login.microsoftonline.com/{self._settings.tenant_id}",
                client_credential=self._settings.client_secret,
            )
        return self._msal_app

    def _acquire_token_sync(self) -> str:
        # msal's HTTP calls are synchronous under the hood (uses `requests`)
        # — pushed through asyncio.to_thread same as storage.py does for
        # the synchronous minio SDK, so it doesn't block the event loop.
        result = self._get_msal_app().acquire_token_for_client(
            scopes=["https://graph.microsoft.com/.default"]
        )
        if "access_token" not in result:
            raise RuntimeError(
                f"failed to acquire Graph token: {result.get('error_description', result)}"
            )
        return result["access_token"]

    async def _acquire_token(self) -> str:
        return await asyncio.to_thread(self._acquire_token_sync)

    async def _resolve_site_id(self, client: httpx.AsyncClient, token: str) -> str:
        if self._resolved_site_id:
            return self._resolved_site_id
        response = await client.get(
            f"{GRAPH_BASE}/sites/{self._settings.site_hostname}:{self._settings.site_path}",
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        self._resolved_site_id = response.json()["id"]
        return self._resolved_site_id

    async def write_back(self, version: DocumentVersion, document_name: str, content: bytes) -> None:
        if len(content) > _SIMPLE_UPLOAD_LIMIT_BYTES:
            raise RuntimeError(
                f"document_version {version.id} is {len(content)} bytes, over Graph's 4 MiB simple-"
                "upload limit — resumable upload sessions aren't implemented by this adapter"
            )

        folder = self._settings.library_folder.strip("/")
        path = f"{folder}/{document_name}" if folder else document_name

        async with httpx.AsyncClient(timeout=60) as client:
            token = await self._acquire_token()
            site_id = await self._resolve_site_id(client, token)
            # Path-addressed "upload or replace content" — Graph creates
            # any missing intermediate folders automatically, so
            # `library_folder` needs no separate provisioning step.
            response = await client.put(
                f"{GRAPH_BASE}/sites/{site_id}/drive/root:/{path}:/content",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/octet-stream",
                },
                content=content,
            )
            response.raise_for_status()

        logger.info(
            "sharepoint write-back (graph): document_version=%s -> site=%s path=%r",
            version.id, site_id, path,
        )


@lru_cache
def get_sharepoint_adapter() -> SharePointAdapter:
    settings = get_sharepoint_settings()
    if settings.provider == "noop":
        return NoOpSharePointAdapter()
    if settings.provider == "graph":
        return GraphSharePointAdapter(settings)
    raise ValueError(f"unknown CUE_SHAREPOINT_PROVIDER: {settings.provider!r}")
