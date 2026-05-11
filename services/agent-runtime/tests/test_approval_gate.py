"""Tests for ApprovalGate.create_pending_task."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import text

from alex_agent_runtime.db import admin_session
from alex_agent_runtime.schemas import (
    ApprovalRequested,
    PendingTaskCreate,
    PendingTaskStatus,
)
from alex_agent_runtime.services.approval_gate import (
    TOPIC_APPROVAL_REQUESTED,
    ApprovalGate,
)
from alex_agent_runtime.services.event_bus import EventBus


@pytest.mark.asyncio
async def test_create_pending_task_inserts_row_with_awaiting_approval(
    tenant: UUID, rep: UUID
):
    bus = EventBus()
    seen: list[ApprovalRequested] = []

    async def listener(payload):
        seen.append(payload)

    bus.subscribe(TOPIC_APPROVAL_REQUESTED, listener)
    gate = ApprovalGate(event_bus=bus)

    before = datetime.now(timezone.utc)
    task = await gate.create_pending_task(
        PendingTaskCreate(
            tenant_id=tenant,
            rep_id=rep,
            task_type="crm.write",
            payload={"platform": "hubspot", "external_id": "deal-1"},
            title="Update Q4 deal",
            expires_in_hours=24,
        )
    )
    after = datetime.now(timezone.utc)

    assert task.status is PendingTaskStatus.AWAITING_APPROVAL
    assert task.assignee_rep_id == rep
    assert task.task_type == "crm.write"
    assert before + timedelta(hours=23, minutes=59) <= task.deadline <= after + timedelta(
        hours=24, minutes=1
    )
    # ApprovalRequested fan-out fired with the right payload.
    assert seen and seen[0].task_id == task.task_id
    assert seen[0].title == "Update Q4 deal"

    # Row really exists in task_state.
    async with admin_session() as session:
        row = await session.execute(
            text(
                "SELECT status, task_type, assignee_rep_id, deadline "
                "FROM task_state WHERE id = :id"
            ),
            {"id": str(task.task_id)},
        )
        record = row.mappings().one()
    assert record["status"] == "awaiting_approval"
    assert record["task_type"] == "crm.write"


@pytest.mark.asyncio
async def test_create_pending_task_honours_custom_expiry(tenant: UUID, rep: UUID):
    gate = ApprovalGate(event_bus=EventBus())
    before = datetime.now(timezone.utc)
    task = await gate.create_pending_task(
        PendingTaskCreate(
            tenant_id=tenant,
            rep_id=rep,
            task_type="meeting.brief",
            payload={},
            expires_in_hours=1,
        )
    )
    delta = task.deadline - before
    assert timedelta(minutes=59) <= delta <= timedelta(minutes=61)


@pytest.mark.asyncio
async def test_get_pending_task_returns_persisted_row(tenant: UUID, rep: UUID):
    gate = ApprovalGate(event_bus=EventBus())
    created = await gate.create_pending_task(
        PendingTaskCreate(
            tenant_id=tenant, rep_id=rep, task_type="crm.write", payload={"x": 1}
        )
    )
    fetched = await gate.get_pending_task(tenant_id=tenant, task_id=created.task_id)
    assert fetched is not None
    assert fetched.task_id == created.task_id
    assert fetched.payload == {"x": 1}


@pytest.mark.asyncio
async def test_get_pending_task_returns_none_for_unknown_id(tenant: UUID):
    from uuid import uuid4

    gate = ApprovalGate(event_bus=EventBus())
    fetched = await gate.get_pending_task(tenant_id=tenant, task_id=uuid4())
    assert fetched is None
