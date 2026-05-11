"""Tests for ApprovalExpiryScan."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import text

from alex_agent_runtime.db import admin_session
from alex_agent_runtime.schemas import (
    PendingTaskCreate,
    PendingTaskStatus,
    TaskExpired,
)
from alex_agent_runtime.services.approval_expiry_scan import (
    TOPIC_APPROVAL_EXPIRED,
    ApprovalExpiryScan,
)
from alex_agent_runtime.services.approval_gate import ApprovalGate
from alex_agent_runtime.services.event_bus import EventBus


async def _force_deadline(task_id: UUID, *, hours_ago: int) -> None:
    """Move a task's deadline into the past so the scan sees it."""
    new_deadline = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    async with admin_session() as session:
        await session.execute(
            text("UPDATE task_state SET deadline = :d WHERE id = :id"),
            {"d": new_deadline, "id": str(task_id)},
        )


@pytest.mark.asyncio
async def test_scan_flips_overdue_task_and_emits_expired(tenant: UUID, rep: UUID):
    bus = EventBus()
    seen: list[TaskExpired] = []

    async def listener(payload):
        seen.append(payload)

    bus.subscribe(TOPIC_APPROVAL_EXPIRED, listener)

    gate = ApprovalGate(event_bus=EventBus())  # separate bus — we don't care about its events
    task = await gate.create_pending_task(
        PendingTaskCreate(
            tenant_id=tenant, rep_id=rep, task_type="crm.write", payload={}
        )
    )
    await _force_deadline(task.task_id, hours_ago=1)

    scan = ApprovalExpiryScan(event_bus=bus)
    expired = await scan.run_once()
    assert expired >= 1
    assert any(p.task_id == task.task_id for p in seen)

    async with admin_session() as session:
        status_val = await session.scalar(
            text("SELECT status FROM task_state WHERE id = :id"),
            {"id": str(task.task_id)},
        )
        audit_count = await session.scalar(
            text(
                "SELECT count(*) FROM audit_log "
                "WHERE action_type = 'approval.expired' AND target_id = :id"
            ),
            {"id": str(task.task_id)},
        )
    assert status_val == PendingTaskStatus.EXPIRED.value
    assert audit_count == 1


@pytest.mark.asyncio
async def test_scan_ignores_future_deadlines(tenant: UUID, rep: UUID):
    gate = ApprovalGate(event_bus=EventBus())
    task = await gate.create_pending_task(
        PendingTaskCreate(
            tenant_id=tenant,
            rep_id=rep,
            task_type="crm.write",
            payload={},
            expires_in_hours=24,
        )
    )

    bus = EventBus()
    seen: list = []

    async def listener(payload):
        seen.append(payload)

    bus.subscribe(TOPIC_APPROVAL_EXPIRED, listener)
    scan = ApprovalExpiryScan(event_bus=bus)
    # Don't move the deadline.
    expired = await scan.run_once()
    # Whatever expired count comes back, this task isn't in it.
    assert not any(p.task_id == task.task_id for p in seen)

    async with admin_session() as session:
        status_val = await session.scalar(
            text("SELECT status FROM task_state WHERE id = :id"),
            {"id": str(task.task_id)},
        )
    assert status_val == PendingTaskStatus.AWAITING_APPROVAL.value
    _ = expired


@pytest.mark.asyncio
async def test_scan_is_idempotent_across_runs(tenant: UUID, rep: UUID):
    """A task expired once shouldn't expire again on the next tick."""
    gate = ApprovalGate(event_bus=EventBus())
    task = await gate.create_pending_task(
        PendingTaskCreate(
            tenant_id=tenant, rep_id=rep, task_type="crm.write", payload={}
        )
    )
    await _force_deadline(task.task_id, hours_ago=2)

    bus = EventBus()
    fires: list = []

    async def listener(payload):
        fires.append(payload)

    bus.subscribe(TOPIC_APPROVAL_EXPIRED, listener)
    scan = ApprovalExpiryScan(event_bus=bus)
    first = await scan.run_once()
    second = await scan.run_once()
    matching = [p for p in fires if p.task_id == task.task_id]
    assert first >= 1
    assert len(matching) == 1
    # The second tick won't re-fire this task; whatever it returns
    # comes from other tests' fixtures in the same DB.
    _ = second
