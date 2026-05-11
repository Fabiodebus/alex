"""Centralised topic names + typed publish helpers for meeting events.

Both :class:`MeetingClassifier` (detection / cancellation) and
:class:`MeetingCompletionScan` (completion) publish through this
module so the topic names live in exactly one place. Downstream
feature WOs (Meeting Prep, Post-Meeting Follow-Up Drafting, CRM Notes
& Updates) subscribe by importing these constants rather than
duplicating string literals.
"""
from __future__ import annotations

from typing import Final

from ..schemas import MeetingCancelled, MeetingCompleted, MeetingDetected
from .event_bus import EventBus

TOPIC_MEETING_DETECTED: Final[str] = "meeting.detected"
TOPIC_MEETING_COMPLETED: Final[str] = "meeting.completed"
TOPIC_MEETING_CANCELLED: Final[str] = "meeting.cancelled"


class MeetingEventEmitter:
    """Typed front-door for the three meeting topics.

    Wraps :class:`EventBus.publish` so callers can't mistype the topic
    name or pass the wrong payload shape."""

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    async def emit_detected(self, payload: MeetingDetected) -> None:
        await self._bus.publish(TOPIC_MEETING_DETECTED, payload)

    async def emit_completed(self, payload: MeetingCompleted) -> None:
        await self._bus.publish(TOPIC_MEETING_COMPLETED, payload)

    async def emit_cancelled(self, payload: MeetingCancelled) -> None:
        await self._bus.publish(TOPIC_MEETING_CANCELLED, payload)
