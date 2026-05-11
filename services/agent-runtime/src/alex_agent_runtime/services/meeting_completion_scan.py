"""Periodic scan that emits ``meeting.completed`` for past meetings.

The blueprint's ``MeetingCompleted`` contract fires "when a calendar
event's end time passes and a recording signal or calendar
confirmation is received." V1 implements the calendar-confirmation
path: this scan walks ORG-tier ``calendar.event`` memory rows for
every active tenant and emits :class:`MeetingCompleted` once
``end_at`` plus a small grace window has elapsed. Recording-signal-
driven completion is a follow-on (a future WO can subscribe to
``IngestionPipeline`` events and short-circuit the scan).

Scheduled by :class:`SchedulerService` from the lifespan; default
tick interval is :data:`MEETING_COMPLETION_SCAN_INTERVAL_SECONDS`.
The scan uses the ``calendar.event_state`` ledger (written by
:class:`MeetingClassifier`) to decide whether an event is already
finalised, so a re-run cannot double-emit ``MeetingCompleted``.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import text

from ..db import admin_session
from ..schemas import (
    CRMPlatform,
    CalendarEvent,
    CalendarLifecycleState,
    MeetingCompleted,
    MemoryRecord,
    MemoryTier,
    MemoryWrite,
)
from .meeting_events import MeetingEventEmitter
from .memory_store import MemoryStore

log = structlog.get_logger(__name__)


MEETING_COMPLETION_SCAN_INTERVAL_SECONDS = 300  # 5 minutes
DEFAULT_COMPLETION_GRACE_SECONDS = 5 * 60  # emit 5 minutes after end_at


class MeetingCompletionScan:
    """One instance per runtime, run on a fixed interval."""

    def __init__(
        self,
        *,
        memory_store: MemoryStore,
        emitter: MeetingEventEmitter,
        grace_seconds: int = DEFAULT_COMPLETION_GRACE_SECONDS,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._memory_store = memory_store
        self._emitter = emitter
        self._grace = timedelta(seconds=grace_seconds)
        self._now = now or (lambda: datetime.now(timezone.utc))

    async def run_once(self) -> int:
        """Scan all active tenants once; returns the number of
        ``MeetingCompleted`` events emitted."""
        emitted = 0
        tenants = await _list_active_tenants()
        for tenant_id in tenants:
            emitted += await self._scan_tenant(tenant_id)
        log.info("meeting_completion_scan.tick", emitted=emitted, tenants=len(tenants))
        return emitted

    async def _scan_tenant(self, tenant_id: UUID) -> int:
        finalised = await self._load_finalised_ids(tenant_id)
        rows = await self._memory_store.list_recent(
            tenant_id=tenant_id,
            tier=MemoryTier.ORG,
            owner_id=None,
            kinds_filter=["calendar.event"],
            limit=500,
        )
        cutoff = self._now() - self._grace
        emitted = 0
        for row in rows:
            attrs = row.attributes or {}
            ce_id = attrs.get("calendar_event_id")
            if not isinstance(ce_id, str) or ce_id in finalised:
                continue
            end_at_raw = attrs.get("end_at")
            if not isinstance(end_at_raw, str):
                continue
            try:
                end_at = datetime.fromisoformat(end_at_raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if end_at > cutoff:
                continue
            await self._emit_completion(tenant_id=tenant_id, row=row, attrs=attrs)
            # Add to the in-tick finalised set so two rows with the same
            # id (rare, but possible if MemoryStore deduplication missed
            # for some reason) don't both fire.
            finalised.add(ce_id)
            emitted += 1
        return emitted

    async def _load_finalised_ids(self, tenant_id: UUID) -> set[str]:
        """Build the set of calendar_event_ids that already transitioned
        to COMPLETED or CANCELLED."""
        finalising = {
            CalendarLifecycleState.COMPLETED.value,
            CalendarLifecycleState.CANCELLED.value,
        }
        rows = await self._memory_store.list_recent(
            tenant_id=tenant_id,
            tier=MemoryTier.ORG,
            owner_id=None,
            kinds_filter=["calendar.event_state"],
            limit=1000,
        )
        out: set[str] = set()
        for row in rows:
            attrs = row.attributes or {}
            if attrs.get("lifecycle_state") in finalising:
                ce_id = attrs.get("calendar_event_id")
                if isinstance(ce_id, str):
                    out.add(ce_id)
        return out

    async def _emit_completion(
        self, *, tenant_id: UUID, row: MemoryRecord, attrs: dict[str, Any]
    ) -> None:
        try:
            event = CalendarEvent.model_validate(json.loads(row.content))
        except Exception:
            log.exception(
                "meeting_completion_scan.parse_failed",
                memory_id=str(row.id),
            )
            return
        opportunity_external_id = attrs.get("opportunity_external_id")
        account_external_id = attrs.get("account_external_id")
        completed = MeetingCompleted(
            tenant_id=tenant_id,
            rep_id=event.rep_id,
            calendar_event_id=event.calendar_event_id,
            provider=event.provider,
            start_at=event.start_at,
            end_at=event.end_at,
            title=event.title,
            opportunity_external_id=opportunity_external_id,
            account_external_id=account_external_id,
            crm_platform=_maybe_platform(attrs.get("crm_platform")),
        )
        await self._write_state_row(
            tenant_id=tenant_id,
            event=event,
            opportunity_external_id=opportunity_external_id,
            account_external_id=account_external_id,
        )
        await self._emitter.emit_completed(completed)
        log.info(
            "meeting_completion_scan.completed",
            calendar_event_id=event.calendar_event_id,
            rep_id=str(event.rep_id),
        )

    async def _write_state_row(
        self,
        *,
        tenant_id: UUID,
        event: CalendarEvent,
        opportunity_external_id: str | None,
        account_external_id: str | None,
    ) -> None:
        now = self._now().isoformat()
        content = json.dumps(
            {
                "calendar_event_id": event.calendar_event_id,
                "state": CalendarLifecycleState.COMPLETED.value,
                "ts": now,
            },
            separators=(",", ":"),
        )
        await self._memory_store.write_with_status(
            tenant_id=tenant_id,
            write=MemoryWrite(
                tier=MemoryTier.ORG,
                owner_id=None,
                kind="calendar.event_state",
                content=content,
                attributes={
                    "calendar_event_id": event.calendar_event_id,
                    "lifecycle_state": CalendarLifecycleState.COMPLETED.value,
                    "transitioned_at": now,
                    "opportunity_external_id": opportunity_external_id,
                    "account_external_id": account_external_id,
                },
                source_uri=(
                    f"{event.provider.value}://event/{event.calendar_event_id}#completed"
                ),
            ),
            index_embeddings=False,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _list_active_tenants() -> list[UUID]:
    """Pull the set of tenant ids that have at least one calendar event
    in the past 30 days. The scan is intrinsically cross-tenant, so it
    runs through ``admin_session``."""
    async with admin_session() as session:
        result = await session.execute(
            text(
                """
                SELECT DISTINCT tenant_id
                  FROM org_memories
                 WHERE kind = 'calendar.event'
                   AND deleted_at IS NULL
                   AND created_at > now() - interval '30 days'
                """
            )
        )
        return [row[0] for row in result.all()]


def _maybe_platform(value: Any) -> CRMPlatform | None:
    if isinstance(value, str) and value in CRMPlatform._value2member_map_:
        return CRMPlatform(value)
    return None


def build_completion_scan_job(scan: MeetingCompletionScan) -> Callable[[], Any]:
    """Wraps a :class:`MeetingCompletionScan` in a try/except so a single
    tick failure does not kill the scheduler's job."""
    async def _tick() -> None:
        try:
            await scan.run_once()
        except Exception:
            log.exception("meeting_completion_scan.tick_failed")

    return _tick
