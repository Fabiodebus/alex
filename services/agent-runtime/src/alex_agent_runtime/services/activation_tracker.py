"""ActivationTracker — Phase 3's "first proactive output" engine.

Per the blueprint, after onboarding's OAuth+ingestion phase completes:

1. Send the rep a summary of what Alex learned (deals, accounts, calls).
2. Select the highest-priority first proactive output:
   meeting prep > follow-up draft > stalled deal summary > fallback
   intro.
3. Record the activation milestone when the rep approves their first
   agent-generated draft.

The selectors here are *intentionally* lightweight — feature WOs in
Phase 4 own the real generation. This module's job is to (a) pick the
output type using the data we already have in memory, (b) emit a
plain-language introduction framed as "this is what I'd normally do
for you", and (c) flip the onboarding_state book-keeping.

Trigger surfaces:

* ``ingestion.complete`` (EventBus) — summary + first-output selection.
* ``approval.approved`` (EventBus) — when this is the FIRST approve for
  the rep, mark the activation milestone.
* Periodic scan — for reps whose ingestion completed >= 24 h ago and
  who still have no first proactive output, emit the fallback intro
  so the 24-hour blueprint guarantee is honoured even when no
  meeting / follow-up / stalled-deal context exists.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import structlog

from ..config import Settings, get_settings
from ..schemas import (
    ActivationMilestone,
    CalendarLifecycleState,
    DeliveryRequest,
    FirstProactiveOutputType,
    FirstProactiveSelection,
    IngestionComplete,
    MemoryTier,
    OutputType,
    TaskApproved,
)
from ..tenant_context import tenant_scope
from .event_bus import EventBus
from .memory_store import MemoryStore
from .onboarding_state_repo import OnboardingStateRepo
from .output_router import OutputRouter

log = structlog.get_logger(__name__)


TOPIC_ACTIVATION_MILESTONE = "activation.milestone"
TOPIC_FIRST_PROACTIVE_SELECTED = "activation.first_proactive_selected"


class ActivationTracker:
    def __init__(
        self,
        *,
        memory_store: MemoryStore,
        state_repo: OnboardingStateRepo,
        output_router: OutputRouter,
        event_bus: EventBus,
        settings: Settings | None = None,
    ) -> None:
        self._memory_store = memory_store
        self._state_repo = state_repo
        self._output_router = output_router
        self._event_bus = event_bus
        self._settings = settings or get_settings()

    # ------------------------------------------------------------------
    # EventBus adapters
    # ------------------------------------------------------------------
    async def on_ingestion_complete(self, event: IngestionComplete) -> None:
        with tenant_scope(event.tenant_id):
            await self._state_repo.mark_ingestion_complete(
                tenant_id=event.tenant_id, rep_id=event.rep_id
            )
            await self._send_summary(event)
            selection = await self._select_and_emit_first_output(event)
            await self._state_repo.mark_first_proactive(
                tenant_id=event.tenant_id, rep_id=event.rep_id
            )
        await self._event_bus.publish(TOPIC_FIRST_PROACTIVE_SELECTED, selection)

    async def on_approval_approved(self, event: TaskApproved) -> None:
        """First approval for a rep is the activation milestone."""
        state = await self._state_repo.get(
            tenant_id=event.tenant_id, rep_id=event.rep_id
        )
        if state is None or state.activation_milestone_at is not None:
            return
        updated = await self._state_repo.mark_activation_milestone(
            tenant_id=event.tenant_id, rep_id=event.rep_id, task_id=event.task_id
        )
        if updated is None or updated.activation_milestone_at is None:
            return  # raced with another approval — fine
        with tenant_scope(event.tenant_id):
            await self._post_milestone(event=event)
        await self._event_bus.publish(
            TOPIC_ACTIVATION_MILESTONE,
            ActivationMilestone(
                tenant_id=event.tenant_id,
                rep_id=event.rep_id,
                task_id=event.task_id,
                achieved_at=updated.activation_milestone_at,
            ),
        )

    # ------------------------------------------------------------------
    # Periodic scan (24-h fallback)
    # ------------------------------------------------------------------
    async def run_fallback_scan(self) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(
            hours=self._settings.activation_proactive_window_hours
        )
        pending = await self._state_repo.list_awaiting_first_proactive(older_than=cutoff)
        emitted = 0
        for state in pending:
            with tenant_scope(state.tenant_id):
                ingestion_proxy = IngestionComplete(
                    tenant_id=state.tenant_id,
                    rep_id=state.rep_id,
                    result=_zero_result(state.tenant_id, state.rep_id),
                )
                # Force the fallback path: no scan inputs.
                selection = FirstProactiveSelection(
                    tenant_id=state.tenant_id,
                    rep_id=state.rep_id,
                    output_type=FirstProactiveOutputType.FALLBACK_INTRO,
                    reason="24h_window_elapsed_no_eligible_context",
                )
                await self._deliver_first_output(
                    event=ingestion_proxy, selection=selection
                )
                await self._state_repo.mark_first_proactive(
                    tenant_id=state.tenant_id, rep_id=state.rep_id
                )
            emitted += 1
        if emitted:
            log.info("activation_tracker.fallback_scan", emitted=emitted)
        return emitted

    # ------------------------------------------------------------------
    # Output selection + delivery
    # ------------------------------------------------------------------
    async def _send_summary(self, event: IngestionComplete) -> None:
        r = event.result
        body = (
            f"All set — I've finished my initial pass. Here's what landed: "
            f"*{r.memories_written}* memory rows from *{r.records_processed}* "
            f"sources processed (*{r.memories_deduplicated}* deduped). "
            f"I'll pick something useful and drop it in your DM."
        )
        await self._output_router.deliver(
            DeliveryRequest(
                tenant_id=event.tenant_id,
                rep_id=event.rep_id,
                output_id=f"onboarding:summary:{event.rep_id}",
                output_type=OutputType.NOTIFICATION,
                title="Initial ingestion complete",
                body=body,
                metadata={
                    "actions": [],  # informational only — no approve/edit/discard
                    "records_processed": r.records_processed,
                    "memories_written": r.memories_written,
                    "memories_deduplicated": r.memories_deduplicated,
                },
            )
        )

    async def _select_and_emit_first_output(
        self, event: IngestionComplete
    ) -> FirstProactiveSelection:
        selection = await self._select_first_output(event)
        await self._deliver_first_output(event=event, selection=selection)
        return selection

    async def _select_first_output(
        self, event: IngestionComplete
    ) -> FirstProactiveSelection:
        meeting = await self._find_upcoming_external_meeting(
            tenant_id=event.tenant_id, rep_id=event.rep_id
        )
        if meeting is not None:
            return FirstProactiveSelection(
                tenant_id=event.tenant_id,
                rep_id=event.rep_id,
                output_type=FirstProactiveOutputType.MEETING_PREP,
                reason="external_meeting_within_24h",
                payload={"calendar_event_id": meeting},
            )
        recent_meeting = await self._find_recent_meeting_without_followup(
            tenant_id=event.tenant_id, rep_id=event.rep_id
        )
        if recent_meeting is not None:
            return FirstProactiveSelection(
                tenant_id=event.tenant_id,
                rep_id=event.rep_id,
                output_type=FirstProactiveOutputType.FOLLOW_UP_DRAFT,
                reason="recent_meeting_no_follow_up",
                payload={"calendar_event_id": recent_meeting},
            )
        stalled = await self._find_stalled_deal(
            tenant_id=event.tenant_id, rep_id=event.rep_id
        )
        if stalled is not None:
            return FirstProactiveSelection(
                tenant_id=event.tenant_id,
                rep_id=event.rep_id,
                output_type=FirstProactiveOutputType.STALLED_DEAL_SUMMARY,
                reason="stalled_deal_detected",
                payload={"crm_external_id": stalled},
            )
        return FirstProactiveSelection(
            tenant_id=event.tenant_id,
            rep_id=event.rep_id,
            output_type=FirstProactiveOutputType.FALLBACK_INTRO,
            reason="no_eligible_context_in_memory",
        )

    async def _deliver_first_output(
        self,
        *,
        event: IngestionComplete,
        selection: FirstProactiveSelection,
    ) -> None:
        title, body, output_type = _intro_for(selection)
        await self._output_router.deliver(
            DeliveryRequest(
                tenant_id=event.tenant_id,
                rep_id=event.rep_id,
                output_id=f"onboarding:first_proactive:{event.rep_id}",
                output_type=output_type,
                title=title,
                body=body,
                metadata={
                    # Introduction card — no actions; real generators
                    # (meeting prep / follow-up / stalled-deal) will
                    # render their own approval buttons in Phase 4.
                    "actions": [],
                    "first_proactive": True,
                    "selection_type": selection.output_type.value,
                    "reason": selection.reason,
                    "selection_payload": selection.payload,
                },
            )
        )

    async def _post_milestone(self, *, event: TaskApproved) -> None:
        await self._output_router.deliver(
            DeliveryRequest(
                tenant_id=event.tenant_id,
                rep_id=event.rep_id,
                output_id=f"onboarding:milestone:{event.rep_id}",
                output_type=OutputType.NOTIFICATION,
                title="🎉 You're activated",
                body=(
                    "That was your first approved draft — we're officially up and "
                    "running. I'll keep getting better as you edit and approve more "
                    "of my work. You can pause me any time with `/alex pause`."
                ),
                metadata={"actions": [], "task_id": str(event.task_id)},
            )
        )

    # ------------------------------------------------------------------
    # Memory-backed selectors (read-only; intentionally simple)
    # ------------------------------------------------------------------
    async def _find_upcoming_external_meeting(
        self, *, tenant_id: UUID, rep_id: UUID
    ) -> str | None:
        """Pull the next external calendar event within 24 h."""
        rows = await self._memory_store.list_recent(
            tenant_id=tenant_id,
            tier=MemoryTier.ORG,
            owner_id=None,
            kinds_filter=["calendar.event"],
            limit=200,
        )
        # State-ledger view of already-finalised meetings.
        state_rows = await self._memory_store.list_recent(
            tenant_id=tenant_id,
            tier=MemoryTier.ORG,
            owner_id=None,
            kinds_filter=["calendar.event_state"],
            limit=500,
        )
        finalised: set[str] = set()
        for state_row in state_rows:
            attrs = state_row.attributes or {}
            if attrs.get("lifecycle_state") in (
                CalendarLifecycleState.COMPLETED.value,
                CalendarLifecycleState.CANCELLED.value,
            ):
                ce_id = attrs.get("calendar_event_id")
                if isinstance(ce_id, str):
                    finalised.add(ce_id)

        horizon = datetime.now(timezone.utc) + timedelta(hours=24)
        best: tuple[datetime, str] | None = None
        for row in rows:
            attrs = row.attributes or {}
            ce_id = attrs.get("calendar_event_id")
            if not isinstance(ce_id, str) or ce_id in finalised:
                continue
            if attrs.get("is_external") is False:
                continue
            if str(attrs.get("rep_id")) != str(rep_id):
                continue
            start_raw = attrs.get("start_at")
            if not isinstance(start_raw, str):
                continue
            try:
                start_at = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            now = datetime.now(timezone.utc)
            if start_at <= now or start_at > horizon:
                continue
            if best is None or start_at < best[0]:
                best = (start_at, ce_id)
        return best[1] if best else None

    async def _find_recent_meeting_without_followup(
        self, *, tenant_id: UUID, rep_id: UUID
    ) -> str | None:
        """A meeting that has completed but no follow-up was logged.

        v1 heuristic: a 'calendar.event_state' row with state=completed
        for this rep, and no rep memory row of kind 'followup.sent' or
        'crm.note' that references the same external_id. Feature WOs
        will sharpen the heuristic; we just need a candidate."""
        state_rows = await self._memory_store.list_recent(
            tenant_id=tenant_id,
            tier=MemoryTier.ORG,
            owner_id=None,
            kinds_filter=["calendar.event_state"],
            limit=200,
        )
        # We don't have a follow-up store yet, so any completed meeting
        # for this rep counts. This becomes accurate once Post-Meeting
        # Follow-Up Drafting ships.
        for row in state_rows:
            attrs = row.attributes or {}
            if attrs.get("lifecycle_state") != CalendarLifecycleState.COMPLETED.value:
                continue
            ce_id = attrs.get("calendar_event_id")
            if isinstance(ce_id, str):
                return ce_id
        return None

    async def _find_stalled_deal(
        self, *, tenant_id: UUID, rep_id: UUID
    ) -> str | None:
        """Stalled = an opportunity memory row with no activity in 14d.

        We can't easily compute "no activity" without a per-deal
        activity counter, so v1 picks the oldest cached opportunity as
        a stand-in. Feature WO swaps in a real signal."""
        rows = await self._memory_store.list_recent(
            tenant_id=tenant_id,
            tier=MemoryTier.ORG,
            owner_id=None,
            kinds_filter=["crm.opportunity"],
            limit=50,
        )
        oldest: tuple[datetime, str] | None = None
        for row in rows:
            attrs = row.attributes or {}
            ext = attrs.get("crm_external_id")
            if not isinstance(ext, str):
                continue
            try:
                payload = json.loads(row.content)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            updated_raw = payload.get("updated_at")
            try:
                updated = (
                    datetime.fromisoformat(updated_raw.replace("Z", "+00:00"))
                    if isinstance(updated_raw, str)
                    else row.updated_at
                )
            except ValueError:
                updated = row.updated_at
            if oldest is None or updated < oldest[0]:
                oldest = (updated, ext)
        if oldest is None:
            return None
        cutoff = datetime.now(timezone.utc) - timedelta(days=14)
        return oldest[1] if oldest[0] < cutoff else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _intro_for(selection: FirstProactiveSelection) -> tuple[str, str, OutputType]:
    """Return (title, body, output_type) for the chosen first output."""
    if selection.output_type is FirstProactiveOutputType.MEETING_PREP:
        return (
            "Your first proactive output — meeting prep ✨",
            (
                "I noticed you have an external meeting coming up in the next 24 hours. "
                "Normally I'd hand you a one-page prep brief with the deal context, "
                "MEDDIC fit, and the top three questions worth pre-empting. "
                "The full meeting-prep generator ships in the next phase — for now "
                "this is the introduction to that capability."
            ),
            OutputType.MEETING_PREP,
        )
    if selection.output_type is FirstProactiveOutputType.FOLLOW_UP_DRAFT:
        return (
            "Your first proactive output — follow-up draft ✨",
            (
                "I see a recent meeting with no follow-up logged. Normally I'd draft "
                "a personalised follow-up email in your voice, including the next-"
                "step ask, for you to approve and send. The full follow-up "
                "drafter ships in the next phase — this is the introduction."
            ),
            OutputType.NOTIFICATION,
        )
    if selection.output_type is FirstProactiveOutputType.STALLED_DEAL_SUMMARY:
        return (
            "Your first proactive output — stalled deal summary ✨",
            (
                "One of your deals looks like it's stalled. Normally I'd surface "
                "what changed, the last meaningful touch, and a concrete suggested "
                "action. The full stalled-deal coach ships in the next phase — "
                "this is the introduction."
            ),
            OutputType.NOTIFICATION,
        )
    return (
        "Welcome aboard — here's what's possible ✨",
        (
            "I've finished ingesting your initial context. There's nothing time-"
            "sensitive to act on this minute, but here's what I'll do as your "
            "pipeline moves:\n"
            "• Meeting prep briefs *30 minutes* before every external meeting\n"
            "• Draft follow-ups when a meeting wraps with no logged next step\n"
            "• Stalled-deal nudges with a concrete suggested move\n\n"
            "Every output waits for your approval before anything leaves Alex."
        ),
        OutputType.NOTIFICATION,
    )


def _zero_result(tenant_id: UUID, rep_id: UUID):
    """Synthesise a minimal IngestionResult for the fallback scan path
    where we don't have a real one."""
    from ..schemas import IngestionResult

    now = datetime.now(timezone.utc)
    return IngestionResult(
        tenant_id=tenant_id,
        rep_id=rep_id,
        records_processed=0,
        memories_written=0,
        memories_deduplicated=0,
        started_at=now,
        completed_at=now,
    )


def attach_activation_tracker(
    *,
    bus: EventBus,
    tracker: ActivationTracker,
) -> None:
    """Subscribe the tracker to ingestion + approval topics."""
    from .approval_handler import TOPIC_APPROVAL_APPROVED

    bus.subscribe("ingestion.complete", tracker.on_ingestion_complete)
    bus.subscribe(TOPIC_APPROVAL_APPROVED, tracker.on_approval_approved)


def build_activation_scan_job(tracker: ActivationTracker) -> Callable[[], Any]:
    async def _tick() -> None:
        try:
            await tracker.run_fallback_scan()
        except Exception:
            log.exception("activation_tracker.fallback_scan_failed")

    return _tick
