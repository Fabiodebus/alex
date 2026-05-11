"""Hourly scan that fires :class:`MeetingBriefComposer` on time.

Per the user's WO #17 scoping: an hourly scan window. The blueprint
contract ("30 minutes before start, or immediately if within 30
minutes") is honoured with up to ~60 min slack — meetings created
inside the scan window may not get their on-time brief. Cadence is
set in ``Settings.meeting_brief_scan_interval_seconds``; drop to
300 s if the slack starts hurting reps.

The scan walks ``calendar.event`` memory rows for every tenant whose
``trigger_at`` has elapsed and whose ``end_at`` hasn't, skipping rows
already covered by a ``composer.meeting_brief.composed`` marker.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import text

from ..config import Settings, get_settings
from ..db import admin_session
from ..schemas import (
    CalendarEvent,
    CalendarLifecycleState,
    MemoryRecord,
    MemoryTier,
    MeetingDetected,
)
from ..tenant_context import tenant_scope
from .meeting_brief_composer import MeetingBriefComposer
from .memory_store import MemoryStore

log = structlog.get_logger(__name__)


class MeetingBriefScan:
    def __init__(
        self,
        *,
        memory_store: MemoryStore,
        composer: MeetingBriefComposer,
        settings: Settings | None = None,
    ) -> None:
        self._memory_store = memory_store
        self._composer = composer
        self._settings = settings or get_settings()

    async def run_once(self) -> int:
        """Returns the number of briefs fired this tick."""
        tenants = await _list_active_tenants(
            lookahead_hours=self._settings.meeting_brief_lookahead_hours
        )
        fired = 0
        for tenant_id in tenants:
            fired += await self._scan_tenant(tenant_id)
        if fired:
            log.info("meeting_brief_scan.tick", fired=fired, tenants=len(tenants))
        return fired

    async def _scan_tenant(self, tenant_id: UUID) -> int:
        composed_keys = await self._composed_brief_keys(tenant_id)
        finalised = await self._finalised_event_ids(tenant_id)
        rows = await self._memory_store.list_recent(
            tenant_id=tenant_id,
            tier=MemoryTier.ORG,
            owner_id=None,
            kinds_filter=["calendar.event"],
            limit=500,
        )
        now = datetime.now(timezone.utc)
        horizon = now + timedelta(hours=self._settings.meeting_brief_lookahead_hours)
        fired = 0
        for row in rows:
            attrs = row.attributes or {}
            ce_id = attrs.get("calendar_event_id")
            if not isinstance(ce_id, str):
                continue
            if ce_id in finalised:
                continue
            correlation_key = f"brief:{ce_id}"
            if correlation_key in composed_keys:
                continue
            if not attrs.get("is_external"):
                continue
            trigger = _parse_dt(attrs.get("trigger_at"))
            start = _parse_dt(attrs.get("start_at"))
            end = _parse_dt(attrs.get("end_at"))
            if start is None or end is None:
                continue
            # Skip meetings already over or way out beyond the horizon.
            if end <= now or start > horizon:
                continue
            if trigger is not None and trigger > now:
                continue
            detected = self._row_to_detected(row=row, tenant_id=tenant_id)
            if detected is None:
                continue
            with tenant_scope(tenant_id):
                await self._composer.compose(detected)
            fired += 1
        return fired

    async def _composed_brief_keys(self, tenant_id: UUID) -> set[str]:
        rows = await self._memory_store.list_recent(
            tenant_id=tenant_id,
            tier=MemoryTier.ORG,
            owner_id=None,
            kinds_filter=["composer.meeting_brief.composed"],
            limit=500,
        )
        return {
            (r.attributes or {}).get("correlation_key")
            for r in rows
            if (r.attributes or {}).get("correlation_key")
        }

    async def _finalised_event_ids(self, tenant_id: UUID) -> set[str]:
        rows = await self._memory_store.list_recent(
            tenant_id=tenant_id,
            tier=MemoryTier.ORG,
            owner_id=None,
            kinds_filter=["calendar.event_state"],
            limit=500,
        )
        finalising = {
            CalendarLifecycleState.COMPLETED.value,
            CalendarLifecycleState.CANCELLED.value,
        }
        return {
            (r.attributes or {}).get("calendar_event_id")
            for r in rows
            if (r.attributes or {}).get("lifecycle_state") in finalising
        }

    def _row_to_detected(
        self,
        *,
        row: MemoryRecord,
        tenant_id: UUID,
    ) -> MeetingDetected | None:
        try:
            event = CalendarEvent.model_validate(json.loads(row.content))
        except Exception:
            log.warning(
                "meeting_brief_scan.calendar_event_unparseable",
                memory_id=str(row.id),
            )
            return None
        attrs = row.attributes or {}
        trigger_raw = attrs.get("trigger_at")
        trigger_at = _parse_dt(trigger_raw) or max(
            event.start_at - timedelta(minutes=30),
            datetime.now(timezone.utc),
        )
        return MeetingDetected(
            tenant_id=tenant_id,
            rep_id=event.rep_id,
            calendar_event_id=event.calendar_event_id,
            provider=event.provider,
            start_at=event.start_at,
            end_at=event.end_at,
            trigger_at=trigger_at,
            title=event.title,
            is_external=bool(attrs.get("is_external", True)),
            attendee_profiles=[],
            opportunity_external_id=attrs.get("opportunity_external_id"),
            account_external_id=attrs.get("account_external_id"),
            crm_platform=None,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


async def _list_active_tenants(*, lookahead_hours: int) -> list[UUID]:
    """Find tenants with at least one calendar event in the lookahead
    window — cheap filter so we don't sweep dormant tenants."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=2)
    async with admin_session() as session:
        result = await session.execute(
            text(
                """
                SELECT DISTINCT tenant_id
                  FROM org_memories
                 WHERE kind = 'calendar.event'
                   AND deleted_at IS NULL
                   AND created_at > :cutoff
                """
            ),
            {"cutoff": cutoff},
        )
        return [row[0] for row in result.all()]


def build_meeting_brief_scan_job(scan: MeetingBriefScan) -> Callable[[], Any]:
    async def _tick() -> None:
        try:
            await scan.run_once()
        except Exception:
            log.exception("meeting_brief_scan.tick_failed")

    return _tick
