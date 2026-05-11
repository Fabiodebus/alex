"""Periodic scan that flips overdue PendingTasks to ``expired``.

The blueprint guarantees: "Expired ``PendingTask`` records are
surfaced in the rep's next daily brief rather than silently
dropped." This scan implements the first half — finding stale tasks
and flipping their status to ``expired`` + writing the audit row +
publishing ``approval.expired``. The daily-brief query that surfaces
them to the rep belongs to a feature WO (Daily Brief).

The scan runs cross-tenant via :func:`admin_session` because RLS
binds a session to a single tenant. Each tenant's transitions still
land in ``audit_log`` under that tenant's id.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import text

from ..db import admin_session
from ..schemas import (
    ApprovalOutcome,
    PendingTaskStatus,
    TaskExpired,
)
from .event_bus import EventBus

log = structlog.get_logger(__name__)


APPROVAL_EXPIRY_SCAN_INTERVAL_SECONDS = 60
TOPIC_APPROVAL_EXPIRED = "approval.expired"


class ApprovalExpiryScan:
    def __init__(
        self,
        *,
        event_bus: EventBus,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._now = now or (lambda: datetime.now(timezone.utc))

    async def run_once(self) -> int:
        """Flip every overdue ``awaiting_approval`` task to ``expired``;
        return the number of transitions."""
        cutoff = self._now()
        rows: list[dict[str, Any]] = []
        async with admin_session(allow_audit_purge=False) as session:
            # Lock + claim every overdue task in one statement. The
            # RETURNING clause feeds the per-row event publication
            # outside the transaction so a slow EventBus subscriber
            # can't extend the lock.
            result = await session.execute(
                text(
                    """
                    UPDATE task_state
                       SET status = :expired,
                           updated_at = now()
                     WHERE status = :awaiting
                       AND deadline IS NOT NULL
                       AND deadline < :cutoff
                    RETURNING id, tenant_id, assignee_rep_id, task_type, deadline
                    """
                ),
                {
                    "expired": PendingTaskStatus.EXPIRED.value,
                    "awaiting": PendingTaskStatus.AWAITING_APPROVAL.value,
                    "cutoff": cutoff,
                },
            )
            rows = [dict(r) for r in result.mappings().all()]
            # One audit row per expired task, written under the same
            # transaction so the trail is durable even if the scan
            # crashes before publishing events.
            for row in rows:
                await session.execute(
                    text(
                        """
                        INSERT INTO audit_log (
                            tenant_id, actor_rep_id, action_type,
                            target_type, target_id, metadata
                        ) VALUES (
                            :tenant_id, :rep_id, :action_type,
                            'task_state', :task_id,
                            CAST(:metadata AS jsonb)
                        )
                        """
                    ),
                    {
                        "tenant_id": row["tenant_id"],
                        "rep_id": row["assignee_rep_id"],
                        "action_type": f"approval.{ApprovalOutcome.EXPIRED.value}",
                        "task_id": row["id"],
                        "metadata": '{"reason": "deadline_elapsed"}',
                    },
                )

        for row in rows:
            await self._event_bus.publish(
                TOPIC_APPROVAL_EXPIRED,
                TaskExpired(
                    tenant_id=row["tenant_id"],
                    rep_id=row["assignee_rep_id"],
                    task_id=row["id"],
                    task_type=row["task_type"],
                    deadline=row["deadline"],
                ),
            )
        if rows:
            log.info(
                "approval_expiry_scan.tick",
                expired=len(rows),
                tenants=len({str(r["tenant_id"]) for r in rows}),
            )
        return len(rows)


def build_expiry_scan_job(scan: ApprovalExpiryScan) -> Callable[[], Any]:
    """Tick wrapper for :class:`SchedulerService.add_interval_job`."""
    async def _tick() -> None:
        try:
            await scan.run_once()
        except Exception:
            log.exception("approval_expiry_scan.tick_failed")

    return _tick
