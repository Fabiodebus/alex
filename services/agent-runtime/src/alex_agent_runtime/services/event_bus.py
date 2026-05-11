"""Tiny in-process async event bus.

The Persistent Memory blueprint specifies a `MemorySummaryUpdated`
event consumed internally to invalidate cached prompt contexts, and an
`IngestionComplete` event the onboarding flow listens for to fire the
first proactive output. Both are in-process publish/subscribe in a
single-runtime deployment; if we ever shard the agent runtime across
processes we'll swap this for Redis pub/sub or NATS without changing
the call sites.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

log = structlog.get_logger(__name__)

EventHandler = Callable[[Any], Awaitable[None]]


class EventBus:
    """Per-process async pub/sub.

    Subscribers are registered at lifespan startup; publishes fan-out
    with ``asyncio.gather(return_exceptions=True)`` so a single
    misbehaving handler can't stall the publisher (errors are logged
    rather than re-raised).
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[EventHandler]] = defaultdict(list)

    def subscribe(self, event_name: str, handler: EventHandler) -> None:
        self._subscribers[event_name].append(handler)
        log.debug("event_bus.subscribed", topic=event_name, handler=getattr(handler, "__qualname__", str(handler)))

    def unsubscribe(self, event_name: str, handler: EventHandler) -> None:
        try:
            self._subscribers[event_name].remove(handler)
        except ValueError:
            pass

    async def publish(self, event_name: str, payload: Any) -> None:
        handlers = list(self._subscribers.get(event_name, ()))
        if not handlers:
            log.debug("event_bus.publish.no_subscribers", topic=event_name)
            return
        log.info("event_bus.publish", topic=event_name, subscribers=len(handlers))
        results = await asyncio.gather(
            *(handler(payload) for handler in handlers), return_exceptions=True
        )
        for handler, result in zip(handlers, results, strict=True):
            if isinstance(result, Exception):
                log.exception(
                    "event_bus.handler_failed",
                    topic=event_name,
                    handler=getattr(handler, "__qualname__", str(handler)),
                    exc_info=result,
                )

    def subscriber_count(self, event_name: str) -> int:
        return len(self._subscribers.get(event_name, ()))
