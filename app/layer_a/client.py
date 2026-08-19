import httpx

from app.layer_a.config import LayerAAdminSettings, get_layer_a_admin_settings


class LayerAConfigError(Exception):
    """Raised at client construction, not at first use — mirrors
    app/capture/adapters/errors.py's CaptureConfigError. Callers that expect
    this to be optional (app/layer_a/poller.py's sweep, which is opt-in per
    organisation) check settings before constructing a client at all, rather
    than catching this; app/api/layer_a_admin.py's live-status route does
    catch it, translated to a 503."""


class LayerAAdminClient:
    """Thin wrapper around Layer A's admin API (layer-A/src/api/admin/
    index.ts) — HTTP Basic auth, held only here, server-side, never
    returned to any caller (the browser never sees Layer A's admin
    credentials, per this feature's own locked design decision)."""

    def __init__(self, settings: LayerAAdminSettings | None = None):
        settings = settings or get_layer_a_admin_settings()
        if not (settings.base_url and settings.username and settings.password):
            raise LayerAConfigError(
                "Layer A admin API requires CUE_LAYERA_ADMIN_BASE_URL, "
                "CUE_LAYERA_ADMIN_USERNAME and CUE_LAYERA_ADMIN_PASSWORD"
            )
        self._base_url = settings.base_url.rstrip("/")
        self._auth = (settings.username, settings.password)

    async def list_accounts(self) -> list[dict]:
        async with httpx.AsyncClient(auth=self._auth, timeout=10) as client:
            response = await client.get(f"{self._base_url}/admin/accounts")
            response.raise_for_status()
            return response.json().get("accounts", [])

    async def get_account(self, account_id: str) -> dict | None:
        async with httpx.AsyncClient(auth=self._auth, timeout=10) as client:
            response = await client.get(f"{self._base_url}/admin/accounts/{account_id}")
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()

    async def get_health_history(self, account_id: str) -> list[dict]:
        async with httpx.AsyncClient(auth=self._auth, timeout=10) as client:
            response = await client.get(f"{self._base_url}/admin/accounts/{account_id}/health-history")
            response.raise_for_status()
            return response.json().get("history", [])

    async def list_conflicts(self) -> list[dict]:
        async with httpx.AsyncClient(auth=self._auth, timeout=10) as client:
            response = await client.get(f"{self._base_url}/admin/conflicts")
            response.raise_for_status()
            return response.json().get("conflicts", [])
