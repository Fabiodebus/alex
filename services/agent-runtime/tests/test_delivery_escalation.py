"""Tests for DeliveryEscalationScan + DeliveryTracker.recent_for_rep."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from alex_agent_runtime.db import admin_session
from alex_agent_runtime.schemas import (
    DeliveryChannel,
    DeliveryEscalated,
    DeliveryRequest,
    DeliveryStatusValue,
    OutputType,
)
from alex_agent_runtime.services.delivery_escalation_scan import (
    TOPIC_DELIVERY_ESCALATED,
    DeliveryEscalationScan,
)
from alex_agent_runtime.services.delivery_preferences import DeliveryPreferenceRepo
from alex_agent_runtime.services.delivery_tracker import DeliveryTracker
from alex_agent_runtime.services.event_bus import EventBus
from alex_agent_runtime.services.messaging_delivery_client import (
    StubMessagingDeliveryClient,
)
from alex_agent_runtime.services.output_router import OutputRouter
from alex_agent_runtime.tenant_context import tenant_scope


def _build(escalation_seconds: int) -> tuple[
    OutputRouter, StubMessagingDeliveryClient, DeliveryTracker
]:
    client = StubMessagingDeliveryClient()
    tracker = DeliveryTracker(escalation_seconds=escalation_seconds)
    router = OutputRouter(
        delivery_client=client,
        preferences=DeliveryPreferenceRepo(),
        tracker=tracker,
    )
    return router, client, tracker


async def _force_retry_after(*, tenant_id: UUID, output_id: str, seconds_ago: int) -> None:
    """Drop a row's retry_after into the past so the scan sees it."""
    when = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    async with admin_session() as session:
        await session.execute(
            text(
                "UPDATE delivery_statuses SET retry_after = :r "
                "WHERE tenant_id = :t AND output_id = :o"
            ),
            {"r": when, "t": str(tenant_id), "o": output_id},
        )


@pytest.mark.asyncio
async def test_escalation_flips_pending_and_emits(tenant: UUID, rep: UUID):
    """A delivery whose retry_after is past gets escalated."""
    router, client, tracker = _build(escalation_seconds=1800)
    client.fail_next()  # surface failure → status stays in failed
    request = DeliveryRequest(
        tenant_id=tenant,
        rep_id=rep,
        output_id="out-escalate",
        output_type=OutputType.NOTIFICATION,
        title="t",
        body="b",
    )
    with tenant_scope(tenant):
        status = await router.deliver(request)
    assert status.status is DeliveryStatusValue.FAILED

    await _force_retry_after(tenant_id=tenant, output_id="out-escalate", seconds_ago=120)

    bus = EventBus()
    fired: list[DeliveryEscalated] = []

    async def listener(payload):
        fired.append(payload)

    bus.subscribe(TOPIC_DELIVERY_ESCALATED, listener)
    scan = DeliveryEscalationScan(tracker=tracker, event_bus=bus)
    escalated = await scan.run_once()
    assert escalated >= 1
    assert any(p.output_id == "out-escalate" for p in fired)

    async with admin_session() as session:
        status_val = await session.scalar(
            text(
                "SELECT status FROM delivery_statuses "
                "WHERE tenant_id = :t AND output_id = :o"
            ),
            {"t": str(tenant), "o": "out-escalate"},
        )
    assert status_val == DeliveryStatusValue.ESCALATED.value


@pytest.mark.asyncio
async def test_escalation_ignores_delivered_rows(tenant: UUID, rep: UUID):
    """A successfully delivered row stays put even if its retry_after is past."""
    router, _client, tracker = _build(escalation_seconds=1800)
    request = DeliveryRequest(
        tenant_id=tenant,
        rep_id=rep,
        output_id="out-already-delivered",
        output_type=OutputType.NOTIFICATION,
        title="t",
        body="b",
    )
    with tenant_scope(tenant):
        await router.deliver(request)
    await _force_retry_after(
        tenant_id=tenant, output_id="out-already-delivered", seconds_ago=120
    )

    bus = EventBus()
    seen: list = []

    async def listener(payload):
        seen.append(payload)

    bus.subscribe(TOPIC_DELIVERY_ESCALATED, listener)
    scan = DeliveryEscalationScan(tracker=tracker, event_bus=bus)
    await scan.run_once()
    # This output isn't in the escalation set.
    assert not any(p.output_id == "out-already-delivered" for p in seen)


@pytest.mark.asyncio
async def test_recent_for_rep_returns_recent_rows(tenant: UUID, rep: UUID):
    router, _client, tracker = _build(escalation_seconds=1800)
    for i in range(3):
        with tenant_scope(tenant):
            await router.deliver(
                DeliveryRequest(
                    tenant_id=tenant,
                    rep_id=rep,
                    output_id=f"out-recent-{i}",
                    output_type=OutputType.NOTIFICATION,
                    title=f"t{i}",
                    body="b",
                )
            )
    rows = await tracker.recent_for_rep(tenant_id=tenant, rep_id=rep, limit=10)
    output_ids = {r.output_id for r in rows}
    assert {"out-recent-0", "out-recent-1", "out-recent-2"} <= output_ids
    assert all(r.rep_id == rep for r in rows)
