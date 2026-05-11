"""Integration tests for OutputRouter (Postgres + auto-subscriptions)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from alex_agent_runtime.db import admin_session
from alex_agent_runtime.schemas import (
    ApprovalRequested,
    CRMPlatform,
    CRMWriteFailed,
    DeliveryChannel,
    DeliveryRequest,
    DeliveryStatusValue,
    OutputType,
    TaskExpired,
)
from alex_agent_runtime.services.approval_expiry_scan import TOPIC_APPROVAL_EXPIRED
from alex_agent_runtime.services.approval_gate import TOPIC_APPROVAL_REQUESTED
from alex_agent_runtime.services.crm_writer import TOPIC_CRM_WRITE_FAILED
from alex_agent_runtime.services.delivery_preferences import DeliveryPreferenceRepo
from alex_agent_runtime.services.delivery_tracker import DeliveryTracker
from alex_agent_runtime.services.event_bus import EventBus
from alex_agent_runtime.services.messaging_delivery_client import (
    StubMessagingDeliveryClient,
)
from alex_agent_runtime.services.output_router import OutputRouter, attach_router
from alex_agent_runtime.tenant_context import tenant_scope


def _build_router(escalation_seconds: int = 1800) -> tuple[
    OutputRouter,
    StubMessagingDeliveryClient,
    DeliveryTracker,
    DeliveryPreferenceRepo,
]:
    client = StubMessagingDeliveryClient()
    tracker = DeliveryTracker(escalation_seconds=escalation_seconds)
    prefs = DeliveryPreferenceRepo()
    router = OutputRouter(delivery_client=client, preferences=prefs, tracker=tracker)
    return router, client, tracker, prefs


def _delivery_request(*, tenant_id: UUID, rep_id: UUID, output_id: str | None = None) -> DeliveryRequest:
    return DeliveryRequest(
        tenant_id=tenant_id,
        rep_id=rep_id,
        output_id=output_id or f"out:{uuid4()}",
        output_type=OutputType.NOTIFICATION,
        title="Heads up",
        body="The thing happened.",
    )


@pytest.mark.asyncio
async def test_deliver_records_status_and_calls_client(tenant: UUID, rep: UUID):
    router, client, _tracker, _prefs = _build_router()
    request = _delivery_request(tenant_id=tenant, rep_id=rep, output_id="out-1")

    with tenant_scope(tenant):
        status = await router.deliver(request)

    assert status.status is DeliveryStatusValue.DELIVERED
    assert status.channel is DeliveryChannel.SLACK
    assert len(client.calls) == 1
    channel_sent, attempt_sent = client.calls[0]
    assert channel_sent is DeliveryChannel.SLACK
    assert attempt_sent.title == "Heads up"
    assert attempt_sent.output_id == "out-1"


@pytest.mark.asyncio
async def test_deliver_marks_failed_on_client_error(tenant: UUID, rep: UUID):
    router, client, _tracker, _prefs = _build_router()
    client.fail_next(status=502, message="surface down")
    request = _delivery_request(tenant_id=tenant, rep_id=rep, output_id="out-fail")

    with tenant_scope(tenant):
        status = await router.deliver(request)

    assert status.status is DeliveryStatusValue.FAILED
    assert status.attempt_count == 1
    assert "surface down" in str(status.response)


@pytest.mark.asyncio
async def test_deliver_is_idempotent_on_repeat_output_id(tenant: UUID, rep: UUID):
    router, _client, _tracker, _prefs = _build_router()
    request = _delivery_request(tenant_id=tenant, rep_id=rep, output_id="out-idem")

    with tenant_scope(tenant):
        first = await router.deliver(request)
        second = await router.deliver(request)

    assert first.id == second.id
    # Exactly one row in the table for this output_id.
    async with admin_session() as session:
        count = await session.scalar(
            text(
                "SELECT count(*) FROM delivery_statuses "
                "WHERE tenant_id = :t AND output_id = :o"
            ),
            {"t": str(tenant), "o": "out-idem"},
        )
    assert count == 1


@pytest.mark.asyncio
async def test_crm_native_falls_back_to_slack(tenant: UUID, rep: UUID):
    router, client, _tracker, prefs = _build_router()
    await prefs.set_channel(
        tenant_id=tenant,
        rep_id=rep,
        output_type="notification",
        channel=DeliveryChannel.CRM_NATIVE,
    )
    request = _delivery_request(tenant_id=tenant, rep_id=rep, output_id="out-crm")

    with tenant_scope(tenant):
        status = await router.deliver(request)

    # Fell back to Slack with a fallback_reason in metadata.
    assert status.channel is DeliveryChannel.SLACK
    channel_sent, attempt_sent = client.calls[0]
    assert channel_sent is DeliveryChannel.SLACK
    assert attempt_sent.metadata.get("preferred_channel") == "crm_native"
    assert attempt_sent.metadata.get("fallback_reason") == "crm_native_not_implemented"


@pytest.mark.asyncio
async def test_approval_requested_subscription_fires_delivery(tenant: UUID, rep: UUID):
    router, client, _tracker, _prefs = _build_router()
    bus = EventBus()
    attach_router(bus=bus, router=router)
    task_id = uuid4()

    await bus.publish(
        TOPIC_APPROVAL_REQUESTED,
        ApprovalRequested(
            tenant_id=tenant,
            rep_id=rep,
            task_id=task_id,
            task_type="crm.write",
            title="Update Q4 deal",
            payload={"platform": "hubspot"},
            deadline=datetime.now(timezone.utc) + timedelta(hours=24),
        ),
    )
    assert len(client.calls) == 1
    _channel, attempt = client.calls[0]
    assert attempt.output_id == f"approval:{task_id}"
    assert attempt.task_id == task_id
    assert "approval" in attempt.title.lower() or attempt.title == "Update Q4 deal"


@pytest.mark.asyncio
async def test_crm_write_failed_subscription_renders_notification(tenant: UUID, rep: UUID):
    router, client, _tracker, _prefs = _build_router()
    bus = EventBus()
    attach_router(bus=bus, router=router)
    audit_id = uuid4()

    await bus.publish(
        TOPIC_CRM_WRITE_FAILED,
        CRMWriteFailed(
            tenant_id=tenant,
            rep_id=rep,
            platform=CRMPlatform.HUBSPOT,
            external_id="deal-99",
            field_names=["dealstage"],
            reason="field locked",
            audit_log_id=audit_id,
        ),
    )
    assert len(client.calls) == 1
    _channel, attempt = client.calls[0]
    assert "CRM write failed" in attempt.title
    assert "dealstage" in attempt.body
    assert attempt.metadata["audit_log_id"] == str(audit_id)


@pytest.mark.asyncio
async def test_approval_expired_subscription_notifies_rep(tenant: UUID, rep: UUID):
    router, client, _tracker, _prefs = _build_router()
    bus = EventBus()
    attach_router(bus=bus, router=router)
    task_id = uuid4()
    deadline = datetime.now(timezone.utc) - timedelta(hours=1)

    await bus.publish(
        TOPIC_APPROVAL_EXPIRED,
        TaskExpired(
            tenant_id=tenant,
            rep_id=rep,
            task_id=task_id,
            task_type="crm.write",
            deadline=deadline,
        ),
    )
    assert len(client.calls) == 1
    _channel, attempt = client.calls[0]
    assert attempt.output_id == f"approval_expired:{task_id}"
    assert "expired" in attempt.title.lower()
