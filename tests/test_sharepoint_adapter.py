"""No DB, no network — provider resolution and config validation are pure
config logic, mirroring tests/test_llm_factory.py's own approach for
app/llm/factory.py's identical provider-switch shape.

get_sharepoint_settings()/get_sharepoint_adapter() are @lru_cache'd
(deliberately — the real app shouldn't re-read .env or re-authenticate on
every request), so tests must clear both caches around each monkeypatch.
"""

import uuid

import pytest

from app.core.graph_auth import GraphSettings, get_graph_settings
from app.documents.config import SharePointSettings, get_sharepoint_settings
from app.documents.models import DocumentVersion
from app.documents.sharepoint import (
    GraphSharePointAdapter,
    NextcloudWriteBackAdapter,
    NoOpSharePointAdapter,
    SharePointConfigError,
    get_sharepoint_adapter,
)


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_sharepoint_settings.cache_clear()
    get_graph_settings.cache_clear()
    get_sharepoint_adapter.cache_clear()
    yield
    get_sharepoint_settings.cache_clear()
    get_graph_settings.cache_clear()
    get_sharepoint_adapter.cache_clear()


def test_default_provider_is_noop(monkeypatch):
    monkeypatch.delenv("CUE_SHAREPOINT_PROVIDER", raising=False)

    adapter = get_sharepoint_adapter()

    assert isinstance(adapter, NoOpSharePointAdapter)


@pytest.mark.asyncio
async def test_noop_adapter_write_back_is_a_genuine_no_op():
    """No exception, no network call — just a log line naming what a real
    write-back would have done."""
    adapter = NoOpSharePointAdapter()
    version = DocumentVersion(
        id=uuid.uuid4(), document_id=uuid.uuid4(), version_no=1, storage_ref="documents/x/v1/y",
    )
    await adapter.write_back(version, "Booth floor plan.pdf", b"fake pdf bytes")


def test_graph_provider_without_credentials_raises_config_error():
    # _env_file=None: this machine's own .env may have real CUE_GRAPH_*/
    # CUE_SHAREPOINT_* values (backend/PROGRESS.md's M8 notes) —
    # pydantic-settings' env_file is a fallback below actual os.environ, not
    # below "unset", so a bare constructor call isn't guaranteed empty
    # without this. Direct-construction tests below need the same treatment
    # whenever they assert "credentials absent".
    settings = SharePointSettings(provider="graph", site_id="test-site-id", _env_file=None)
    graph_settings = GraphSettings(_env_file=None)
    with pytest.raises(SharePointConfigError, match="TENANT_ID"):
        GraphSharePointAdapter(settings, graph_settings)


def test_graph_provider_without_site_raises_config_error():
    settings = SharePointSettings(provider="graph", _env_file=None)
    graph_settings = GraphSettings(tenant_id="t", client_id="c", client_secret="s")
    with pytest.raises(SharePointConfigError, match="SITE_ID"):
        GraphSharePointAdapter(settings, graph_settings)


def test_graph_provider_with_full_config_constructs(monkeypatch):
    monkeypatch.setenv("CUE_SHAREPOINT_PROVIDER", "graph")
    monkeypatch.setenv("CUE_GRAPH_TENANT_ID", "test-tenant")
    monkeypatch.setenv("CUE_GRAPH_CLIENT_ID", "test-client")
    monkeypatch.setenv("CUE_GRAPH_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("CUE_SHAREPOINT_SITE_ID", "test-site-id")

    adapter = get_sharepoint_adapter()

    assert isinstance(adapter, GraphSharePointAdapter)


def test_graph_provider_accepts_hostname_and_path_instead_of_site_id():
    settings = SharePointSettings(
        provider="graph", site_hostname="demo.sharepoint.com", site_path="/sites/CUEDemo",
    )
    graph_settings = GraphSettings(tenant_id="t", client_id="c", client_secret="s")
    adapter = GraphSharePointAdapter(settings, graph_settings)
    assert adapter is not None


def test_nextcloud_provider_without_credentials_raises_config_error():
    settings = SharePointSettings(provider="nextcloud", _env_file=None)
    with pytest.raises(SharePointConfigError, match="NEXTCLOUD_BASE_URL"):
        NextcloudWriteBackAdapter(settings)


def test_nextcloud_provider_with_full_config_constructs(monkeypatch):
    monkeypatch.setenv("CUE_SHAREPOINT_PROVIDER", "nextcloud")
    monkeypatch.setenv("CUE_SHAREPOINT_NEXTCLOUD_BASE_URL", "https://cloud.example.test")
    monkeypatch.setenv("CUE_SHAREPOINT_NEXTCLOUD_USERNAME", "cue-bot")
    monkeypatch.setenv("CUE_SHAREPOINT_NEXTCLOUD_APP_PASSWORD", "test-app-password")

    adapter = get_sharepoint_adapter()

    assert isinstance(adapter, NextcloudWriteBackAdapter)


def test_unknown_provider_raises(monkeypatch):
    monkeypatch.setenv("CUE_SHAREPOINT_PROVIDER", "dropbox")

    with pytest.raises(ValueError, match="unknown CUE_SHAREPOINT_PROVIDER"):
        get_sharepoint_adapter()


@pytest.mark.asyncio
async def test_graph_adapter_rejects_oversized_content():
    """4 MiB simple-upload limit, named explicitly rather than silently
    hit — no resumable-upload-session support in this adapter."""
    settings = SharePointSettings(provider="graph", site_id="test-site-id")
    graph_settings = GraphSettings(tenant_id="t", client_id="c", client_secret="s")
    adapter = GraphSharePointAdapter(settings, graph_settings)
    version = DocumentVersion(
        id=uuid.uuid4(), document_id=uuid.uuid4(), version_no=1, storage_ref="documents/x/v1/y",
    )
    oversized = b"0" * (4 * 1024 * 1024 + 1)

    with pytest.raises(RuntimeError, match="4 MiB"):
        await adapter.write_back(version, "Big render.pdf", oversized)
