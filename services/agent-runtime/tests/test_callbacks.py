from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from alex_agent_runtime.db import transactional_session
from alex_agent_runtime.tenant_context import tenant_scope


async def _seed_task(tenant_id: UUID, rep_id: UUID) -> UUID:
    task_id = uuid4()
    with tenant_scope(tenant_id):
        async with transactional_session() as session:
            await session.execute(
                text(
                    "INSERT INTO task_state "
                    "  (id, tenant_id, assignee_rep_id, task_type, status) "
                    "VALUES (:id, :tenant_id, :rep_id, 'draft_followup', 'awaiting_approval')"
                ),
                {
                    "id": str(task_id),
                    "tenant_id": str(tenant_id),
                    "rep_id": str(rep_id),
                },
            )
    return task_id


@pytest.mark.asyncio
async def test_callbacks_approve_transitions_to_completed(client, tenant, rep):
    task_id = await _seed_task(tenant, rep)
    response = await client.post(
        "/callbacks",
        headers={"X-Tenant-Id": str(tenant)},
        json={
            "task_id": str(task_id),
            "rep_id": str(rep),
            "action": "approve",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["new_status"] == "completed"

    with tenant_scope(tenant):
        async with transactional_session() as session:
            status_val = await session.scalar(
                text("SELECT status FROM task_state WHERE id = :id"),
                {"id": str(task_id)},
            )
            audit = await session.scalar(
                text(
                    "SELECT count(*) FROM audit_log "
                    "WHERE action_type = 'approval.approve' "
                    "AND target_id = :id"
                ),
                {"id": str(task_id)},
            )
    assert status_val == "completed"
    assert audit == 1


@pytest.mark.asyncio
async def test_callbacks_unknown_task_returns_404(client, tenant, rep):
    response = await client.post(
        "/callbacks",
        headers={"X-Tenant-Id": str(tenant)},
        json={
            "task_id": str(uuid4()),
            "rep_id": str(rep),
            "action": "approve",
        },
    )
    assert response.status_code == 404
