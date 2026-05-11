"""Tests for the proactive /deliver route and the signing middleware."""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from alex_teams_bot.config import Settings
from alex_teams_bot.services.signing import expected_signature


class FakeAdapter:
    """Mocks CloudAdapter.continue_conversation enough for /deliver to run."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def continue_conversation(self, reference, callback, bot_id):
        self.calls.append(
            {
                "reference": reference,
                "bot_id": bot_id,
                "callback": callback,
            }
        )
        # Invoke the callback with a fake TurnContext so we exercise the
        # MessageFactory + render path.
        await callback(_FakeTurnContext())


class _FakeTurnContext:
    async def send_activity(self, _message):
        from types import SimpleNamespace
        return SimpleNamespace(id="msg-1")


@pytest_asyncio.fixture
async def app() -> AsyncIterator[Any]:
    from alex_teams_bot.main import create_app

    fastapi_app = create_app()
    async with fastapi_app.router.lifespan_context(fastapi_app):
        # Swap in the fake adapter so /deliver doesn't try to talk to Azure.
        fastapi_app.state.adapter = FakeAdapter()
        yield fastapi_app


@pytest_asyncio.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _payload(**overrides):
    base = {
        "task_id": str(uuid4()),
        "tenant_id": str(uuid4()),
        "rep_id": str(uuid4()),
        "conversation_reference": {
            "channel_id": "msteams",
            "service_url": "https://smba.trafficmanager.net/emea/",
            "conversation": {"id": "19:abc@thread.tacv2"},
            "bot": {"id": "28:bot-app-id", "name": "Alex"},
            "user": {"id": "29:user-id", "name": "Alice"},
        },
        "title": "Brief: Acme",
        "body": "Stage: Discovery",
        "metadata": {"deal_id": "d-1"},
        "actions": ["approve", "discard"],
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_deliver_invokes_continue_conversation(client, app):
    response = await client.post("/deliver", json=_payload())
    assert response.status_code == 202, response.text
    fake: FakeAdapter = app.state.adapter
    assert len(fake.calls) == 1
    # Bot id passed through (empty in dev — Settings default).
    assert fake.calls[0]["bot_id"] in (None, "")


@pytest.mark.asyncio
async def test_signature_middleware_rejects_unsigned_when_secret_set(monkeypatch, client):
    overridden = Settings(alex_webhook_secret="middleware-test-secret")
    monkeypatch.setattr("alex_teams_bot.config.get_settings", lambda: overridden)
    import alex_teams_bot.middleware as mw
    monkeypatch.setattr(mw, "get_settings", lambda: overridden)
    response = await client.post("/deliver", json=_payload())
    assert response.status_code == 401
    assert response.json()["error"] == "missing_signature"


@pytest.mark.asyncio
async def test_signature_middleware_accepts_signed_request(monkeypatch, client):
    secret = "middleware-test-secret"
    overridden = Settings(alex_webhook_secret=secret)
    monkeypatch.setattr("alex_teams_bot.config.get_settings", lambda: overridden)
    import alex_teams_bot.middleware as mw
    monkeypatch.setattr(mw, "get_settings", lambda: overridden)
    body = json.dumps(_payload()).encode("utf-8")
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    sig = expected_signature(secret=secret, timestamp=ts, body=body)
    response = await client.post(
        "/deliver",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Alex-Signature": sig,
            "X-Alex-Timestamp": ts,
        },
    )
    assert response.status_code == 202, response.text
