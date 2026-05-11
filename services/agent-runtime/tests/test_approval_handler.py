"""Integration tests for the refactored ApprovalHandler."""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from alex_agent_runtime.db import admin_session, transactional_session
from alex_agent_runtime.schemas import (
    ApprovalCallback,
    CallbackAction,
    EditDiff,
    PendingTaskCreate,
    TaskApproved,
    TaskDiscarded,
)
from alex_agent_runtime.services.approval_gate import ApprovalGate
from alex_agent_runtime.services.approval_handler import (
    TOPIC_APPROVAL_APPROVED,
    TOPIC_APPROVAL_DISCARDED,
    ApprovalHandler,
    ApprovalScopingError,
    TaskAlreadyActionedError,
    TaskNotFoundError,
)
from alex_agent_runtime.services.event_bus import EventBus
from alex_agent_runtime.tenant_context import tenant_scope


async def _seed_other_rep(tenant_id: UUID) -> UUID:
    """Insert a second rep on the tenant so we can test scoping."""
    rep_id = uuid4()
    with tenant_scope(tenant_id):
        async with transactional_session() as session:
            await session.execute(
                text(
                    "INSERT INTO reps (id, tenant_id, email, display_name) "
                    "VALUES (:id, :tenant_id, :email, :name)"
                ),
                {
                    "id": str(rep_id),
                    "tenant_id": str(tenant_id),
                    "email": f"other-{rep_id}@example.com",
                    "name": "Other Rep",
                },
            )
    return rep_id


async def _gate_task(*, tenant: UUID, rep: UUID, payload: dict | None = None) -> UUID:
    gate = ApprovalGate(event_bus=EventBus())
    task = await gate.create_pending_task(
        PendingTaskCreate(
            tenant_id=tenant,
            rep_id=rep,
            task_type="crm.write",
            payload=payload or {"platform": "hubspot", "external_id": "deal-1"},
        )
    )
    return task.task_id


@pytest.mark.asyncio
async def test_handle_approve_publishes_task_approved_and_audits(
    tenant: UUID, rep: UUID
):
    task_id = await _gate_task(tenant=tenant, rep=rep)
    bus = EventBus()
    seen: list[TaskApproved] = []

    async def listener(payload):
        seen.append(payload)

    bus.subscribe(TOPIC_APPROVAL_APPROVED, listener)
    handler = ApprovalHandler(event_bus=bus)

    with tenant_scope(tenant):
        result = await handler.handle(
            ApprovalCallback(task_id=task_id, rep_id=rep, action=CallbackAction.APPROVE)
        )
    assert result.outcome == "approved"
    assert result.dispatched is True
    assert seen and seen[0].task_id == task_id
    assert seen[0].edit_diff is None

    # task_state flipped + audit row exists.
    async with admin_session() as session:
        status_val = await session.scalar(
            text("SELECT status FROM task_state WHERE id = :id"), {"id": str(task_id)}
        )
        audit_count = await session.scalar(
            text(
                "SELECT count(*) FROM audit_log "
                "WHERE action_type = 'approval.approved' AND target_id = :id"
            ),
            {"id": str(task_id)},
        )
    assert status_val == "completed"
    assert audit_count == 1


@pytest.mark.asyncio
async def test_handle_edit_emits_edit_diff(tenant: UUID, rep: UUID):
    original = {"platform": "hubspot", "external_id": "deal-1", "stage": "qualification"}
    edited = {"platform": "hubspot", "external_id": "deal-1", "stage": "presentation"}
    task_id = await _gate_task(tenant=tenant, rep=rep, payload=original)
    bus = EventBus()
    seen: list[TaskApproved] = []

    async def listener(payload):
        seen.append(payload)

    bus.subscribe(TOPIC_APPROVAL_APPROVED, listener)
    handler = ApprovalHandler(event_bus=bus)

    with tenant_scope(tenant):
        result = await handler.handle(
            ApprovalCallback(
                task_id=task_id,
                rep_id=rep,
                action=CallbackAction.EDIT,
                edited_output=edited,
            )
        )
    assert result.outcome == "edited"
    assert seen and isinstance(seen[0].edit_diff, EditDiff)
    diff = seen[0].edit_diff
    assert diff.before == original
    assert diff.after == edited
    assert seen[0].payload == edited


@pytest.mark.asyncio
async def test_handle_discard_publishes_task_discarded(tenant: UUID, rep: UUID):
    task_id = await _gate_task(tenant=tenant, rep=rep)
    bus = EventBus()
    discarded: list[TaskDiscarded] = []
    approved: list[TaskApproved] = []

    async def discard_listener(payload):
        discarded.append(payload)

    async def approve_listener(payload):
        approved.append(payload)

    bus.subscribe(TOPIC_APPROVAL_DISCARDED, discard_listener)
    bus.subscribe(TOPIC_APPROVAL_APPROVED, approve_listener)
    handler = ApprovalHandler(event_bus=bus)

    with tenant_scope(tenant):
        result = await handler.handle(
            ApprovalCallback(
                task_id=task_id,
                rep_id=rep,
                action=CallbackAction.DISCARD,
                feedback="not relevant",
            )
        )
    assert result.outcome == "discarded"
    assert approved == []
    assert discarded and discarded[0].feedback == "not relevant"


@pytest.mark.asyncio
async def test_handle_rejects_other_rep(tenant: UUID, rep: UUID):
    other_rep = await _seed_other_rep(tenant)
    task_id = await _gate_task(tenant=tenant, rep=rep)
    handler = ApprovalHandler(event_bus=EventBus())

    with tenant_scope(tenant):
        with pytest.raises(ApprovalScopingError):
            await handler.handle(
                ApprovalCallback(
                    task_id=task_id, rep_id=other_rep, action=CallbackAction.APPROVE
                )
            )

    # Task should still be open.
    async with admin_session() as session:
        status_val = await session.scalar(
            text("SELECT status FROM task_state WHERE id = :id"), {"id": str(task_id)}
        )
    assert status_val == "awaiting_approval"


@pytest.mark.asyncio
async def test_handle_rejects_double_action(tenant: UUID, rep: UUID):
    task_id = await _gate_task(tenant=tenant, rep=rep)
    handler = ApprovalHandler(event_bus=EventBus())

    with tenant_scope(tenant):
        await handler.handle(
            ApprovalCallback(task_id=task_id, rep_id=rep, action=CallbackAction.APPROVE)
        )
        with pytest.raises(TaskAlreadyActionedError):
            await handler.handle(
                ApprovalCallback(
                    task_id=task_id, rep_id=rep, action=CallbackAction.DISCARD
                )
            )


@pytest.mark.asyncio
async def test_handle_unknown_task_raises_not_found(tenant: UUID, rep: UUID):
    handler = ApprovalHandler(event_bus=EventBus())
    with tenant_scope(tenant):
        with pytest.raises(TaskNotFoundError):
            await handler.handle(
                ApprovalCallback(
                    task_id=uuid4(), rep_id=rep, action=CallbackAction.APPROVE
                )
            )


@pytest.mark.asyncio
async def test_handle_feedback_keeps_task_open(tenant: UUID, rep: UUID):
    task_id = await _gate_task(tenant=tenant, rep=rep)
    bus = EventBus()
    approved: list[TaskApproved] = []

    async def listener(payload):
        approved.append(payload)

    bus.subscribe(TOPIC_APPROVAL_APPROVED, listener)
    handler = ApprovalHandler(event_bus=bus)

    with tenant_scope(tenant):
        result = await handler.handle(
            ApprovalCallback(
                task_id=task_id,
                rep_id=rep,
                action=CallbackAction.FEEDBACK,
                feedback="please retry with EUR",
            )
        )
    assert result.outcome == "feedback"
    assert result.new_status == "awaiting_approval"
    assert approved == []  # no dispatch event for feedback


@pytest.mark.asyncio
async def test_callbacks_route_returns_403_for_scoping_violation(
    client, tenant, rep
):
    other_rep = await _seed_other_rep(tenant)
    task_id = await _gate_task(tenant=tenant, rep=rep)
    response = await client.post(
        "/callbacks",
        headers={"X-Tenant-Id": str(tenant)},
        json={"task_id": str(task_id), "rep_id": str(other_rep), "action": "approve"},
    )
    assert response.status_code == 403
