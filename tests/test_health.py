"""Codifies the manual `curl http://localhost:8000/health` check done earlier
in this session — a real request through FastAPI's dependency injection,
hitting the real (test) database via the same get_session dependency
production uses.

httpx.AsyncClient with ASGITransport, not FastAPI's sync TestClient —
TestClient runs requests through its own anyio thread-backed portal, which
hangs the whole suite when combined with the other async tests sharing one
session-scoped event loop (harmless in isolation, only surfaces running the
full suite together). AsyncClient runs in the same loop as everything else.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from main import app


@pytest.mark.asyncio
async def test_health_returns_ok():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
