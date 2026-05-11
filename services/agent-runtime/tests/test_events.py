from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from alex_agent_runtime.db import admin_session, transactional_session
from alex_agent_runtime.tenant_context import tenant_scope


def _payload(event_id: str = "evt-1") -> dict:
    return {
        "event_id": event_id,
        "source": "pipedream",
        "kind": "calendar.meeting_detected",
        "occurred_at": "2026-05-08T12:00:00Z",
        "payload": {"meeting_id": "abc"},
    }


@pytest.mark.asyncio
async def test_events_requires_tenant_header(client):
    response = await client.post("/events", json=_payload())
    assert response.status_code == 400
    assert response.json()["error"] == "missing_tenant_header"


@pytest.mark.asyncio
async def test_events_persists_and_audits(client, tenant: UUID):
    response = await client.post(
        "/events",
        json=_payload(),
        headers={"X-Tenant-Id": str(tenant)},
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["accepted"] is True
    assert body["deduplicated"] is False

    with tenant_scope(tenant):
        async with transactional_session() as session:
            event_count = await session.scalar(
                text("SELECT count(*) FROM processed_events WHERE event_id = 'evt-1'")
            )
            audit_count = await session.scalar(
                text(
                    "SELECT count(*) FROM audit_log "
                    "WHERE action_type = 'event.received' "
                    "AND metadata->>'event_id' = 'evt-1'"
                )
            )
    assert event_count == 1
    assert audit_count == 1


@pytest.mark.asyncio
async def test_events_deduplicates(client, tenant: UUID):
    headers = {"X-Tenant-Id": str(tenant)}
    first = await client.post("/events", json=_payload("dup-1"), headers=headers)
    second = await client.post("/events", json=_payload("dup-1"), headers=headers)

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["deduplicated"] is True

    with tenant_scope(tenant):
        async with transactional_session() as session:
            count = await session.scalar(
                text(
                    "SELECT count(*) FROM processed_events WHERE event_id = 'dup-1'"
                )
            )
    assert count == 1


@pytest.mark.asyncio
async def test_events_dedup_is_per_tenant(client, tenant: UUID):
    """Same event_id from two tenants is NOT a duplicate — the composite
    PK on (tenant_id, event_id) is what makes idempotency tenant-scoped.

    Note: a strict cross-tenant invisibility test (RLS) requires the
    application to connect as a non-owner role; that posture is verified
    end-to-end in WO #1's data-layer manual suite, not here.
    """
    other_tenant = uuid4()
    async with admin_session() as session:
        await session.execute(
            text("INSERT INTO tenants (id, name) VALUES (:id, 'other')"),
            {"id": str(other_tenant)},
        )

    try:
        a = await client.post(
            "/events",
            json=_payload("shared-id"),
            headers={"X-Tenant-Id": str(tenant)},
        )
        b = await client.post(
            "/events",
            json=_payload("shared-id"),
            headers={"X-Tenant-Id": str(other_tenant)},
        )
        assert a.status_code == 202
        assert b.status_code == 202
        assert a.json()["deduplicated"] is False
        assert b.json()["deduplicated"] is False

        with tenant_scope(tenant):
            async with transactional_session() as session:
                rows = await session.scalar(
                    text(
                        "SELECT count(*) FROM processed_events "
                        "WHERE event_id = 'shared-id' AND tenant_id = :tid"
                    ),
                    {"tid": str(tenant)},
                )
        assert rows == 1
    finally:
        async with admin_session(allow_audit_purge=True) as session:
            await session.execute(
                text("DELETE FROM tenants WHERE id = :id"),
                {"id": str(other_tenant)},
            )
