"""ApprovalGate — holds every agent output as a PendingTask awaiting rep action.

The blueprint's first contract: no external write happens without a
correlated ``ApprovalCallback`` carrying ``approved`` or
``edited_and_approved``. :class:`ApprovalGate` is the entry point — a
feature workflow that wants Alex to take an external action calls
:meth:`create_pending_task` instead of dispatching directly. The
returned ``task_id`` correlates the eventual rep response.

Per the WO scoping choice: ``task_state`` rows back the pending tasks
(existing table, status='awaiting_approval'). Deadline defaults to
``now + 24h`` per the blueprint's named example for CRM-update
proposals; callers can override via ``expires_in_hours``.

On insert the gate publishes ``approval.requested`` on the
:class:`EventBus`. Notification Delivery (WO #13) subscribes and routes
the approval card to the rep's preferred channel.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import UUID

import structlog
from sqlalchemy import text

from ..db import transactional_session
from ..schemas import (
    ApprovalRequested,
    PendingTask,
    PendingTaskCreate,
    PendingTaskStatus,
)
from ..tenant_context import tenant_scope
from .event_bus import EventBus

log = structlog.get_logger(__name__)


TOPIC_APPROVAL_REQUESTED = "approval.requested"


class ApprovalGateError(RuntimeError):
    pass


class ApprovalGate:
    def __init__(self, *, event_bus: EventBus) -> None:
        self._event_bus = event_bus

    async def create_pending_task(self, request: PendingTaskCreate) -> PendingTask:
        deadline = _now() + timedelta(hours=request.expires_in_hours)
        payload = dict(request.payload)
        if request.title is not None:
            payload.setdefault("_meta", {})["title"] = request.title

        with tenant_scope(request.tenant_id):
            async with transactional_session() as session:
                row = await session.execute(
                    text(
                        """
                        INSERT INTO task_state (
                            tenant_id,
                            parent_task_id,
                            assignee_rep_id,
                            task_type,
                            status,
                            payload,
                            scheduled_for,
                            deadline
                        ) VALUES (
                            current_setting('app.tenant_id')::uuid,
                            :parent_task_id,
                            :assignee_rep_id,
                            :task_type,
                            'awaiting_approval',
                            CAST(:payload AS jsonb),
                            now(),
                            :deadline
                        )
                        RETURNING id, status, payload, result, deadline, created_at, updated_at
                        """
                    ),
                    {
                        "parent_task_id": request.parent_task_id,
                        "assignee_rep_id": request.rep_id,
                        "task_type": request.task_type,
                        "payload": json.dumps(payload, default=str),
                        "deadline": deadline,
                    },
                )
                inserted = row.mappings().one()

        task = PendingTask(
            task_id=inserted["id"],
            tenant_id=request.tenant_id,
            assignee_rep_id=request.rep_id,
            task_type=request.task_type,
            status=PendingTaskStatus(inserted["status"]),
            payload=inserted["payload"] or {},
            result=inserted["result"],
            deadline=inserted["deadline"],
            created_at=inserted["created_at"],
            updated_at=inserted["updated_at"],
        )

        await self._event_bus.publish(
            TOPIC_APPROVAL_REQUESTED,
            ApprovalRequested(
                tenant_id=request.tenant_id,
                rep_id=request.rep_id,
                task_id=task.task_id,
                task_type=request.task_type,
                title=request.title,
                payload=request.payload,
                deadline=task.deadline,
            ),
        )
        log.info(
            "approval_gate.pending_task_created",
            task_id=str(task.task_id),
            task_type=request.task_type,
            rep_id=str(request.rep_id),
            deadline=task.deadline.isoformat(),
        )
        return task

    async def get_pending_task(self, *, tenant_id: UUID, task_id: UUID) -> PendingTask | None:
        with tenant_scope(tenant_id):
            async with transactional_session() as session:
                row = await session.execute(
                    text(
                        """
                        SELECT id, assignee_rep_id, task_type, status, payload,
                               result, deadline, created_at, updated_at
                          FROM task_state
                         WHERE id = :task_id
                           AND tenant_id = current_setting('app.tenant_id')::uuid
                        """
                    ),
                    {"task_id": str(task_id)},
                )
                record = row.mappings().one_or_none()

        if record is None:
            return None
        try:
            status = PendingTaskStatus(record["status"])
        except ValueError as exc:
            # task_state allows other statuses (pending, in_progress, failed)
            # that ApprovalGate never produces. Surface loudly rather than
            # silently coercing.
            raise ApprovalGateError(
                f"task {task_id} has non-approval status {record['status']!r}"
            ) from exc
        return PendingTask(
            task_id=record["id"],
            tenant_id=tenant_id,
            assignee_rep_id=record["assignee_rep_id"],
            task_type=record["task_type"],
            status=status,
            payload=record["payload"] or {},
            result=record["result"],
            deadline=record["deadline"],
            created_at=record["created_at"],
            updated_at=record["updated_at"],
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)
