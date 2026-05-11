"""End-to-end tests for the WebhookSignatureMiddleware.

Boots a fresh FastAPI app with a non-empty webhook secret, signs a
request body the same way the Pipedream forwarder does, and asserts the
middleware accepts / rejects exactly the cases it should.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from alex_agent_runtime.config import Settings, get_settings
from alex_agent_runtime.db import admin_session
from alex_agent_runtime.main import create_app
from alex_agent_runtime.webhook_signing import expected_signature

SECRET = "integration-test-secret"


@pytest_asyncio.fixture
async def signed_client(monkeypatch) -> AsyncIterator[AsyncClient]:
    """An app instance with webhook signing enforced."""
    # Override Settings so the middleware sees a non-empty secret. We swap
    # the cached settings instance returned by get_settings() so all
    # middleware/lifespan callers pick it up.
    overridden = Settings(alex_webhook_secret=SECRET)
    monkeypatch.setattr(
        "alex_agent_runtime.config.get_settings", lambda: overridden
    )
    # Also patch the symbol re-exported by the middleware module's import.
    import alex_agent_runtime.middleware as mw
    monkeypatch.setattr(mw, "get_settings", lambda: overridden)

    app = create_app()
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@pytest_asyncio.fixture
async def signed_tenant(_engine) -> AsyncIterator[UUID]:
    tenant_id = uuid4()
    async with admin_session() as session:
        await session.execute(
            text("INSERT INTO tenants (id, name) VALUES (:id, :name)"),
            {"id": str(tenant_id), "name": f"signed-{tenant_id}"},
        )
    yield tenant_id
    async with admin_session(allow_audit_purge=True) as session:
        await session.execute(
            text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)}
        )


def _signed_headers(*, tenant: UUID, body: bytes, timestamp: str | None = None) -> dict[str, str]:
    ts = timestamp or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    sig = expected_signature(secret=SECRET, timestamp=ts, body=body)
    return {
        "Content-Type": "application/json",
        "X-Tenant-Id": str(tenant),
        "X-Alex-Signature": sig,
        "X-Alex-Timestamp": ts,
    }


async def test_signed_events_round_trip(signed_client, signed_tenant):
    body = json.dumps(
        {
            "event_id": "signed-1",
            "source": "pipedream",
            "kind": "unknown",
            "occurred_at": "2026-05-11T08:00:00Z",
            "payload": {},
        }
    ).encode("utf-8")
    response = await signed_client.post(
        "/events",
        content=body,
        headers=_signed_headers(tenant=signed_tenant, body=body),
    )
    assert response.status_code == 202, response.text


async def test_unsigned_request_rejected(signed_client, signed_tenant):
    response = await signed_client.post(
        "/events",
        json={
            "event_id": "unsigned-1",
            "source": "pipedream",
            "kind": "unknown",
            "occurred_at": "2026-05-11T08:00:00Z",
            "payload": {},
        },
        headers={"X-Tenant-Id": str(signed_tenant)},
    )
    assert response.status_code == 401
    assert response.json()["error"] == "missing_signature"


async def test_bad_signature_rejected(signed_client, signed_tenant):
    body = b'{"event_id":"bad","source":"pipedream","kind":"unknown","occurred_at":"2026-05-11T08:00:00Z","payload":{}}'
    headers = _signed_headers(tenant=signed_tenant, body=body)
    headers["X-Alex-Signature"] = "sha256=" + "0" * 64
    response = await signed_client.post("/events", content=body, headers=headers)
    assert response.status_code == 401
    assert response.json()["error"] == "invalid_signature"


async def test_stale_timestamp_rejected(signed_client, signed_tenant):
    body = b'{"event_id":"stale","source":"pipedream","kind":"unknown","occurred_at":"2026-05-11T08:00:00Z","payload":{}}'
    headers = _signed_headers(
        tenant=signed_tenant,
        body=body,
        timestamp="2020-01-01T00:00:00Z",
    )
    response = await signed_client.post("/events", content=body, headers=headers)
    assert response.status_code == 401
    assert response.json()["error"] == "stale_signature"


async def test_health_routes_skip_signature_check(signed_client):
    response = await signed_client.get("/healthz")
    assert response.status_code == 200
