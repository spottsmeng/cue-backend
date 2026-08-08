import asyncio
from functools import lru_cache

import msal
from pydantic_settings import BaseSettings, SettingsConfigDict


class GraphSettings(BaseSettings):
    """One Entra ID (Azure AD) app registration backs the whole Microsoft
    Graph surface CUE talks to — SharePoint write-back
    (app/documents/sharepoint.py) and, once credentials exist, the
    capture-side GraphAdapter's Teams/Outlook/SharePoint reads
    (app/capture/adapters/graph.py) both read this one settings object
    rather than each keeping its own copy. Application permission
    (Sites.ReadWrite.All / ChannelMessage.Read.All / Mail.Read etc.,
    per-capability, admin-consented), client-credentials flow — CUE acts on
    its own behalf, not as a signed-in user."""

    tenant_id: str | None = None
    client_id: str | None = None
    client_secret: str | None = None

    model_config = SettingsConfigDict(env_prefix="CUE_GRAPH_", env_file=".env", extra="ignore")


@lru_cache
def get_graph_settings() -> GraphSettings:
    return GraphSettings()


class GraphConfigError(Exception):
    """Raised at GraphTokenProvider construction — a missing tenant/client
    credential should fail loudly and immediately, not partway into a live
    call."""


class GraphTokenProvider:
    """Shared MSAL client-credentials token acquisition, extracted from
    app/documents/sharepoint.py's original GraphSharePointAdapter so the
    capture-side GraphAdapter doesn't build a second MSAL client against the
    same tenant."""

    def __init__(self, settings: GraphSettings):
        if not (settings.tenant_id and settings.client_id and settings.client_secret):
            raise GraphConfigError(
                "Graph access requires CUE_GRAPH_TENANT_ID, _CLIENT_ID and _CLIENT_SECRET"
            )
        self._settings = settings
        self._msal_app: msal.ConfidentialClientApplication | None = None

    def _get_msal_app(self) -> msal.ConfidentialClientApplication:
        # Built lazily, not in __init__: MSAL's ConfidentialClientApplication
        # unconditionally performs a live OIDC tenant-discovery call against
        # the configured tenant as part of construction — no flag defers it.
        # Constructing this provider must stay a pure, local operation, so
        # the MSAL app (and the network call building it triggers) waits for
        # first actual token acquisition instead.
        if self._msal_app is None:
            self._msal_app = msal.ConfidentialClientApplication(
                self._settings.client_id,
                authority=f"https://login.microsoftonline.com/{self._settings.tenant_id}",
                client_credential=self._settings.client_secret,
            )
        return self._msal_app

    def _acquire_token_sync(self) -> str:
        # msal's HTTP calls are synchronous (uses `requests`) — pushed
        # through asyncio.to_thread so it doesn't block the event loop.
        result = self._get_msal_app().acquire_token_for_client(
            scopes=["https://graph.microsoft.com/.default"]
        )
        if "access_token" not in result:
            raise RuntimeError(
                f"failed to acquire Graph token: {result.get('error_description', result)}"
            )
        return result["access_token"]

    async def acquire_token(self) -> str:
        return await asyncio.to_thread(self._acquire_token_sync)
