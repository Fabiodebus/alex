from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from alex_slack_bot.config import Settings
from alex_slack_bot.services.signing import expected_signature


def _payload(**overrides):
    base = {
        "task_id": str(uuid4()),
        "tenant_id": str(uuid4()),
        "rep_id": str(uuid4()),
        "slack_user_id": "U123",
        "title": "Brief: Acme Corp",
        "body": "Stage: Discovery",
        "metadata": {"deal_id": "d-1"},
        "actions": ["approve", "discard"],
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_deliver_renders_and_posts_to_slack(client, app):
    response = await client.post("/deliver", json=_payload())
    assert response.status_code == 202, response.text
    body = response.json()
    fake = app.state.fake_slack_client
    assert len(fake.posted_messages) == 1
    sent = fake.posted_messages[0]
    assert sent["channel"] == fake.dm_channel_id
    # The fallback `text` is the title so notifications work.
    assert "Acme" in sent["text"]
    assert any(b["type"] == "actions" for b in sent["blocks"])
    assert body["channel_id"] == fake.dm_channel_id


@pytest.mark.asyncio
async def test_deliver_opens_im_when_dm_channel_id_not_supplied(client, app):
    await client.post("/deliver", json=_payload())
    fake = app.state.fake_slack_client
    assert fake.opened_users == ["U123"]


@pytest.mark.asyncio
async def test_deliver_skips_open_when_dm_channel_id_supplied(client, app):
    await client.post("/deliver", json=_payload(dm_channel_id="D-prebound"))
    fake = app.state.fake_slack_client
    assert fake.opened_users == []
    assert fake.posted_messages[0]["channel"] == "D-prebound"


@pytest.mark.asyncio
async def test_signature_middleware_rejects_unsigned_when_secret_set(monkeypatch, app, client):
    overridden = Settings(alex_webhook_secret="middleware-test-secret")
    monkeypatch.setattr("alex_slack_bot.config.get_settings", lambda: overridden)
    import alex_slack_bot.middleware as mw
    monkeypatch.setattr(mw, "get_settings", lambda: overridden)

    response = await client.post("/deliver", json=_payload())
    assert response.status_code == 401
    assert response.json()["error"] == "missing_signature"


@pytest.mark.asyncio
async def test_signature_middleware_accepts_signed_request(monkeypatch, app, client):
    secret = "middleware-test-secret"
    overridden = Settings(alex_webhook_secret=secret)
    monkeypatch.setattr("alex_slack_bot.config.get_settings", lambda: overridden)
    import alex_slack_bot.middleware as mw
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
