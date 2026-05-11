"""MeetingClassifier — turn CalendarEvents into MeetingDetected events.

The blueprint splits Calendar & Meeting Detection across three components:

* **CalendarMonitor** lives in Pipedream — normalises Google / Outlook
  payloads into a :class:`CalendarEvent` and forwards it to the runtime.
* **MeetingClassifier** (this module) does the external-vs-internal
  check, joins attendees against the cached CRM memories from WO #9 to
  resolve the meeting to an opportunity / account, calculates the
  trigger time, and persists the event into the ORG-tier MemoryStore so
  the completion scan can find it later.
* **MeetingEventEmitter** is a tiny wrapper around the in-process
  :class:`EventBus`; this classifier owns the detected-event emission.

Key contracts enforced here:

* Internal-only meetings (no attendee outside the rep's email domain)
  do not trigger a ``MeetingDetected`` event.
* A meeting with no resolved opportunity still emits ``MeetingDetected``
  — downstream features handle the no-opportunity case themselves.
* Trigger time is ``max(start - 30min, now)``.
* Cancellations short-circuit detection and emit ``MeetingCancelled``
  iff a prior detected state row exists.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import structlog

from ..schemas import (
    AttendeeProfile,
    CRMPlatform,
    CalendarEvent,
    CalendarEventStatus,
    CalendarLifecycleState,
    IntegrationEvent,
    MeetingCancelled,
    MeetingDetected,
    MemoryRecord,
    MemoryTier,
    MemoryWrite,
)
from .meeting_events import MeetingEventEmitter
from .memory_store import MemoryStore

log = structlog.get_logger(__name__)


# Per the blueprint: 30-minute lead time before the meeting starts.
DEFAULT_TRIGGER_LEAD_MINUTES = 30


class MeetingClassifierError(RuntimeError):
    pass


class MeetingClassifier:
    """One handler per registered ``calendar.update`` event."""

    def __init__(
        self,
        *,
        memory_store: MemoryStore,
        emitter: MeetingEventEmitter,
        lead_minutes: int = DEFAULT_TRIGGER_LEAD_MINUTES,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._memory_store = memory_store
        self._emitter = emitter
        self._lead = timedelta(minutes=lead_minutes)
        self._now = now or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------
    # FeatureRouter handler
    # ------------------------------------------------------------------
    async def handle_calendar_update(self, event: IntegrationEvent) -> MeetingDetected | None:
        try:
            calendar_event = CalendarEvent.model_validate(event.payload)
        except Exception as exc:
            raise MeetingClassifierError(
                f"calendar.update payload did not match CalendarEvent schema: {exc}"
            ) from exc
        return await self.classify(calendar_event)

    # ------------------------------------------------------------------
    # Public entry — used directly by tests + the handler above.
    # ------------------------------------------------------------------
    async def classify(self, event: CalendarEvent) -> MeetingDetected | None:
        if event.status is CalendarEventStatus.CANCELLED:
            await self._handle_cancellation(event)
            return None

        rep_domain = _domain_of(event.rep_email)
        attendee_profiles_raw = self._initial_attendee_profiles(event, rep_domain=rep_domain)
        is_external = any(p.is_external for p in attendee_profiles_raw)

        if not is_external:
            log.info(
                "meeting_classifier.skip_internal",
                calendar_event_id=event.calendar_event_id,
                rep_id=str(event.rep_id),
            )
            return None

        attendee_profiles = await self._resolve_crm(event=event, profiles=attendee_profiles_raw)
        opportunity_external_id, account_external_id, crm_platform = self._pick_opportunity(
            attendee_profiles
        )

        trigger_at = max(event.start_at - self._lead, self._now())
        detected = MeetingDetected(
            tenant_id=event.tenant_id,
            rep_id=event.rep_id,
            calendar_event_id=event.calendar_event_id,
            provider=event.provider,
            start_at=event.start_at,
            end_at=event.end_at,
            trigger_at=trigger_at,
            title=event.title,
            is_external=True,
            attendee_profiles=attendee_profiles,
            opportunity_external_id=opportunity_external_id,
            account_external_id=account_external_id,
            crm_platform=crm_platform,
        )

        await self._persist_lifecycle(
            event=event,
            detected=detected,
            state=CalendarLifecycleState.DETECTED,
        )
        await self._emitter.emit_detected(detected)
        log.info(
            "meeting_classifier.detected",
            calendar_event_id=event.calendar_event_id,
            rep_id=str(event.rep_id),
            opportunity_external_id=opportunity_external_id,
            trigger_at=trigger_at.isoformat(),
        )
        return detected

    # ------------------------------------------------------------------
    # Cancellation path
    # ------------------------------------------------------------------
    async def _handle_cancellation(self, event: CalendarEvent) -> None:
        prior_event_row = await self._find_calendar_row(
            tenant_id=event.tenant_id, calendar_event_id=event.calendar_event_id
        )
        if prior_event_row is None:
            # Nothing was ever detected — silently drop. No downstream
            # feature has anything to retract.
            log.info(
                "meeting_classifier.cancel_no_prior",
                calendar_event_id=event.calendar_event_id,
            )
            return

        if await self._has_finalising_state(
            tenant_id=event.tenant_id,
            calendar_event_id=event.calendar_event_id,
            states=(CalendarLifecycleState.CANCELLED,),
        ):
            return  # already cancelled

        prior_attrs = prior_event_row.attributes or {}
        cancelled = MeetingCancelled(
            tenant_id=event.tenant_id,
            rep_id=event.rep_id,
            calendar_event_id=event.calendar_event_id,
            provider=event.provider,
            title=event.title or prior_attrs.get("title"),
            opportunity_external_id=prior_attrs.get("opportunity_external_id"),
        )
        await self._write_state_row(
            event=event,
            state=CalendarLifecycleState.CANCELLED,
            opportunity_external_id=prior_attrs.get("opportunity_external_id"),
            account_external_id=prior_attrs.get("account_external_id"),
        )
        await self._emitter.emit_cancelled(cancelled)
        log.info(
            "meeting_classifier.cancelled",
            calendar_event_id=event.calendar_event_id,
        )

    # ------------------------------------------------------------------
    # CRM resolution — joins attendees against the WO #9 cache.
    # ------------------------------------------------------------------
    async def _resolve_crm(
        self,
        *,
        event: CalendarEvent,
        profiles: list[AttendeeProfile],
    ) -> list[AttendeeProfile]:
        """For each external attendee, hydrate crm_contact_external_id +
        crm_account_external_id from the cached ORG-tier CRM memories."""
        cached_contacts = await self._load_crm_cache(
            tenant_id=event.tenant_id, kind_suffix="contact"
        )
        cached_accounts = await self._load_crm_cache(
            tenant_id=event.tenant_id, kind_suffix="account"
        )

        # Index by email and by domain for the two-step join.
        contacts_by_email = {
            row.email.lower(): row for row in cached_contacts if row.email
        }
        accounts_by_external_id = {row.external_id: row for row in cached_accounts}
        accounts_by_domain = {
            row.domain.lower(): row for row in cached_accounts if row.domain
        }

        resolved: list[AttendeeProfile] = []
        for profile in profiles:
            if not profile.is_external:
                resolved.append(profile)
                continue
            email = profile.email.lower()
            contact = contacts_by_email.get(email)
            account = None
            if contact is not None and contact.account_external_id is not None:
                account = accounts_by_external_id.get(contact.account_external_id)
            if account is None:
                account = accounts_by_domain.get(_domain_of(email))
            resolved.append(
                profile.model_copy(
                    update={
                        "crm_contact_external_id": contact.external_id if contact else None,
                        "crm_account_external_id": (
                            account.external_id
                            if account is not None
                            else (
                                contact.account_external_id
                                if contact is not None
                                else None
                            )
                        ),
                    }
                )
            )
        return resolved

    async def _load_crm_cache(
        self, *, tenant_id: UUID, kind_suffix: str
    ) -> list[_CachedCRMRow]:
        """Pull the most-recent CRM memories of one kind for the tenant."""
        rows = await self._memory_store.list_recent(
            tenant_id=tenant_id,
            tier=MemoryTier.ORG,
            owner_id=None,
            kinds_filter=[f"crm.{kind_suffix}"],
            limit=500,
        )
        out: list[_CachedCRMRow] = []
        for row in rows:
            try:
                payload = json.loads(row.content)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            out.append(
                _CachedCRMRow(
                    external_id=str(payload.get("external_id") or ""),
                    email=(payload.get("email") or None),
                    domain=(payload.get("domain") or None),
                    account_external_id=payload.get("account_external_id"),
                    platform=(
                        CRMPlatform(payload["platform"])
                        if "platform" in payload
                        and isinstance(payload["platform"], str)
                        and payload["platform"] in CRMPlatform._value2member_map_
                        else None
                    ),
                )
            )
        return out

    def _pick_opportunity(
        self, profiles: Iterable[AttendeeProfile]
    ) -> tuple[str | None, str | None, CRMPlatform | None]:
        """Pick the most likely opportunity for the meeting.

        ADR-001 says CRM resolution happens once at detection time. The
        cached CRM cache doesn't carry an attendee→opportunity edge
        directly (opportunities are linked to accounts, not contacts), so
        the practical signal in v1 is the account_external_id of the
        resolved attendees. When multiple external attendees resolve to
        the same account we report it; when they split across accounts
        we return ``None`` and let the rep correct it via the meeting-
        prep card.
        """
        accounts: list[str] = []
        for profile in profiles:
            if profile.crm_account_external_id and profile.crm_account_external_id not in accounts:
                accounts.append(profile.crm_account_external_id)
        if len(accounts) == 1:
            return None, accounts[0], None
        return None, None, None

    # ------------------------------------------------------------------
    # Lifecycle persistence
    # ------------------------------------------------------------------
    async def _persist_lifecycle(
        self,
        *,
        event: CalendarEvent,
        detected: MeetingDetected,
        state: CalendarLifecycleState,
    ) -> None:
        """On detection we write two rows:

        * ``kind='calendar.event'`` — canonical event payload (dedup-
          friendly via content hash); written once per calendar id.
        * ``kind='calendar.event_state'`` — state-transition ledger
          (each transition is a distinct row because the content
          carries the state + timestamp). The completion scan reads
          this ledger to decide whether an event is already finalised.
        """
        attrs = {
            "calendar_event_id": event.calendar_event_id,
            "lifecycle_state": state.value,
            "provider": event.provider.value,
            "rep_id": str(event.rep_id),
            "rep_email": event.rep_email,
            "start_at": event.start_at.isoformat(),
            "end_at": event.end_at.isoformat(),
            "title": event.title,
            "opportunity_external_id": detected.opportunity_external_id,
            "account_external_id": detected.account_external_id,
            "is_external": detected.is_external,
            "trigger_at": detected.trigger_at.isoformat(),
        }
        content = json.dumps(event.model_dump(mode="json"), separators=(",", ":"), default=str)
        await self._memory_store.write_with_status(
            tenant_id=event.tenant_id,
            write=MemoryWrite(
                tier=MemoryTier.ORG,
                owner_id=None,
                kind="calendar.event",
                content=content,
                attributes=attrs,
                source_uri=f"{event.provider.value}://event/{event.calendar_event_id}",
            ),
            index_embeddings=False,
        )
        await self._write_state_row(
            event=event,
            state=state,
            opportunity_external_id=detected.opportunity_external_id,
            account_external_id=detected.account_external_id,
        )

    async def _write_state_row(
        self,
        *,
        event: CalendarEvent,
        state: CalendarLifecycleState,
        opportunity_external_id: str | None,
        account_external_id: str | None,
    ) -> None:
        now = self._now().isoformat()
        content = json.dumps(
            {
                "calendar_event_id": event.calendar_event_id,
                "state": state.value,
                "ts": now,
            },
            separators=(",", ":"),
        )
        await self._memory_store.write_with_status(
            tenant_id=event.tenant_id,
            write=MemoryWrite(
                tier=MemoryTier.ORG,
                owner_id=None,
                kind="calendar.event_state",
                content=content,
                attributes={
                    "calendar_event_id": event.calendar_event_id,
                    "lifecycle_state": state.value,
                    "transitioned_at": now,
                    "opportunity_external_id": opportunity_external_id,
                    "account_external_id": account_external_id,
                },
                source_uri=(
                    f"{event.provider.value}://event/{event.calendar_event_id}"
                    f"#{state.value}"
                ),
            ),
            index_embeddings=False,
        )

    async def _has_finalising_state(
        self,
        *,
        tenant_id: UUID,
        calendar_event_id: str,
        states: tuple[CalendarLifecycleState, ...],
    ) -> bool:
        rows = await self._memory_store.list_recent(
            tenant_id=tenant_id,
            tier=MemoryTier.ORG,
            owner_id=None,
            kinds_filter=["calendar.event_state"],
            limit=200,
        )
        wanted = {s.value for s in states}
        for row in rows:
            attrs = row.attributes or {}
            if (
                attrs.get("calendar_event_id") == calendar_event_id
                and attrs.get("lifecycle_state") in wanted
            ):
                return True
        return False

    async def _find_calendar_row(
        self, *, tenant_id: UUID, calendar_event_id: str
    ) -> MemoryRecord | None:
        rows = await self._memory_store.list_recent(
            tenant_id=tenant_id,
            tier=MemoryTier.ORG,
            owner_id=None,
            kinds_filter=["calendar.event"],
            limit=200,
        )
        for row in rows:
            if row.attributes.get("calendar_event_id") == calendar_event_id:
                return row
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _initial_attendee_profiles(
        self, event: CalendarEvent, *, rep_domain: str
    ) -> list[AttendeeProfile]:
        profiles: list[AttendeeProfile] = []
        for attendee in event.attendees:
            domain = _domain_of(attendee.email)
            if not domain or not attendee.email:
                continue
            # Skip the rep themselves so "is_external" only reflects the
            # other parties on the invite.
            if attendee.email.lower() == event.rep_email.lower():
                continue
            profiles.append(
                AttendeeProfile(
                    email=attendee.email,
                    name=attendee.name,
                    is_external=(domain != rep_domain),
                    response_status=attendee.response_status,
                    is_organizer=attendee.is_organizer,
                )
            )
        return profiles


# ---------------------------------------------------------------------------
# Internal helper types
# ---------------------------------------------------------------------------
class _CachedCRMRow:
    __slots__ = ("external_id", "email", "domain", "account_external_id", "platform")

    def __init__(
        self,
        *,
        external_id: str,
        email: str | None,
        domain: str | None,
        account_external_id: str | None,
        platform: CRMPlatform | None,
    ) -> None:
        self.external_id = external_id
        self.email = email
        self.domain = domain
        self.account_external_id = account_external_id
        self.platform = platform


def _domain_of(email: str | None) -> str:
    if not email or "@" not in email:
        return ""
    return email.split("@", 1)[1].lower()


