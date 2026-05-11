"""HTTP-level integration tests for /onboarding routes."""
from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import text

from alex_agent_runtime.db import admin_session


@pytest.mark.asyncio
async def test_start_for_slack_user_auto_provisions_rep(client, tenant: UUID):
    response = await client.post(
        "/onboarding/start_for_slack_user",
        headers={"X-Tenant-Id": str(tenant)},
        json={
            "slack_user_id": "U01TEST",
            "slack_team_id": "T01TEST",
            "slack_display_name": "Test Rep",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["current_step"] in {"welcome", "connect_close"}
    rep_id = body["rep_id"]

    async with admin_session() as session:
        rep_exists = await session.scalar(
            text("SELECT count(*) FROM reps WHERE id = :id"), {"id": rep_id}
        )
        identity_exists = await session.scalar(
            text(
                "SELECT count(*) FROM messaging_identities "
                "WHERE rep_id = :id AND platform = 'slack' AND external_user_id = :u"
            ),
            {"id": rep_id, "u": "U01TEST"},
        )
    assert rep_exists == 1
    assert identity_exists == 1


@pytest.mark.asyncio
async def test_start_for_slack_user_reuses_existing_identity(client, tenant: UUID):
    """Second call with the same Slack user_id should hit the existing rep."""
    first = await client.post(
        "/onboarding/start_for_slack_user",
        headers={"X-Tenant-Id": str(tenant)},
        json={"slack_user_id": "U02REUSE", "slack_team_id": "T0X"},
    )
    second = await client.post(
        "/onboarding/start_for_slack_user",
        headers={"X-Tenant-Id": str(tenant)},
        json={"slack_user_id": "U02REUSE", "slack_team_id": "T0X"},
    )
    assert first.json()["rep_id"] == second.json()["rep_id"]


@pytest.mark.asyncio
async def test_oauth_initiate_returns_stub_url(client, tenant: UUID):
    start = await client.post(
        "/onboarding/start_for_slack_user",
        headers={"X-Tenant-Id": str(tenant)},
        json={"slack_user_id": "U03INIT"},
    )
    rep_id = start.json()["rep_id"]
    response = await client.post(
        "/onboarding/oauth/initiate",
        headers={"X-Tenant-Id": str(tenant)},
        json={"rep_id": rep_id, "connector": "close"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["stub"] is True
    assert body["connector"] == "close"
    assert "stub_complete" in body["authorize_url"]
    assert "state" in body and len(body["state"]) >= 8


@pytest.mark.asyncio
async def test_stub_complete_advances_state(client, tenant: UUID):
    start = await client.post(
        "/onboarding/start_for_slack_user",
        headers={"X-Tenant-Id": str(tenant)},
        json={"slack_user_id": "U04STUB"},
    )
    rep_id = start.json()["rep_id"]
    initiate = await client.post(
        "/onboarding/oauth/initiate",
        headers={"X-Tenant-Id": str(tenant)},
        json={"rep_id": rep_id, "connector": "close"},
    )
    state = initiate.json()["state"]
    response = await client.get(
        "/onboarding/oauth/stub_complete",
        headers={"X-Tenant-Id": str(tenant)},
        params={
            "rep_id": rep_id,
            "state": state,
            "connector": "close",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    assert body["connector"] == "close"

    # The flow has advanced to CONNECT_GOOGLE.
    async with admin_session() as session:
        step = await session.scalar(
            text("SELECT current_step FROM onboarding_state WHERE rep_id = :id"),
            {"id": rep_id},
        )
    assert step == "connect_google"


@pytest.mark.asyncio
async def test_skip_route_marks_optional_connector(client, tenant: UUID):
    start = await client.post(
        "/onboarding/start_for_slack_user",
        headers={"X-Tenant-Id": str(tenant)},
        json={"slack_user_id": "U05SKIP"},
    )
    rep_id = start.json()["rep_id"]
    # Walk to Krisp step by completing close + google.
    for connector in ("close", "google"):
        initiate = await client.post(
            "/onboarding/oauth/initiate",
            headers={"X-Tenant-Id": str(tenant)},
            json={"rep_id": rep_id, "connector": connector},
        )
        await client.get(
            "/onboarding/oauth/stub_complete",
            headers={"X-Tenant-Id": str(tenant)},
            params={
                "rep_id": rep_id,
                "state": initiate.json()["state"],
                "connector": connector,
            },
        )

    skip_response = await client.post(
        "/onboarding/skip",
        headers={"X-Tenant-Id": str(tenant)},
        json={"rep_id": rep_id, "connector": "krisp"},
    )
    assert skip_response.status_code == 200
    assert skip_response.json()["current_step"] == "ingesting"
