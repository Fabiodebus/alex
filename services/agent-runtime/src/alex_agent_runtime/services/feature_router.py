"""Coarse feature router for inbound IntegrationEvent payloads.

Feature WOs (meeting prep, post-meeting follow-up, CRM notes, etc.) will
register handlers here. For the WO #2 scaffold the router simply records
that an event reached it; it does not invoke any feature logic.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeAlias

import structlog

from ..schemas import IntegrationEvent

log = structlog.get_logger(__name__)

Handler: TypeAlias = Callable[[IntegrationEvent], Awaitable[None]]


class FeatureRouter:
    """Maps an ``IntegrationEvent.kind`` to a feature handler."""

    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def register(self, kind: str, handler: Handler) -> None:
        if kind in self._handlers:
            raise ValueError(f"handler for kind={kind!r} already registered")
        self._handlers[kind] = handler

    async def dispatch(self, event: IntegrationEvent) -> str:
        """Return the name of the handler that ran ('noop' if unmapped)."""
        handler = self._handlers.get(str(event.kind))
        if handler is None:
            log.info(
                "feature_router.unmapped",
                kind=str(event.kind),
                event_id=event.event_id,
            )
            return "noop"
        await handler(event)
        return handler.__qualname__
