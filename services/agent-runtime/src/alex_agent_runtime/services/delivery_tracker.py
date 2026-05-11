"""CRUD over the ``delivery_statuses`` table.

Every dispatch from :class:`OutputRouter` produces (or refreshes) a
row here. The blueprint's contracts pinned to this table:

* Daily-brief dedup — feature WOs query "recent delivered rows for
  this rep" to skip re-notification on items already seen.
* Escalation — the periodic scan finds rows older than the retry
  window that aren't ``delivered`` and flips them to ``escalated``.
* At-least-once — the unique constraint on ``(tenant_id, output_id)``
  lets retries replay safely; we ``ON CONFLICT`` and bump the attempt
  counter rather than inserting duplicates.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import text

from ..db import admin_session, transactional_session
from ..schemas import DeliveryChannel, DeliveryStatus, DeliveryStatusValue
from ..tenant_context import tenant_scope

log = structlog.get_logger(__name__)


class DeliveryTracker:
    """Stateless helper that wraps the ``delivery_statuses`` table."""

    def __init__(self, *, escalation_seconds: int) -> None:
        self._escalation = timedelta(seconds=escalation_seconds)

    async def record_pending(
        self,
        *,
        tenant_id: UUID,
        rep_id: UUID,
        output_id: str,
        output_type: str,
        channel: DeliveryChannel,
        task_id: UUID | None,
        payload: dict[str, Any],
    ) -> DeliveryStatus:
        """Upsert a row in the ``pending`` state.

        Re-recording the same ``output_id`` updates the existing row
        rather than creating a duplicate — the blueprint's
        at-least-once guarantee plus the on-the-wire idempotency rule.
        """
        retry_after = _now() + self._escalation
        with tenant_scope(tenant_id):
            async with transactional_session() as session:
                row = await session.execute(
                    text(
                        """
                        INSERT INTO delivery_statuses (
                            tenant_id, rep_id, task_id, output_id,
                            output_type, channel, status, payload,
                            retry_after
                        ) VALUES (
                            current_setting('app.tenant_id')::uuid,
                            :rep_id, :task_id, :output_id,
                            :output_type, :channel, 'pending',
                            CAST(:payload AS jsonb),
                            :retry_after
                        )
                        ON CONFLICT (tenant_id, output_id) DO UPDATE
                          SET status = 'pending',
                              channel = EXCLUDED.channel,
                              payload = EXCLUDED.payload,
                              retry_after = EXCLUDED.retry_after,
                              updated_at = now()
                        RETURNING *
                        """
                    ),
                    {
                        "rep_id": rep_id,
                        "task_id": task_id,
                        "output_id": output_id,
                        "output_type": output_type,
                        "channel": channel.value,
                        "payload": json.dumps(payload, default=str),
                        "retry_after": retry_after,
                    },
                )
                return _row_to_status(row.mappings().one())

    async def mark_delivered(
        self,
        *,
        tenant_id: UUID,
        output_id: str,
        response: dict[str, Any],
    ) -> DeliveryStatus | None:
        return await self._transition(
            tenant_id=tenant_id,
            output_id=output_id,
            status=DeliveryStatusValue.DELIVERED,
            response=response,
            acknowledged_at=_now(),
        )

    async def mark_failed(
        self,
        *,
        tenant_id: UUID,
        output_id: str,
        response: dict[str, Any],
    ) -> DeliveryStatus | None:
        return await self._transition(
            tenant_id=tenant_id,
            output_id=output_id,
            status=DeliveryStatusValue.FAILED,
            response=response,
            increment_attempt=True,
        )

    async def recent_for_rep(
        self,
        *,
        tenant_id: UUID,
        rep_id: UUID,
        limit: int = 20,
        since: datetime | None = None,
    ) -> list[DeliveryStatus]:
        """Used by the daily brief assembler to skip re-notification."""
        params: dict[str, Any] = {"rep_id": rep_id, "limit": limit}
        where_since = ""
        if since is not None:
            params["since"] = since
            where_since = "AND created_at >= :since"
        with tenant_scope(tenant_id):
            async with transactional_session() as session:
                row = await session.execute(
                    text(
                        f"""
                        SELECT *
                          FROM delivery_statuses
                         WHERE tenant_id = current_setting('app.tenant_id')::uuid
                           AND rep_id = :rep_id
                           {where_since}
                         ORDER BY created_at DESC
                         LIMIT :limit
                        """
                    ),
                    params,
                )
                return [_row_to_status(r) for r in row.mappings().all()]

    # ------------------------------------------------------------------
    # Cross-tenant escalation scan helpers (used by DeliveryEscalationScan).
    # ------------------------------------------------------------------
    async def escalate_overdue(self) -> list[DeliveryStatus]:
        """Cross-tenant: flip every overdue pending/failed row to
        ``escalated`` and return the transitioned rows."""
        cutoff = _now()
        async with admin_session() as session:
            row = await session.execute(
                text(
                    """
                    UPDATE delivery_statuses
                       SET status = 'escalated',
                           escalated_at = now(),
                           updated_at = now()
                     WHERE status IN ('pending','failed')
                       AND retry_after IS NOT NULL
                       AND retry_after < :cutoff
                    RETURNING *
                    """
                ),
                {"cutoff": cutoff},
            )
            return [_row_to_status(r) for r in row.mappings().all()]

    async def _transition(
        self,
        *,
        tenant_id: UUID,
        output_id: str,
        status: DeliveryStatusValue,
        response: dict[str, Any],
        acknowledged_at: datetime | None = None,
        increment_attempt: bool = False,
    ) -> DeliveryStatus | None:
        with tenant_scope(tenant_id):
            async with transactional_session() as session:
                row = await session.execute(
                    text(
                        """
                        UPDATE delivery_statuses
                           SET status = :status,
                               last_attempt_at = now(),
                               attempt_count = attempt_count + :delta,
                               response = CAST(:response AS jsonb),
                               acknowledged_at = COALESCE(
                                   :acknowledged_at, acknowledged_at
                               ),
                               updated_at = now()
                         WHERE tenant_id = current_setting('app.tenant_id')::uuid
                           AND output_id = :output_id
                        RETURNING *
                        """
                    ),
                    {
                        "status": status.value,
                        "output_id": output_id,
                        "response": json.dumps(response, default=str),
                        "acknowledged_at": acknowledged_at,
                        "delta": 1 if increment_attempt else 0,
                    },
                )
                record = row.mappings().one_or_none()
        if record is None:
            return None
        return _row_to_status(record)


def _row_to_status(record: Any) -> DeliveryStatus:
    return DeliveryStatus(
        id=record["id"],
        tenant_id=record["tenant_id"],
        rep_id=record["rep_id"],
        task_id=record["task_id"],
        output_id=record["output_id"],
        output_type=record["output_type"],
        channel=DeliveryChannel(record["channel"]),
        status=DeliveryStatusValue(record["status"]),
        attempt_count=record["attempt_count"],
        last_attempt_at=record["last_attempt_at"],
        acknowledged_at=record["acknowledged_at"],
        escalated_at=record["escalated_at"],
        retry_after=record["retry_after"],
        payload=record["payload"] or {},
        response=record["response"],
        created_at=record["created_at"],
        updated_at=record["updated_at"],
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)
