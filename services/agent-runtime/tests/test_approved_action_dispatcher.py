"""Integration tests for ApprovedActionDispatcher (crm.write path)."""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from alex_agent_runtime.schemas import (
    CRMPlatform,
    CRMRecordKind,
    CRMWriteRequest,
    CRMWriteResult,
    CRMWriteStatus,
    TaskApproved,
)
from alex_agent_runtime.services.approved_action_dispatcher import (
    ApprovedActionDispatcher,
    attach_dispatcher,
)
from alex_agent_runtime.services.approval_handler import TOPIC_APPROVAL_APPROVED
from alex_agent_runtime.services.crm_validator import CRMValidator
from alex_agent_runtime.services.crm_writer import CRMWriter
from alex_agent_runtime.services.event_bus import EventBus
from alex_agent_runtime.tenant_context import tenant_scope


class _RecordingWriteClient:
    name = "test-recording"

    def __init__(self) -> None:
        self.calls: list[CRMWriteRequest] = []

    async def write(self, request: CRMWriteRequest) -> CRMWriteResult:
        self.calls.append(request)
        return CRMWriteResult(
            status=CRMWriteStatus.SUCCEEDED,
            platform=request.platform,
            external_id=request.external_id,
            succeeded_fields=[u.update.field_name for u in request.field_updates],
        )


@pytest.mark.asyncio
async def test_dispatcher_routes_crm_write_to_writer(tenant: UUID, rep: UUID):
    client = _RecordingWriteClient()
    writer = CRMWriter(write_client=client)
    dispatcher = ApprovedActionDispatcher(crm_writer=writer, crm_validator=CRMValidator())
    bus = EventBus()
    attach_dispatcher(bus=bus, dispatcher=dispatcher)

    event = TaskApproved(
        tenant_id=tenant,
        rep_id=rep,
        task_id=uuid4(),
        task_type="crm.write",
        payload={
            "platform": CRMPlatform.HUBSPOT.value,
            "kind": CRMRecordKind.OPPORTUNITY.value,
            "external_id": "deal-dispatch-1",
            "field_updates": [
                {
                    "field_name": "dealstage",
                    "current_value": "qualification",
                    "proposed_value": "Presentation",
                },
            ],
        },
    )
    with tenant_scope(tenant):
        await bus.publish(TOPIC_APPROVAL_APPROVED, event)

    assert len(client.calls) == 1
    sent = client.calls[0]
    assert sent.field_updates[0].update.field_name == "dealstage"
    assert sent.field_updates[0].normalized_value == "presentation"
    assert sent.idempotency_key == f"task:{event.task_id}"


@pytest.mark.asyncio
async def test_dispatcher_drops_invalid_field_updates(tenant: UUID, rep: UUID):
    """A bad post-edit value is rejected here rather than reaching CRMWriter."""
    client = _RecordingWriteClient()
    writer = CRMWriter(write_client=client)
    dispatcher = ApprovedActionDispatcher(crm_writer=writer, crm_validator=CRMValidator())
    bus = EventBus()
    attach_dispatcher(bus=bus, dispatcher=dispatcher)

    event = TaskApproved(
        tenant_id=tenant,
        rep_id=rep,
        task_id=uuid4(),
        task_type="crm.write",
        payload={
            "platform": CRMPlatform.HUBSPOT.value,
            "kind": CRMRecordKind.OPPORTUNITY.value,
            "external_id": "deal-dispatch-2",
            "field_updates": [
                # Two updates: one valid, one invalid enum.
                {
                    "field_name": "dealstage",
                    "current_value": "qualification",
                    "proposed_value": "presentation",
                },
                {
                    "field_name": "dealstage",
                    "current_value": "qualification",
                    "proposed_value": "GarbageStage",
                },
            ],
        },
    )
    with tenant_scope(tenant):
        await bus.publish(TOPIC_APPROVAL_APPROVED, event)

    # Only the valid update reaches the writer.
    assert len(client.calls) == 1
    assert len(client.calls[0].field_updates) == 1
    assert client.calls[0].field_updates[0].update.field_name == "dealstage"


@pytest.mark.asyncio
async def test_dispatcher_logs_and_drops_unknown_task_type(tenant: UUID, rep: UUID):
    client = _RecordingWriteClient()
    dispatcher = ApprovedActionDispatcher(
        crm_writer=CRMWriter(write_client=client), crm_validator=CRMValidator()
    )
    bus = EventBus()
    attach_dispatcher(bus=bus, dispatcher=dispatcher)

    await bus.publish(
        TOPIC_APPROVAL_APPROVED,
        TaskApproved(
            tenant_id=tenant,
            rep_id=rep,
            task_id=uuid4(),
            task_type="email.send",  # not implemented yet
            payload={"to": "someone@example.com", "body": "hi"},
        ),
    )
    assert client.calls == []
