import logging
from functools import lru_cache
from pathlib import PurePosixPath
from typing import Protocol

import httpx

from app.core.graph_auth import GraphConfigError, GraphSettings, GraphTokenProvider, get_graph_settings
from app.documents.config import SharePointSettings, get_sharepoint_settings
from app.documents.models import DocumentVersion
from app.documents.nextcloud_client import NextcloudWebDavClient

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

    def __init__(self, settings: SharePointSettings, graph_settings: GraphSettings | None = None):
        # GraphConfigError (from GraphTokenProvider, below) covers the
        # tenant/client credential half — this class only needs to add the
        # site-specific half SharePointSettings still owns.
        if not settings.site_id and not (settings.site_hostname and settings.site_path):
            raise SharePointConfigError(
                "CUE_SHAREPOINT_PROVIDER=graph requires either CUE_SHAREPOINT_SITE_ID, or both "
                "CUE_SHAREPOINT_SITE_HOSTNAME and CUE_SHAREPOINT_SITE_PATH"
            )
        try:
            self._token_provider = GraphTokenProvider(graph_settings or get_graph_settings())
        except GraphConfigError as e:
            raise SharePointConfigError(
                f"CUE_SHAREPOINT_PROVIDER=graph requires Graph credentials: {e}"
            ) from e
        self._settings = settings
        self._resolved_site_id: str | None = settings.site_id

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
            token = await self._token_provider.acquire_token()
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


class NextcloudWriteBackAdapter:
    """FR-DOC-07's write-back target, built ahead of Pico's Microsoft Graph
    credentials against a real Nextcloud instance (CUE-PRD.md §9.3a) —
    genuinely functional, not a sandbox pretending to be Pico's. Same
    "one file per Document, overwritten each version" reading of FR-DOC-07
    GraphSharePointAdapter's own docstring already argues for — Nextcloud's
    own built-in file versioning then shows the approval trail natively,
    the same role SharePoint's version history plays for the Graph path.
    """

    def __init__(self, settings: SharePointSettings):
        if not (settings.nextcloud_base_url and settings.nextcloud_username
                 and settings.nextcloud_app_password):
            raise SharePointConfigError(
                "CUE_SHAREPOINT_PROVIDER=nextcloud requires CUE_SHAREPOINT_NEXTCLOUD_BASE_URL, "
                "_NEXTCLOUD_USERNAME and _NEXTCLOUD_APP_PASSWORD"
            )
        self._settings = settings
        self._client = NextcloudWebDavClient(
            settings.nextcloud_base_url, settings.nextcloud_username, settings.nextcloud_app_password
        )

    async def write_back(self, version: DocumentVersion, document_name: str, content: bytes) -> None:
        folder = self._settings.nextcloud_remote_folder.strip("/")
        path = f"{folder}/{document_name}" if folder else document_name
        await self._client.put(path, content, "application/octet-stream")
        logger.info(
            "sharepoint write-back (nextcloud): document_version=%s -> path=%r", version.id, path
        )


@lru_cache
def get_sharepoint_adapter() -> SharePointAdapter:
    settings = get_sharepoint_settings()
    if settings.provider == "noop":
        return NoOpSharePointAdapter()
    if settings.provider == "graph":
        return GraphSharePointAdapter(settings)
    if settings.provider == "nextcloud":
        return NextcloudWriteBackAdapter(settings)
    raise ValueError(f"unknown CUE_SHAREPOINT_PROVIDER: {settings.provider!r}")
