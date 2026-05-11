"""Periodic scan that escalates undelivered outputs.

Per the blueprint: "Delivery failure does not silently drop the
output; DeliveryTracker escalates undelivered outputs after a
configurable retry window." This scan implements that flip — a row
in ``pending`` or ``failed`` whose ``retry_after`` has elapsed gets
moved to ``escalated`` and a :class:`DeliveryEscalated` event
published. The Daily Brief WO subscribes and re-surfaces the output.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import structlog

from ..schemas import DeliveryEscalated
from .delivery_tracker import DeliveryTracker
from .event_bus import EventBus

log = structlog.get_logger(__name__)


DELIVERY_ESCALATION_SCAN_INTERVAL_SECONDS = 60
TOPIC_DELIVERY_ESCALATED = "delivery.escalated"


class DeliveryEscalationScan:
    def __init__(
        self,
        *,
        tracker: DeliveryTracker,
        event_bus: EventBus,
    ) -> None:
        self._tracker = tracker
        self._event_bus = event_bus

    async def run_once(self) -> int:
        escalated = await self._tracker.escalate_overdue()
        for row in escalated:
            await self._event_bus.publish(
                TOPIC_DELIVERY_ESCALATED,
                DeliveryEscalated(
                    tenant_id=row.tenant_id,
                    rep_id=row.rep_id,
                    output_id=row.output_id,
                    output_type=row.output_type,
                    channel=row.channel,
                    attempt_count=row.attempt_count,
                    escalated_at=row.escalated_at or row.updated_at,
                ),
            )
        if escalated:
            log.info(
                "delivery_escalation_scan.tick",
                escalated=len(escalated),
                tenants=len({str(r.tenant_id) for r in escalated}),
            )
        return len(escalated)


def build_escalation_scan_job(scan: DeliveryEscalationScan) -> Callable[[], Any]:
    async def _tick() -> None:
        try:
            await scan.run_once()
        except Exception:
            log.exception("delivery_escalation_scan.tick_failed")

    return _tick
