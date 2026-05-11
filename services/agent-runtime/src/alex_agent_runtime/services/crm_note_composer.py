"""CRMNoteComposer + MEDDICMapper — generate the post-meeting CRM review.

Subscribes to ``meeting.completed`` (same trigger as
FollowUpDraftComposer). Produces a single review package:

* The structured note body (summary, key discussion points, decisions
  made, next steps, new contacts identified).
* Proposed :class:`FieldUpdate` payloads — typically a stage
  transition + MEDDIC field updates when the tenant has
  ``meddic_enabled``.

Every proposed FieldUpdate is *re-validated* through
:class:`CRMValidator` before the approval card is shown to the rep —
the same write-safety contract WO #10 set up. Validation rejections
are stripped (and logged), so the rep never sees a write that would
fail downstream.

The whole package is wrapped into a single ``task_type='crm.write'``
PendingTask via :class:`ApprovalGate`. After approval the existing
:class:`ApprovedActionDispatcher.crm.write` branch routes to
:class:`CRMWriter` which handles both the field updates and the
attached note in one call.

v1 = one approve/discard per package. Independent per-component
approval is a documented future enhancement.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import structlog

from ..schemas import (
    AgentResponse,
    CRMNote,
    CRMNoteReview,
    CRMPlatform,
    CRMRecord,
    CRMRecordKind,
    DeliveryRequest,
    FieldUpdate,
    MeetingCompleted,
    MemoryTier,
    OutputType,
    PendingTaskCreate,
    TranscriptRequest,
    TranscriptResult,
)
from ..tenant_context import tenant_scope
from .agent_backend import AgentBackend
from .approval_gate import ApprovalGate
from .composer_base import ComposerContext, ComposerPrompt, ProactiveComposer
from .crm_reader import CRMReader
from .crm_validator import CRMValidator
from .memory_store import MemoryStore
from .output_router import OutputRouter
from .tenant_flags import FLAG_MEDDIC_ENABLED, TenantFlagRepo
from .transcript_fetcher import TranscriptFetcher

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class _NoteContext(ComposerContext):
    completed: MeetingCompleted | None = None
    opportunity: CRMRecord | None = None
    account: CRMRecord | None = None
    transcript: TranscriptResult | None = None
    meddic_enabled: bool = False
    platform: CRMPlatform | None = None
    external_id: str | None = None
    kind: CRMRecordKind = CRMRecordKind.OPPORTUNITY
    raw_field_proposals: list[dict[str, Any]] = field(default_factory=list)
    raw_meddic_mappings: list[dict[str, Any]] = field(default_factory=list)


class CRMNoteComposer(ProactiveComposer):
    name = "crm_note"

    def __init__(
        self,
        *,
        agent_backend: AgentBackend,
        memory_store: MemoryStore,
        crm_reader: CRMReader,
        crm_validator: CRMValidator,
        approval_gate: ApprovalGate,
        output_router: OutputRouter,
        transcript_fetcher: TranscriptFetcher,
        tenant_flags: TenantFlagRepo,
    ) -> None:
        super().__init__(agent_backend=agent_backend, memory_store=memory_store)
        self._crm_reader = crm_reader
        self._crm_validator = crm_validator
        self._approval_gate = approval_gate
        self._output_router = output_router
        self._transcript_fetcher = transcript_fetcher
        self._tenant_flags = tenant_flags

    # ------------------------------------------------------------------
    # EventBus entry
    # ------------------------------------------------------------------
    async def handle_meeting_completed(self, completed: MeetingCompleted) -> None:
        if completed.opportunity_external_id is None or completed.crm_platform is None:
            log.info(
                "crm_note.skip.no_opportunity",
                calendar_event_id=completed.calendar_event_id,
            )
            return
        with tenant_scope(completed.tenant_id):
            await self.compose(completed)

    # ------------------------------------------------------------------
    # ProactiveComposer contract
    # ------------------------------------------------------------------
    async def gather_context(self, trigger_payload: Any) -> _NoteContext:
        if not isinstance(trigger_payload, MeetingCompleted):
            raise TypeError("CRMNoteComposer expects a MeetingCompleted payload")
        completed = trigger_payload
        platform = completed.crm_platform
        assert platform is not None and completed.opportunity_external_id is not None

        transcript = await self._transcript_fetcher.fetch(
            TranscriptRequest(
                tenant_id=completed.tenant_id,
                rep_id=completed.rep_id,
                calendar_event_id=completed.calendar_event_id,
            )
        )
        opportunity = await self._crm_reader.fetch_record(
            tenant_id=completed.tenant_id,
            platform=platform,
            kind=CRMRecordKind.OPPORTUNITY,
            external_id=completed.opportunity_external_id,
        )
        account = None
        if completed.account_external_id is not None:
            account = await self._crm_reader.fetch_record(
                tenant_id=completed.tenant_id,
                platform=platform,
                kind=CRMRecordKind.ACCOUNT,
                external_id=completed.account_external_id,
            )
        meddic_enabled = await self._tenant_flags.get_bool(
            tenant_id=completed.tenant_id, flag=FLAG_MEDDIC_ENABLED, default=False
        )
        return _NoteContext(
            tenant_id=completed.tenant_id,
            rep_id=completed.rep_id,
            correlation_key=f"crm_note:{completed.calendar_event_id}",
            metadata={
                "title": completed.title,
                "end_at": completed.end_at.isoformat(),
            },
            completed=completed,
            opportunity=opportunity,
            account=account,
            transcript=transcript,
            meddic_enabled=meddic_enabled,
            platform=platform,
            external_id=completed.opportunity_external_id,
            kind=CRMRecordKind.OPPORTUNITY,
        )

    async def build_prompt(self, context: ComposerContext) -> ComposerPrompt:
        assert isinstance(context, _NoteContext)
        completed = context.completed
        assert completed is not None

        opp_block = "  (opportunity not in CRM cache)"
        if context.opportunity is not None:
            opp = context.opportunity
            opp_block = (
                f"  name: {opp.name}\n"
                f"  stage: {opp.stage}\n"
                f"  amount: {opp.amount_cents} {opp.currency or ''}\n"
                f"  meddic: {opp.meddic}"
            )
        transcript_block = (
            f"TRANSCRIPT:\n{context.transcript.transcript}"
            if context.transcript is not None
            else "TRANSCRIPT: (no recording available — base the note on calendar context only)"
        )
        meddic_directive = (
            "MEDDIC fields you may populate: M (metrics), E (economic_buyer), "
            "D (decision_criteria), DP (decision_process), I (identify_pain), "
            "C (champion). For each, propose a value when the transcript supports "
            "it; list as `meddic_mappings`. Surface gaps in `meddic_gaps`."
            if context.meddic_enabled
            else "MEDDIC is disabled for this tenant — return [] for meddic_mappings and meddic_gaps."
        )

        user_prompt = (
            f"You are writing a structured CRM note + proposed field updates for the "
            f"rep to approve before anything is written to the CRM.\n\n"
            f"MEETING: {completed.title or 'External meeting'}\n"
            f"OPPORTUNITY:\n{opp_block}\n\n"
            f"{transcript_block}\n\n"
            f"Output JSON with keys: summary, key_points, decisions, next_steps, "
            f"new_contacts, stage_change_proposal, field_updates, meddic_mappings, "
            f"meddic_gaps, note_body.\n"
            f"  - summary: 1-2 sentence overall recap.\n"
            f"  - key_points / decisions / next_steps: lists of short bullets.\n"
            f"  - new_contacts: list of {{name, email?, role?}} for people mentioned "
            f"and not already in CRM.\n"
            f"  - stage_change_proposal: {{from, to, evidence}} when the transcript "
            f"clearly justifies a stage transition, otherwise null.\n"
            f"  - field_updates: list of {{field_name, current_value, proposed_value, "
            f"reason}} — only fields you have strong evidence for.\n"
            f"  - {meddic_directive}\n"
            f"  - note_body: a clean human-readable version of the note for the "
            f"CRMWriter to attach.\n"
            f"Never invent details. If a field can't be supported by the transcript, "
            f"omit it."
        )
        system_prompt = (
            "You are Alex, an AI Chief of Staff for B2B sales reps in DACH. "
            "You ground every claim in the transcript or CRM context. "
            "Output valid JSON only."
        )
        return ComposerPrompt(system_prompt=system_prompt, user_prompt=user_prompt)

    async def wrap(
        self, *, context: ComposerContext, response: AgentResponse
    ) -> CRMNoteReview:
        assert isinstance(context, _NoteContext)
        completed = context.completed
        assert completed is not None
        assert context.platform is not None and context.external_id is not None

        data = _parse_json(response.text)
        validated_updates = self._validate_field_updates(
            raw=data.get("field_updates") or [],
            stage_proposal=data.get("stage_change_proposal"),
            context=context,
        )
        review = CRMNoteReview(
            summary=data.get("summary") or _fallback_summary(context),
            key_points=_str_list(data.get("key_points")),
            decisions=_str_list(data.get("decisions")),
            next_steps=_str_list(data.get("next_steps")),
            new_contacts=_dict_list(data.get("new_contacts")),
            stage_change_proposal=data.get("stage_change_proposal")
            if isinstance(data.get("stage_change_proposal"), dict)
            else None,
            field_updates=validated_updates,
            meddic_mappings=_dict_list(data.get("meddic_mappings")) if context.meddic_enabled else [],
            meddic_gaps=_str_list(data.get("meddic_gaps")) if context.meddic_enabled else [],
            note_body=data.get("note_body") or _fallback_note_body(context),
            crm_platform=context.platform,
            crm_kind=context.kind,
            crm_external_id=context.external_id,
        )
        await self._open_approval_card(context=context, review=review)
        return review

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def _validate_field_updates(
        self,
        *,
        raw: list[Any],
        stage_proposal: Any,
        context: _NoteContext,
    ) -> list[dict[str, Any]]:
        assert context.platform is not None and context.external_id is not None
        out: list[dict[str, Any]] = []

        # Promote the stage_change_proposal into a regular field update.
        stage_field_name = _stage_field_for(context.platform)
        if isinstance(stage_proposal, dict) and stage_field_name:
            raw = [
                {
                    "field_name": stage_field_name,
                    "current_value": stage_proposal.get("from"),
                    "proposed_value": stage_proposal.get("to"),
                    "reason": stage_proposal.get("evidence"),
                },
                *raw,
            ]

        for entry in raw:
            if not isinstance(entry, dict):
                continue
            field_name = entry.get("field_name")
            if not isinstance(field_name, str):
                continue
            try:
                update = FieldUpdate(
                    platform=context.platform,
                    kind=context.kind,
                    external_id=context.external_id,
                    field_name=field_name,
                    current_value=entry.get("current_value"),
                    proposed_value=entry.get("proposed_value"),
                )
            except Exception:
                log.warning(
                    "crm_note.malformed_field_update",
                    field_name=field_name,
                )
                continue
            result = self._crm_validator.validate(update)
            if not result.is_valid or result.validated is None:
                log.info(
                    "crm_note.field_update_rejected",
                    field_name=field_name,
                    code=result.error.code if result.error else None,
                )
                continue
            out.append(
                {
                    "field_name": field_name,
                    "current_value": entry.get("current_value"),
                    "proposed_value": result.validated.normalized_value,
                    "platform_field_id": result.validated.platform_field_id,
                    "reason": entry.get("reason"),
                }
            )
        return out

    # ------------------------------------------------------------------
    # Delivery
    # ------------------------------------------------------------------
    async def _open_approval_card(
        self,
        *,
        context: _NoteContext,
        review: CRMNoteReview,
    ) -> None:
        completed = context.completed
        assert completed is not None
        assert context.platform is not None and context.external_id is not None

        idempotency_key = f"crm_note:{completed.calendar_event_id}:{uuid4()}"
        note = CRMNote(
            platform=context.platform,
            kind=context.kind,
            external_id=context.external_id,
            body=review.note_body,
            title=f"Meeting recap — {completed.title or completed.calendar_event_id}",
        )
        task = await self._approval_gate.create_pending_task(
            PendingTaskCreate(
                tenant_id=completed.tenant_id,
                rep_id=completed.rep_id,
                task_type="crm.write",
                title=f"CRM review: {completed.title or 'External meeting'}",
                payload={
                    "platform": context.platform.value,
                    "kind": context.kind.value,
                    "external_id": context.external_id,
                    "field_updates": [
                        {
                            "field_name": u["field_name"],
                            "current_value": u["current_value"],
                            "proposed_value": u["proposed_value"],
                        }
                        for u in review.field_updates
                    ],
                    "notes": [
                        {
                            "title": note.title,
                            "body": note.body,
                        }
                    ],
                    "idempotency_key": idempotency_key,
                    "review": review.model_dump(mode="json"),
                },
            )
        )

        body = _render_review_body(review=review, meeting_title=completed.title)
        actions = [
            {
                "action_id": "alex.approve",
                "label": "✅ Approve write",
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
                output_id=f"crm_note:{completed.calendar_event_id}",
                output_type=OutputType.NOTIFICATION,
                title=f"CRM review ready — {completed.title or 'External meeting'}",
                body=body,
                metadata={
                    "actions": actions,
                    "review": review.model_dump(mode="json"),
                    "calendar_event_id": completed.calendar_event_id,
                },
            )
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _parse_json(raw: str) -> dict[str, Any]:
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


def _str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x) for x in value if isinstance(x, (str, int, float))]
    return []


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    return []


def _stage_field_for(platform: CRMPlatform) -> str | None:
    return {
        CRMPlatform.CLOSE: "status_id",
        CRMPlatform.HUBSPOT: "dealstage",
        CRMPlatform.SALESFORCE: "StageName",
        CRMPlatform.PIPEDRIVE: "stage_id",
    }.get(platform)


def _fallback_summary(context: _NoteContext) -> str:
    completed = context.completed
    if completed is None:
        return "Meeting recap (no summary generated)."
    return (
        f"Meeting recap for {completed.title or 'external meeting'} "
        f"({completed.end_at.isoformat()})."
    )


def _fallback_note_body(context: _NoteContext) -> str:
    completed = context.completed
    title = completed.title if completed else "External meeting"
    return f"Recap for {title}: [no model output — write the note manually]."


def _render_review_body(
    *, review: CRMNoteReview, meeting_title: str | None
) -> str:
    parts: list[str] = []
    parts.append(f"*Summary:* {review.summary}")
    if review.key_points:
        parts.append("*Key points:*\n" + "\n".join(f"• {p}" for p in review.key_points))
    if review.decisions:
        parts.append("*Decisions:*\n" + "\n".join(f"• {d}" for d in review.decisions))
    if review.next_steps:
        parts.append("*Next steps:*\n" + "\n".join(f"• {s}" for s in review.next_steps))
    if review.new_contacts:
        contact_lines = []
        for c in review.new_contacts:
            name = c.get("name") or c.get("email") or "unknown"
            role = c.get("role") or "role unknown"
            contact_lines.append(f"• {name} ({role})")
        parts.append("*New contacts to add:*\n" + "\n".join(contact_lines))
    if review.stage_change_proposal:
        stage = review.stage_change_proposal
        parts.append(
            f"*Stage change proposal:* {stage.get('from', '?')} → "
            f"{stage.get('to', '?')} — {stage.get('evidence', 'no rationale provided')}"
        )
    if review.field_updates:
        update_lines = [
            f"• `{u['field_name']}`: {u.get('current_value', '∅')} → "
            f"{u.get('proposed_value', '∅')}"
            for u in review.field_updates
        ]
        parts.append("*Proposed CRM field updates:*\n" + "\n".join(update_lines))
    if review.meddic_gaps:
        parts.append("*MEDDIC gaps:*\n" + "\n".join(f"• {g}" for g in review.meddic_gaps))
    parts.append("\n_Approve to commit the note + field updates to the CRM. Discard to throw it away._")
    return "\n\n".join(parts)
