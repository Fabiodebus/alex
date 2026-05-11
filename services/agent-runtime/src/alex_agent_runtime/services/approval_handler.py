"""``/callbacks`` core: rep approval/edit/discard/feedback handling."""
from __future__ import annotations

from dataclasses import dataclass

import structlog
from sqlalchemy import text

from ..db import transactional_session
from ..schemas import ApprovalCallback, AuditLogEntry, CallbackAction
from .audit_log import record_action

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class ApprovalResult:
    task_id: str
    new_status: str
    dispatched: bool


_STATUS_FOR_ACTION: dict[CallbackAction, str] = {
    CallbackAction.APPROVE: "completed",
    CallbackAction.EDIT: "completed",
    CallbackAction.DISCARD: "cancelled",
    CallbackAction.FEEDBACK: "awaiting_approval",
}


class TaskNotFoundError(LookupError):
    """Raised when a callback references a task_id outside the bound tenant."""


class ApprovalHandler:
    """Transitions task_state and writes the rep decision to the audit log.

    Feature WOs will hook into the post-approval branch to dispatch the
    approved ``ActionRequest`` to Pipedream. For the scaffold we simply
    record that approval landed.
    """

    async def handle(self, callback: ApprovalCallback) -> ApprovalResult:
        new_status = _STATUS_FOR_ACTION[callback.action]
        async with transactional_session() as session:
            row = await session.execute(
                text(
                    """
                    UPDATE task_state
                       SET status = :new_status,
                           updated_at = now(),
                           result = COALESCE(CAST(:edited_output AS jsonb), result)
                     WHERE id = :task_id
                       AND tenant_id = current_setting('app.tenant_id')::uuid
                    RETURNING id
                    """
                ),
                {
                    "new_status": new_status,
                    "task_id": str(callback.task_id),
                    "edited_output": _maybe_json(callback.edited_output),
                },
            )
            updated_id = row.scalar_one_or_none()
            if updated_id is None:
                raise TaskNotFoundError(
                    f"task_state row {callback.task_id} not found for current tenant"
                )

            await record_action(
                session,
                AuditLogEntry(
                    action_type=f"approval.{callback.action.value}",
                    actor_rep_id=callback.rep_id,
                    approver_rep_id=callback.rep_id
                    if callback.action == CallbackAction.APPROVE
                    else None,
                    target_type="task_state",
                    target_id=callback.task_id,
                    output=callback.edited_output,
                    metadata={
                        "feedback": callback.feedback,
                        "new_status": new_status,
                    },
                ),
            )

        # In the scaffold we don't yet dispatch ActionRequests to Pipedream.
        dispatched = False
        log.info(
            "approval_handler.recorded",
            task_id=str(callback.task_id),
            rep_id=str(callback.rep_id),
            action=callback.action.value,
            new_status=new_status,
        )
        return ApprovalResult(
            task_id=str(callback.task_id),
            new_status=new_status,
            dispatched=dispatched,
        )


def _maybe_json(value: dict | None) -> str | None:
    if value is None:
        return None
    import json

    return json.dumps(value, default=str)
