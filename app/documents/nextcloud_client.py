from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

import httpx

_DAV_NS = "DAV:"


@dataclass
class NextcloudFile:
    href: str
    display_name: str
    last_modified: datetime
    is_collection: bool


class NextcloudWebDavClient:
    """Thin Basic-Auth WebDAV wrapper shared by the outbound write-back
    adapter (app/documents/sharepoint.py's NextcloudWriteBackAdapter) and
    the inbound capture adapter (app/capture/adapters/nextcloud.py) — the
    HTTP/auth mechanics (same base URL, same app-password auth, same WebDAV
    verbs) are genuinely identical between the two; the higher-level
    adapters stay separate classes on separate Protocols, sharing only this
    one real, small client rather than a forced common abstraction.

    `username`/`app_password` is a Nextcloud app password (Settings ->
    Security -> "Create new app password"), not the account password —
    revocable independently, the same posture as a bot token elsewhere in
    this package.
    """

    def __init__(self, base_url: str, username: str, app_password: str):
        self._base_url = base_url.rstrip("/")
        self._dav_root = f"{self._base_url}/remote.php/dav/files/{username}"
        self._auth = httpx.BasicAuth(username, app_password)

    async def put(self, remote_path: str, content: bytes, content_type: str) -> None:
        async with httpx.AsyncClient(timeout=60, auth=self._auth) as client:
            response = await client.put(
                f"{self._dav_root}/{remote_path.lstrip('/')}",
                content=content,
                headers={"Content-Type": content_type},
            )
            response.raise_for_status()

    async def get(self, remote_path: str) -> bytes:
        async with httpx.AsyncClient(timeout=60, auth=self._auth) as client:
            response = await client.get(f"{self._dav_root}/{remote_path.lstrip('/')}")
            response.raise_for_status()
            return response.content

    async def get_by_href(self, href: str) -> bytes:
        """`href` is a PROPFIND response's own `<d:href>` value
        (`propfind_changes_since` below / `NextcloudFile.href`) — already a
        full server-relative path (e.g.
        `/remote.php/dav/files/cue/CUE-Capture/file.txt`), unlike `get`'s
        `remote_path` (a caller-supplied path *relative to this client's own
        dav_root*, e.g. `"CUE-Capture/file.txt"`). Prepending `_dav_root` to
        an already-absolute href would double the `/remote.php/dav/files/
        {username}` segment — this method exists specifically so
        app/capture/adapters/nextcloud.py's fetch_media (which only ever has
        a PROPFIND-sourced href to work from, per RawCapturedMedia.uri) uses
        the correct base."""
        async with httpx.AsyncClient(timeout=60, auth=self._auth) as client:
            response = await client.get(f"{self._base_url}{href}")
            response.raise_for_status()
            return response.content

    async def propfind_changes_since(
        self, remote_folder: str, since: datetime | None
    ) -> list[NextcloudFile]:
        """Depth-1 PROPFIND over `remote_folder`, filtered to entries whose
        `getlastmodified` is after `since` — the WebDAV equivalent of
        Graph's `/drives/{id}/root/delta` (PRD §9.3) this adapter substitutes
        for. Nextcloud has no native delta/changes API over plain WebDAV, so
        this polls the folder listing rather than subscribing to a feed —
        adequate for a demo/early-production document set, named here rather
        than silently assumed."""
        body = """<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:">
  <d:prop><d:displayname/><d:getlastmodified/><d:resourcetype/></d:prop>
</d:propfind>"""
        async with httpx.AsyncClient(timeout=60, auth=self._auth) as client:
            response = await client.request(
                "PROPFIND",
                f"{self._dav_root}/{remote_folder.strip('/')}",
                content=body,
                headers={"Depth": "1", "Content-Type": "application/xml"},
            )
            response.raise_for_status()
        files = _parse_propfind(response.text)
        if since is None:
            return [f for f in files if not f.is_collection]
        return [f for f in files if not f.is_collection and f.last_modified > since]


def _parse_propfind(xml_text: str) -> list[NextcloudFile]:
    root = ElementTree.fromstring(xml_text)
    results: list[NextcloudFile] = []
    for response in root.findall(f"{{{_DAV_NS}}}response"):
        href = response.findtext(f"{{{_DAV_NS}}}href") or ""
        propstat = response.find(f"{{{_DAV_NS}}}propstat")
        if propstat is None:
            continue
        prop = propstat.find(f"{{{_DAV_NS}}}prop")
        if prop is None:
            continue
        display_name = prop.findtext(f"{{{_DAV_NS}}}displayname") or href
        last_modified_raw = prop.findtext(f"{{{_DAV_NS}}}getlastmodified")
        last_modified = parsedate_to_datetime(last_modified_raw) if last_modified_raw else datetime.min
        resourcetype = prop.find(f"{{{_DAV_NS}}}resourcetype")
        is_collection = resourcetype is not None and resourcetype.find(f"{{{_DAV_NS}}}collection") is not None
        results.append(
            NextcloudFile(
                href=href, display_name=display_name, last_modified=last_modified, is_collection=is_collection
            )
        )
    return results
