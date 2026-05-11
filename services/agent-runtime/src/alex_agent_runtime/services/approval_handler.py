"""``/callbacks`` core: rep approval/edit/discard/feedback handling.

Blueprint contracts enforced here:

* **Approval scoping** — only the rep who owns the task may approve it.
  A callback whose ``rep_id`` doesn't match ``task_state.assignee_rep_id``
  is rejected with :class:`ApprovalScopingError`.
* **Single-use** — a task already in a terminal state (completed,
  cancelled, expired) cannot be re-actioned. Re-submissions raise
  :class:`TaskAlreadyActionedError`.
* **Audit-first** — every outcome (approved, edited, discarded,
  feedback) is written to ``audit_log`` *before* any downstream
  dispatch event is published.

Routing approved tasks is intentionally decoupled: this handler emits
``approval.approved`` on the :class:`EventBus` and lets the
:class:`ApprovedActionDispatcher` map the ``task_type`` to a concrete
executor (CRMWriter for ``crm.write``, future EmailDispatcher for
``email.send``, …). The handler itself knows nothing about CRMs or
emails.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import text

from ..db import transactional_session
from ..schemas import (
    ApprovalCallback,
    AuditLogEntry,
    CallbackAction,
    EditDiff,
    PendingTaskStatus,
    TaskApproved,
    TaskDiscarded,
)
from ..tenant_context import current_tenant_or_none, tenant_scope
from .audit_log import record_action
from .event_bus import EventBus

log = structlog.get_logger(__name__)


TOPIC_APPROVAL_APPROVED = "approval.approved"
TOPIC_APPROVAL_DISCARDED = "approval.discarded"


@dataclass(slots=True)
class ApprovalResult:
    task_id: str
    new_status: str
    outcome: str  # 'approved' | 'edited' | 'discarded' | 'feedback'
    dispatched: bool  # True if an EventBus dispatch event was published


_STATUS_FOR_ACTION: dict[CallbackAction, str] = {
    CallbackAction.APPROVE: "completed",
    CallbackAction.EDIT: "completed",
    CallbackAction.DISCARD: "cancelled",
    CallbackAction.FEEDBACK: "awaiting_approval",
}


class TaskNotFoundError(LookupError):
    """The callback references a task_id outside the bound tenant."""


class ApprovalScopingError(PermissionError):
    """The callback's rep_id doesn't match the task's assignee_rep_id.

    Per the blueprint: another rep's approval is not accepted, even if
    the underlying task and callback are otherwise well-formed."""


class TaskAlreadyActionedError(RuntimeError):
    """The referenced task is no longer in 'awaiting_approval'."""


class ApprovalHandler:
    """Transitions task_state, writes the audit row, fans out dispatch.

    The class holds a reference to the :class:`EventBus` so it can
    publish ``approval.approved`` / ``approval.discarded`` after the
    audit row is durably written. Without the bus an instance is still
    usable (publish becomes a no-op) — handy for the tests that don't
    care about routing.
    """

    def __init__(self, *, event_bus: EventBus | None = None) -> None:
        self._event_bus = event_bus

    async def handle(self, callback: ApprovalCallback) -> ApprovalResult:
        tenant_id = current_tenant_or_none()
        if tenant_id is None:
            raise RuntimeError(
                "ApprovalHandler.handle must run inside tenant_scope(...)"
            )

        async with transactional_session() as session:
            task = await _load_task_locked(session, callback.task_id)
            if task is None:
                raise TaskNotFoundError(
                    f"task_state row {callback.task_id} not found for current tenant"
                )
            if task["assignee_rep_id"] is None or task["assignee_rep_id"] != callback.rep_id:
                raise ApprovalScopingError(
                    f"rep {callback.rep_id} cannot action task {callback.task_id} "
                    f"owned by {task['assignee_rep_id']}"
                )
            if task["status"] != PendingTaskStatus.AWAITING_APPROVAL.value:
                raise TaskAlreadyActionedError(
                    f"task {callback.task_id} is in status {task['status']!r}; "
                    "only awaiting_approval tasks can be actioned"
                )

            new_status = _STATUS_FOR_ACTION[callback.action]
            outcome = _outcome_for(callback)

            await session.execute(
                text(
                    """
                    UPDATE task_state
                       SET status = :new_status,
                           updated_at = now(),
                           result = COALESCE(CAST(:result AS jsonb), result)
                     WHERE id = :task_id
                       AND tenant_id = current_setting('app.tenant_id')::uuid
                    """
                ),
                {
                    "new_status": new_status,
                    "task_id": str(callback.task_id),
                    "result": _maybe_json(_result_payload(callback)),
                },
            )

            await record_action(
                session,
                AuditLogEntry(
                    action_type=f"approval.{outcome}",
                    actor_rep_id=callback.rep_id,
                    approver_rep_id=callback.rep_id
                    if callback.action in (CallbackAction.APPROVE, CallbackAction.EDIT)
                    else None,
                    target_type="task_state",
                    target_id=callback.task_id,
                    output=callback.edited_output,
                    metadata={
                        "feedback": callback.feedback,
                        "new_status": new_status,
                        "outcome": outcome,
                        "task_type": task["task_type"],
                    },
                ),
            )

        dispatched = await self._fan_out(
            tenant_id=tenant_id,
            callback=callback,
            task=task,
            outcome=outcome,
        )
        log.info(
            "approval_handler.recorded",
            task_id=str(callback.task_id),
            rep_id=str(callback.rep_id),
            action=callback.action.value,
            outcome=outcome,
            new_status=new_status,
            dispatched=dispatched,
        )
        return ApprovalResult(
            task_id=str(callback.task_id),
            new_status=new_status,
            outcome=outcome,
            dispatched=dispatched,
        )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    async def _fan_out(
        self,
        *,
        tenant_id: UUID,
        callback: ApprovalCallback,
        task: dict[str, Any],
        outcome: str,
    ) -> bool:
        if self._event_bus is None:
            return False
        if outcome in ("approved", "edited"):
            edit_diff: EditDiff | None = None
            if outcome == "edited" and callback.edited_output is not None:
                edit_diff = EditDiff(
                    tenant_id=tenant_id,
                    rep_id=callback.rep_id,
                    task_id=callback.task_id,
                    task_type=task["task_type"],
                    before=task["payload"] or {},
                    after=callback.edited_output,
                )
            await self._event_bus.publish(
                TOPIC_APPROVAL_APPROVED,
                TaskApproved(
                    tenant_id=tenant_id,
                    rep_id=callback.rep_id,
                    task_id=callback.task_id,
                    task_type=task["task_type"],
                    payload=callback.edited_output or task["payload"] or {},
                    edit_diff=edit_diff,
                ),
            )
            return True
        if outcome == "discarded":
            await self._event_bus.publish(
                TOPIC_APPROVAL_DISCARDED,
                TaskDiscarded(
                    tenant_id=tenant_id,
                    rep_id=callback.rep_id,
                    task_id=callback.task_id,
                    task_type=task["task_type"],
                    feedback=callback.feedback,
                ),
            )
            return True
        return False


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
async def _load_task_locked(session, task_id: UUID) -> dict[str, Any] | None:
    """SELECT ... FOR UPDATE inside the open transaction so a concurrent
    callback can't race to action the same task twice."""
    row = await session.execute(
        text(
            """
            SELECT id, assignee_rep_id, status, task_type, payload
              FROM task_state
             WHERE id = :task_id
               AND tenant_id = current_setting('app.tenant_id')::uuid
             FOR UPDATE
            """
        ),
        {"task_id": str(task_id)},
    )
    record = row.mappings().one_or_none()
    return dict(record) if record is not None else None


def _outcome_for(callback: ApprovalCallback) -> str:
    if callback.action is CallbackAction.APPROVE:
        return "approved"
    if callback.action is CallbackAction.EDIT:
        return "edited"
    if callback.action is CallbackAction.DISCARD:
        return "discarded"
    return "feedback"


def _result_payload(callback: ApprovalCallback) -> dict[str, Any] | None:
    """The ``result`` JSON we record on task_state for this transition.

    For APPROVE we leave any previous result alone (handler is mostly
    a state-transition + dispatch); for EDIT we persist the rep's
    edited output so the dispatcher can replay it; for DISCARD /
    FEEDBACK we capture the optional free-text feedback."""
    if callback.action is CallbackAction.EDIT and callback.edited_output is not None:
        return {"edited_output": callback.edited_output, "feedback": callback.feedback}
    if callback.action in (CallbackAction.DISCARD, CallbackAction.FEEDBACK):
        return {"feedback": callback.feedback}
    return None


def _maybe_json(value: Any | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, default=str)
