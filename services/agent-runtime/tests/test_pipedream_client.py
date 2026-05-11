"""Tests for the outbound HTTP client to Pipedream workflows.

Uses httpx.MockTransport so no network is required and we can inspect
the exact request the runtime would send.
"""
from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest

from alex_agent_runtime.config import Settings
from alex_agent_runtime.schemas import (
    ActionRequest,
    ActionType,
    DryRunRequest,
)
from alex_agent_runtime.services.pipedream_client import (
    PipedreamClient,
    PipedreamConfigError,
    PipedreamExecutionError,
    _sign,
)


def _settings(*, secret: str = "test-secret", base_url: str = "https://pd.example.com") -> Settings:
    return Settings(alex_webhook_secret=secret, pipedream_base_url=base_url)


@pytest.fixture
def captured():
    return {"requests": []}


@pytest.fixture
def client(captured):
    async def handler(request: httpx.Request) -> httpx.Response:
        captured["requests"].append(
            {
                "url": str(request.url),
                "method": request.method,
                "headers": dict(request.headers),
                "body": request.content.decode("utf-8"),
            }
        )
        return httpx.Response(200, json={"ok": True, "echo": json.loads(request.content)})

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return PipedreamClient(settings=_settings(), client=http)


@pytest.mark.asyncio
async def test_dispatch_routes_crm_write_to_hubspot(client, captured):
    req = ActionRequest(
        action_id="act-1",
        tenant_id=uuid4(),
        rep_id=uuid4(),
        action_type=ActionType.CRM_WRITE,
        target_system="hubspot",
        target_id="deal-99",
        payload={"properties": {"stage": "negotiation"}},
    )
    response = await client.dispatch(req)
    await client.close()

    assert response["ok"] is True
    assert len(captured["requests"]) == 1
    r = captured["requests"][0]
    assert r["url"] == "https://pd.example.com/hubspot_crm_write"
    assert r["headers"]["x-tenant-id"] == str(req.tenant_id)
    assert r["headers"]["x-alex-signature"].startswith("sha256=")
    # Verify the signature would round-trip
    expected = _sign("test-secret", r["headers"]["x-alex-timestamp"], r["body"])
    assert r["headers"]["x-alex-signature"] == expected


@pytest.mark.asyncio
async def test_dispatch_unknown_route_raises_config_error(client):
    req = ActionRequest(
        action_id="act-2",
        tenant_id=uuid4(),
        rep_id=uuid4(),
        action_type=ActionType.CRM_WRITE,
        target_system="salesforce",  # not in the reference mapping
        payload={"properties": {}},
    )
    with pytest.raises(PipedreamConfigError):
        await client.dispatch(req)
    await client.close()


@pytest.mark.asyncio
async def test_dispatch_raises_on_4xx():
    async def handler(request):
        return httpx.Response(403, json={"error": "forbidden"})

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    client = PipedreamClient(settings=_settings(), client=http)
    req = ActionRequest(
        action_id="act-3",
        tenant_id=uuid4(),
        rep_id=uuid4(),
        action_type=ActionType.EMAIL_SEND,
        target_system="gmail",
        payload={"to": "a@b", "subject": "x", "body_text": "y"},
    )
    with pytest.raises(PipedreamExecutionError) as exc_info:
        await client.dispatch(req)
    await client.close()
    assert exc_info.value.status == 403
    assert exc_info.value.body == {"error": "forbidden"}


@pytest.mark.asyncio
async def test_dry_run_round_trip(captured):
    async def handler(request):
        return httpx.Response(
            200,
            json={
                "valid": True,
                "target_system": "hubspot",
                "target_id": "deal-1",
                "preview": {"stage": "negotiation"},
                "errors": [],
            },
        )

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    client = PipedreamClient(settings=_settings(), client=http)
    response = await client.dry_run(
        DryRunRequest(
            tenant_id=uuid4(),
            rep_id=uuid4(),
            action_type=ActionType.CRM_WRITE,
            target_system="hubspot",
            target_id="deal-1",
            payload={"properties": {"stage": "negotiation"}},
        )
    )
    await client.close()
    assert response.valid is True
    assert response.preview == {"stage": "negotiation"}


@pytest.mark.asyncio
async def test_dispatch_without_base_url_raises():
    client = PipedreamClient(settings=_settings(base_url=""))
    req = ActionRequest(
        action_id="act-x",
        tenant_id=uuid4(),
        rep_id=uuid4(),
        action_type=ActionType.CRM_WRITE,
        target_system="hubspot",
        payload={"properties": {}},
    )
    with pytest.raises(PipedreamConfigError):
        await client.dispatch(req)
    await client.close()
