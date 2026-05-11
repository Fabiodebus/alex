"""FollowUpDraftComposer — generate a follow-up email after a meeting.

Subscribes to ``meeting.completed`` (the EventBus topic published by
:class:`MeetingCompletionScan` or the cancellation/completion paths).
For each completed external meeting:

1. Fetch the transcript via :class:`TranscriptFetcher`. A miss is a
   first-class state: the composer still drafts something, labelled
   "no transcript was used."
2. Detect multi-company invites — when >1 external email domain
   appears in the attendee list, the composer *pauses* and asks the
   rep to pick the recipient cohort. The "card" in that case is a
   notification with no action buttons (in v1; an interactive picker
   ships in a follow-on).
3. Pull :class:`VoiceProfile` via :class:`VoiceApplicator` so the
   prompt context carries the rep's preferred greeting / sign-off /
   register / Sie/Du selection.
4. Call AgentBackend; parse JSON; wrap into a
   :class:`PendingTask` (``task_type='email.send'``). After approval
   the :class:`ApprovedActionDispatcher` routes the validated send
   request to :class:`EmailSendClient`.

EditDiff already flows from ApprovalHandler to VoiceUpdater via the
existing approval pipeline — no extra wiring needed here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import structlog

from ..schemas import (
    AgentResponse,
    CalendarEvent,
    DeliveryRequest,
    FollowUpDraft,
    GermanRegister,
    MeetingCompleted,
    MemoryTier,
    OutputType,
    PendingTaskCreate,
    VoiceApplication,
    VoiceLanguage,
)
from ..tenant_context import tenant_scope
from .agent_backend import AgentBackend
from .approval_gate import ApprovalGate
from .composer_base import ComposerContext, ComposerPrompt, ProactiveComposer
from .memory_store import MemoryStore
from .output_router import OutputRouter
from .transcript_fetcher import TranscriptFetcher
from ..schemas import TranscriptRequest, TranscriptResult
from .voice_applicator import AccountContext, VoiceApplicator

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class _FollowUpContext(ComposerContext):
    completed: MeetingCompleted | None = None
    calendar_event: CalendarEvent | None = None
    transcript: TranscriptResult | None = None
    external_attendees: list[str] = None  # type: ignore[assignment]
    voice_application: VoiceApplication | None = None
    voice_prompt_fragment: str = ""
    language: VoiceLanguage = VoiceLanguage.EN
    de_register: GermanRegister | None = None
    multi_company: bool = False
    candidate_groups: list[list[str]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.external_attendees is None:
            self.external_attendees = []
        if self.candidate_groups is None:
            self.candidate_groups = []


class FollowUpDraftComposer(ProactiveComposer):
    name = "follow_up_draft"

    def __init__(
        self,
        *,
        agent_backend: AgentBackend,
        memory_store: MemoryStore,
        transcript_fetcher: TranscriptFetcher,
        voice_applicator: VoiceApplicator,
        approval_gate: ApprovalGate,
        output_router: OutputRouter,
    ) -> None:
        super().__init__(agent_backend=agent_backend, memory_store=memory_store)
        self._transcript_fetcher = transcript_fetcher
        self._voice_applicator = voice_applicator
        self._approval_gate = approval_gate
        self._output_router = output_router

    # ------------------------------------------------------------------
    # EventBus entry
    # ------------------------------------------------------------------
    async def handle_meeting_completed(self, completed: MeetingCompleted) -> None:
        with tenant_scope(completed.tenant_id):
            await self.compose(completed)

    # ------------------------------------------------------------------
    # ProactiveComposer contract
    # ------------------------------------------------------------------
    async def gather_context(self, trigger_payload: Any) -> _FollowUpContext:
        if not isinstance(trigger_payload, MeetingCompleted):
            raise TypeError("FollowUpDraftComposer expects a MeetingCompleted payload")
        completed = trigger_payload
        calendar_event = await self._fetch_calendar_event(
            tenant_id=completed.tenant_id, calendar_event_id=completed.calendar_event_id
        )
        external_attendees, language, candidate_groups, multi_company = _categorise_attendees(
            calendar_event=calendar_event, completed=completed
        )
        transcript = await self._transcript_fetcher.fetch(
            TranscriptRequest(
                tenant_id=completed.tenant_id,
                rep_id=completed.rep_id,
                calendar_event_id=completed.calendar_event_id,
            )
        )
        if transcript is not None and transcript.language in (VoiceLanguage.DE, VoiceLanguage.EN):
            language = transcript.language

        voice = await self._voice_applicator.apply(
            tenant_id=completed.tenant_id,
            rep_id=completed.rep_id,
            context=AccountContext(
                account_external_id=completed.account_external_id,
                language=language,
            ),
        )

        return _FollowUpContext(
            tenant_id=completed.tenant_id,
            rep_id=completed.rep_id,
            correlation_key=f"follow_up:{completed.calendar_event_id}",
            metadata={
                "title": completed.title,
                "end_at": completed.end_at.isoformat(),
            },
            completed=completed,
            calendar_event=calendar_event,
            transcript=transcript,
            external_attendees=external_attendees,
            voice_application=voice.application,
            voice_prompt_fragment=voice.prompt_fragment,
            language=language,
            de_register=voice.application.de_register,
            multi_company=multi_company,
            candidate_groups=candidate_groups,
        )

    async def build_prompt(self, context: ComposerContext) -> ComposerPrompt:
        assert isinstance(context, _FollowUpContext)
        completed = context.completed
        assert completed is not None

        transcript_block = (
            f"TRANSCRIPT:\n{context.transcript.transcript}"
            if context.transcript is not None
            else "TRANSCRIPT: (no transcript available — draft from calendar + memory context only)"
        )

        register_hint = (
            f"\nGerman register: address the reader as {context.de_register.value if context.de_register else 'mixed'}."
            if context.language is VoiceLanguage.DE else ""
        )
        recipients_hint = (
            ", ".join(context.external_attendees) or "all external attendees"
        )

        user_prompt = (
            f"You are drafting a follow-up email on behalf of the rep after an external "
            f"meeting. Output a JSON object with keys: subject, body, to (list).\n\n"
            f"MEETING: {completed.title or 'External meeting'} "
            f"(ended {completed.end_at.isoformat()})\n"
            f"DEFAULT RECIPIENTS: {recipients_hint}\n"
            f"LANGUAGE: {context.language.value}{register_hint}\n\n"
            f"REP VOICE:\n{context.voice_prompt_fragment}\n\n"
            f"{transcript_block}\n\n"
            f"Keep it tight — opener, 2 to 4 sentences max for body content, "
            f"reference the meeting's outcome, propose the concrete next step, "
            f"and close in the rep's voice. Never invent commitments. "
            f"If the transcript is missing, write a sober 'recap-and-next-step' "
            f"draft and prepend 'Note for rep: no transcript was used' to the body."
        )
        system_prompt = (
            "You are Alex, an AI Chief of Staff for B2B sales reps in DACH. "
            "You write follow-ups that sound like the rep — never generic. "
            "Output valid JSON only."
        )
        return ComposerPrompt(system_prompt=system_prompt, user_prompt=user_prompt)

    async def wrap(
        self, *, context: ComposerContext, response: AgentResponse
    ) -> FollowUpDraft:
        assert isinstance(context, _FollowUpContext)
        completed = context.completed
        assert completed is not None

        if context.multi_company:
            return await self._post_multi_company_pause(context)

        data = _parse_draft_json(response.text)
        draft = FollowUpDraft(
            subject=data.get("subject") or _fallback_subject(completed),
            body=data.get("body") or _fallback_body(context),
            to=_pick_recipients(data.get("to"), context.external_attendees),
            language=context.language,
            de_register=context.de_register,
            voice_application=context.voice_application,
            used_transcript=context.transcript is not None,
            multi_company_pending=False,
            candidate_recipient_groups=context.candidate_groups,
        )
        await self._open_approval_card(context=context, draft=draft)
        return draft

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    async def _fetch_calendar_event(
        self, *, tenant_id, calendar_event_id: str
    ) -> CalendarEvent | None:
        rows = await self._memory_store.list_recent(
            tenant_id=tenant_id,
            tier=MemoryTier.ORG,
            owner_id=None,
            kinds_filter=["calendar.event"],
            limit=200,
        )
        for row in rows:
            attrs = row.attributes or {}
            if attrs.get("calendar_event_id") == calendar_event_id:
                try:
                    return CalendarEvent.model_validate(json.loads(row.content))
                except Exception:
                    return None
        return None

    async def _open_approval_card(
        self,
        *,
        context: _FollowUpContext,
        draft: FollowUpDraft,
    ) -> None:
        completed = context.completed
        assert completed is not None
        idempotency_key = f"follow_up:{completed.calendar_event_id}:{uuid4()}"
        task = await self._approval_gate.create_pending_task(
            PendingTaskCreate(
                tenant_id=completed.tenant_id,
                rep_id=completed.rep_id,
                task_type="email.send",
                title=f"Follow-up draft: {completed.title or 'External meeting'}",
                payload={
                    "subject": draft.subject,
                    "body": draft.body,
                    "to": draft.to,
                    "language": draft.language.value,
                    "de_register": draft.de_register.value if draft.de_register else None,
                    "calendar_event_id": completed.calendar_event_id,
                    "idempotency_key": idempotency_key,
                    "voice_application": draft.voice_application.model_dump(mode="json")
                    if draft.voice_application else None,
                    "used_transcript": draft.used_transcript,
                },
            )
        )
        body = (
            f"*Draft to:* {', '.join(draft.to) or '(no recipients)'}\n"
            f"*Subject:* {draft.subject}\n\n"
            f"{draft.body}\n\n"
            + ("_No transcript was used._" if not draft.used_transcript else "")
        )
        actions = [
            {
                "action_id": "alex.approve",
                "label": "✅ Approve & send",
                "style": "primary",
                "value": {
                    "task_id": str(task.task_id),
                    "rep_id": str(completed.rep_id),
                    "tenant_id": str(completed.tenant_id),
                    "action": "approve",
                },
            },
            {
                "action_id": "alex.edit",
                "label": "✏️ Edit",
                "value": {
                    "task_id": str(task.task_id),
                    "rep_id": str(completed.rep_id),
                    "tenant_id": str(completed.tenant_id),
                    "action": "edit",
                },
            },
            {
                "action_id": "alex.discard",
                "label": "🗑️ Discard",
                "style": "danger",
                "value": {
                    "task_id": str(task.task_id),
                    "rep_id": str(completed.rep_id),
                    "tenant_id": str(completed.tenant_id),
                    "action": "discard",
                },
            },
        ]
        await self._output_router.deliver(
            DeliveryRequest(
                tenant_id=completed.tenant_id,
                rep_id=completed.rep_id,
                task_id=task.task_id,
                output_id=f"follow_up:{completed.calendar_event_id}",
                output_type=OutputType.NOTIFICATION,
                title=f"Follow-up ready: {completed.title or 'External meeting'}",
                body=body,
                metadata={
                    "actions": actions,
                    "draft": draft.model_dump(mode="json"),
                    "calendar_event_id": completed.calendar_event_id,
                },
            )
        )

    async def _post_multi_company_pause(
        self, context: _FollowUpContext
    ) -> FollowUpDraft:
        completed = context.completed
        assert completed is not None
        groups_listing = "\n".join(
            f"• {', '.join(group)}" for group in context.candidate_groups
        )
        await self._output_router.deliver(
            DeliveryRequest(
                tenant_id=completed.tenant_id,
                rep_id=completed.rep_id,
                output_id=f"follow_up_pending:{completed.calendar_event_id}",
                output_type=OutputType.NOTIFICATION,
                title="Follow-up paused — multi-company meeting",
                body=(
                    "The meeting had attendees from multiple companies. Tell me who the "
                    "follow-up should go to and I'll draft it.\n\n" + groups_listing
                ),
                metadata={
                    "actions": [],  # interactive picker is a follow-on
                    "candidate_groups": context.candidate_groups,
                    "calendar_event_id": completed.calendar_event_id,
                },
            )
        )
        log.info(
            "follow_up_draft.paused_multi_company",
            calendar_event_id=completed.calendar_event_id,
            tenant_id=str(completed.tenant_id),
            groups=len(context.candidate_groups),
        )
        return FollowUpDraft(
            subject="",
            body="",
            to=[],
            language=context.language,
            de_register=context.de_register,
            voice_application=context.voice_application,
            used_transcript=False,
            multi_company_pending=True,
            candidate_recipient_groups=context.candidate_groups,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _parse_draft_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[-1] if "```" in text[3:] else text[3:]
        text = text.rstrip("`").strip()
        if text.startswith("json\n"):
            text = text[len("json\n"):]
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {}


def _fallback_subject(completed: MeetingCompleted) -> str:
    base = completed.title or "our conversation"
    return f"Quick follow-up — {base}"


def _fallback_body(context: _FollowUpContext) -> str:
    note = "Note for rep: no transcript was used.\n\n" if context.transcript is None else ""
    return (
        f"{note}Hi,\n\nThanks for the time today. I wanted to land the next "
        f"step on your radar.\n\n[Add concrete next step here]\n\n"
        f"Best regards"
    )


def _pick_recipients(raw: Any, fallback: list[str]) -> list[str]:
    if isinstance(raw, list) and all(isinstance(x, str) for x in raw):
        return raw or fallback
    return fallback


def _categorise_attendees(
    *,
    calendar_event: CalendarEvent | None,
    completed: MeetingCompleted,
) -> tuple[list[str], VoiceLanguage, list[list[str]], bool]:
    """Returns (external_attendees, default_language, candidate_groups,
    multi_company)."""
    if calendar_event is None:
        return ([], VoiceLanguage.EN, [], False)

    rep_domain = (calendar_event.rep_email or "").split("@", 1)[-1].lower()
    by_domain: dict[str, list[str]] = {}
    for attendee in calendar_event.attendees:
        if not attendee.email:
            continue
        domain = attendee.email.split("@", 1)[-1].lower()
        if domain == rep_domain:
            continue
        by_domain.setdefault(domain, []).append(attendee.email)

    candidate_groups = [emails for emails in by_domain.values() if emails]
    multi_company = len(candidate_groups) > 1
    external_attendees: list[str] = [e for group in candidate_groups for e in group]

    # Naive language pick: if any external domain ends in .de/.at/.ch
    # we default to German. The transcript-detected language wins later.
    language = VoiceLanguage.EN
    if any(d.endswith((".de", ".at", ".ch")) for d in by_domain):
        language = VoiceLanguage.DE
    return external_attendees, language, candidate_groups, multi_company


