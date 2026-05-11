"""``/events`` core: idempotent persistence + feature dispatch + audit log."""
from __future__ import annotations

import json
from dataclasses import dataclass

import structlog
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from ..db import transactional_session
from ..schemas import AuditLogEntry, IntegrationEvent
from .audit_log import record_action
from .feature_router import FeatureRouter

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class EventProcessingResult:
    accepted: bool
    deduplicated: bool
    handler: str | None
    event_id: str


class EventProcessor:
    """Persists, dedupes, and routes an inbound IntegrationEvent."""

    def __init__(self, router: FeatureRouter) -> None:
        self._router = router

    async def process(self, event: IntegrationEvent) -> EventProcessingResult:
        async with transactional_session() as session:
            try:
                await session.execute(
                    text(
                        """
                        INSERT INTO processed_events
                            (tenant_id, event_id, source, kind, payload, received_at)
                        VALUES
                            (current_setting('app.tenant_id')::uuid,
                             :event_id,
                             :source,
                             :kind,
                             CAST(:payload AS jsonb),
                             :received_at)
                        """
                    ),
                    {
                        "event_id": event.event_id,
                        "source": event.source,
                        "kind": str(event.kind),
                        "payload": json.dumps(event.payload, default=str),
                        "received_at": event.occurred_at,
                    },
                )
            except IntegrityError:
                # PK collision => duplicate event. Roll back and ack as idempotent.
                log.info(
                    "event_processor.duplicate",
                    event_id=event.event_id,
                    source=event.source,
                    kind=str(event.kind),
                )
                await session.rollback()
                return EventProcessingResult(
                    accepted=True,
                    deduplicated=True,
                    handler=None,
                    event_id=event.event_id,
                )

            handler_name = await self._router.dispatch(event)

            await record_action(
                session,
                AuditLogEntry(
                    action_type="event.received",
                    target_type="integration_event",
                    prompt=None,
                    output=None,
                    metadata={
                        "event_id": event.event_id,
                        "source": event.source,
                        "kind": str(event.kind),
                        "handler": handler_name,
                    },
                ),
            )

        log.info(
            "event_processor.accepted",
            event_id=event.event_id,
            source=event.source,
            kind=str(event.kind),
            handler=handler_name,
        )
        return EventProcessingResult(
            accepted=True,
            deduplicated=False,
            handler=handler_name,
            event_id=event.event_id,
        )
