"""MeetingBriefComposer — generate a structured meeting prep brief.

Triggered by the periodic :class:`MeetingBriefScan` when a detected
external meeting is inside its lead-time window. Subscribes to
``meeting.detected`` too so the scan-on-create case is covered (a
brief that should fire immediately because the meeting is < 30 min
out lands as soon as classification completes).

Output shape: a :class:`MeetingBrief` (account context, attendee
profiles, last-touch, 3–5 talking points, recommended CTA, MEDDIC
gaps when configured) wrapped into a rep-facing
:class:`DeliveryRequest` posted via :class:`OutputRouter`. The card
exposes inline thumbs up/down buttons (the existing
``alex.feedback`` action handler captures them).

No approval is required — briefs are informational; they don't
trigger any external action. The blueprint reserves the approval
flow for proposed CRM writes / emails (WO #18/#19).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import structlog

from ..config import Settings, get_settings
from ..schemas import (
    AgentResponse,
    AttendeeProfile,
    CRMPlatform,
    CRMRecord,
    CRMRecordKind,
    DeliveryRequest,
    IntegrationEvent,
    MeetingBrief,
    MeetingDetected,
    MemoryTier,
    OutputType,
)
from ..tenant_context import tenant_scope
from .agent_backend import AgentBackend
from .composer_base import ComposerContext, ComposerPrompt, ProactiveComposer
from .crm_reader import CRMReader
from .memory_store import MemoryStore
from .output_router import OutputRouter
from .tenant_flags import FLAG_MEDDIC_ENABLED, TenantFlagRepo

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class _BriefContext(ComposerContext):
    detected: MeetingDetected | None = None
    opportunity: CRMRecord | None = None
    account: CRMRecord | None = None
    meddic_enabled: bool = False


class MeetingBriefComposer(ProactiveComposer):
    name = "meeting_brief"

    def __init__(
        self,
        *,
        agent_backend: AgentBackend,
        memory_store: MemoryStore,
        crm_reader: CRMReader,
        output_router: OutputRouter,
        tenant_flags: TenantFlagRepo,
        settings: Settings | None = None,
    ) -> None:
        super().__init__(agent_backend=agent_backend, memory_store=memory_store)
        self._crm_reader = crm_reader
        self._output_router = output_router
        self._tenant_flags = tenant_flags
        self._settings = settings or get_settings()

    # ------------------------------------------------------------------
    # FeatureRouter / scheduler entry points
    # ------------------------------------------------------------------
    async def handle_meeting_detected(self, detected: MeetingDetected) -> None:
        """Subscribed on the EventBus. The scan-on-create path: when a
        meeting is detected inside its lead window the scan picks it up
        anyway; this hook handles the rest."""
        now = datetime.now(timezone.utc)
        # If the meeting starts within the lead window we fire now.
        if detected.trigger_at <= now < detected.end_at:
            with tenant_scope(detected.tenant_id):
                await self.compose(detected)

    async def handle_integration_event(self, event: IntegrationEvent) -> None:
        """Alternative entry for FeatureRouter wiring (kind="meeting.detected").
        Most installations will subscribe directly via the EventBus."""
        try:
            detected = MeetingDetected.model_validate(event.payload)
        except Exception:
            log.warning(
                "meeting_brief.invalid_event",
                event_id=event.event_id,
                payload_keys=list(event.payload.keys()),
            )
            return
        await self.handle_meeting_detected(detected)

    # ------------------------------------------------------------------
    # ProactiveComposer contract
    # ------------------------------------------------------------------
    async def gather_context(self, trigger_payload: Any) -> _BriefContext:
        if not isinstance(trigger_payload, MeetingDetected):
            raise TypeError("MeetingBriefComposer expects a MeetingDetected payload")
        detected = trigger_payload
        opportunity = await self._fetch_opportunity(detected)
        account = await self._fetch_account(detected)
        meddic_enabled = await self._tenant_flags.get_bool(
            tenant_id=detected.tenant_id, flag=FLAG_MEDDIC_ENABLED, default=False
        )
        return _BriefContext(
            tenant_id=detected.tenant_id,
            rep_id=detected.rep_id,
            correlation_key=f"brief:{detected.calendar_event_id}",
            metadata={
                "title": detected.title,
                "start_at": detected.start_at.isoformat(),
                "is_external": detected.is_external,
            },
            detected=detected,
            opportunity=opportunity,
            account=account,
            meddic_enabled=meddic_enabled,
        )

    async def build_prompt(self, context: ComposerContext) -> ComposerPrompt:
        assert isinstance(context, _BriefContext)
        detected = context.detected
        assert detected is not None

        attendees = "\n".join(
            f"  - {p.email} ({'external' if p.is_external else 'internal'}"
            f"{', org_account=' + p.crm_account_external_id if p.crm_account_external_id else ''}"
            f"{', org_contact=' + p.crm_contact_external_id if p.crm_contact_external_id else ''}"
            ")"
            for p in detected.attendee_profiles
        ) or "  (no attendee profiles)"

        opp_block = "  (no linked opportunity in CRM)"
        if context.opportunity is not None:
            opp = context.opportunity
            opp_block = (
                f"  name: {opp.name}\n"
                f"  stage: {opp.stage}\n"
                f"  amount: {opp.amount_cents} {opp.currency or ''}\n"
                f"  probability: {opp.probability}\n"
                f"  close_date: {opp.close_date.isoformat() if opp.close_date else 'unset'}\n"
                f"  meddic: {opp.meddic}"
            )

        acct_block = "  (no linked account in CRM)"
        if context.account is not None:
            acct = context.account
            acct_block = (
                f"  name: {acct.name}\n"
                f"  domain: {acct.domain}\n"
                f"  industry: {acct.industry}\n"
                f"  country: {acct.country}"
            )

        user_prompt = (
            f"You are preparing a sales rep for an upcoming external meeting. "
            f"Produce a structured brief.\n\n"
            f"MEETING\n"
            f"  title: {detected.title}\n"
            f"  starts: {detected.start_at.isoformat()}\n"
            f"  ends: {detected.end_at.isoformat()}\n\n"
            f"ATTENDEES (external attendees outside the rep's domain are the buyers)\n"
            f"{attendees}\n\n"
            f"OPPORTUNITY\n"
            f"{opp_block}\n\n"
            f"ACCOUNT\n"
            f"{acct_block}\n\n"
            f"Write the brief as JSON matching this schema:\n"
            f'{{"account_context": "...", "attendee_profiles": [{{"email": "...", "role": "...", "context": "..."}}], "last_touch_summary": "...", "open_commitments": ["..."], "talking_points": ["...", "..."], "recommended_cta": "...", "meddic_gaps": ["..."]}}\n'
            f"Talking points: 3 to 5 concrete items. "
            f"MEDDIC gaps: only populate when MEDDIC is configured for this tenant "
            f"(currently {'enabled' if context.meddic_enabled else 'disabled'}); "
            f"otherwise return [].\n"
        )
        system_prompt = (
            "You are Alex, an AI Chief of Staff for B2B sales reps in DACH. "
            "You write tight, plain-language briefs. You never invent CRM "
            "data — if a field is missing, say so. Output valid JSON only."
        )
        return ComposerPrompt(system_prompt=system_prompt, user_prompt=user_prompt)

    async def wrap(
        self, *, context: ComposerContext, response: AgentResponse
    ) -> MeetingBrief:
        assert isinstance(context, _BriefContext)
        detected = context.detected
        assert detected is not None

        brief_data = _parse_brief_json(response.text)
        unknown = [
            p.email for p in detected.attendee_profiles
            if p.is_external and not p.crm_contact_external_id
        ]
        brief = MeetingBrief(
            title=detected.title or "Upcoming external meeting",
            account_context=brief_data.get("account_context") or _fallback_account_context(context),
            attendee_profiles=brief_data.get("attendee_profiles") or _fallback_profiles(detected.attendee_profiles),
            last_touch_summary=brief_data.get("last_touch_summary"),
            open_commitments=brief_data.get("open_commitments") or [],
            talking_points=brief_data.get("talking_points") or [],
            recommended_cta=brief_data.get("recommended_cta"),
            meddic_gaps=brief_data.get("meddic_gaps") if context.meddic_enabled else [],
            flagged_unknown_attendees=unknown,
            opportunity_external_id=detected.opportunity_external_id,
            account_external_id=detected.account_external_id,
        )
        await self._post_brief(detected=detected, brief=brief)
        return brief

    # ------------------------------------------------------------------
    # CRM context helpers
    # ------------------------------------------------------------------
    async def _fetch_opportunity(
        self, detected: MeetingDetected
    ) -> CRMRecord | None:
        if not detected.opportunity_external_id or detected.crm_platform is None:
            return None
        return await self._crm_reader.fetch_record(
            tenant_id=detected.tenant_id,
            platform=detected.crm_platform,
            kind=CRMRecordKind.OPPORTUNITY,
            external_id=detected.opportunity_external_id,
        )

    async def _fetch_account(
        self, detected: MeetingDetected
    ) -> CRMRecord | None:
        if not detected.account_external_id:
            return None
        platform = detected.crm_platform or _platform_from_profiles(detected.attendee_profiles)
        if platform is None:
            return None
        return await self._crm_reader.fetch_record(
            tenant_id=detected.tenant_id,
            platform=platform,
            kind=CRMRecordKind.ACCOUNT,
            external_id=detected.account_external_id,
        )

    # ------------------------------------------------------------------
    # Delivery
    # ------------------------------------------------------------------
    async def _post_brief(
        self,
        *,
        detected: MeetingDetected,
        brief: MeetingBrief,
    ) -> None:
        body = _render_body(brief, detected)
        feedback_payload = {
            "tenant_id": str(detected.tenant_id),
            "rep_id": str(detected.rep_id),
            "task_id": detected.calendar_event_id,  # repurposed correlation key
        }
        actions = [
            {
                "action_id": "alex.feedback",
                "label": "👍 Useful",
                "style": "primary",
                "value": {**feedback_payload, "rating": "1"},
            },
            {
                "action_id": "alex.feedback",
                "label": "👎 Not useful",
                "value": {**feedback_payload, "rating": "-1"},
            },
        ]
        await self._output_router.deliver(
            DeliveryRequest(
                tenant_id=detected.tenant_id,
                rep_id=detected.rep_id,
                output_id=f"brief:{detected.calendar_event_id}",
                output_type=OutputType.MEETING_PREP,
                title=f"Meeting prep — {brief.title}",
                body=body,
                metadata={
                    "actions": actions,
                    "brief": brief.model_dump(mode="json"),
                    "calendar_event_id": detected.calendar_event_id,
                },
            )
        )


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------
def _parse_brief_json(raw: str) -> dict[str, Any]:
    """Best-effort JSON parse — the stub backend returns a placeholder
    that isn't JSON; in that case we return an empty dict so the
    fallback fields kick in."""
    text = raw.strip()
    if text.startswith("```"):
        # Strip a possible ```json fence.
        text = text.split("```", 2)[-1] if "```" in text[3:] else text[3:]
        text = text.rstrip("`").strip()
        if text.startswith("json\n"):
            text = text[len("json\n"):]
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {}


def _fallback_account_context(context: _BriefContext) -> str:
    if context.account is None:
        return (
            "No matching CRM account found for the attendees' domain. "
            "Treat this meeting as exploratory; capture account details during the call."
        )
    return (
        f"{context.account.name} ({context.account.industry or 'industry unknown'}, "
        f"{context.account.country or 'region unknown'}). "
        f"Domain: {context.account.domain or 'n/a'}."
    )


def _fallback_profiles(profiles: list[AttendeeProfile]) -> list[dict[str, Any]]:
    return [
        {
            "email": p.email,
            "role": "external attendee" if p.is_external else "internal",
            "context": "no profile context available yet",
        }
        for p in profiles
        if p.is_external
    ]


def _platform_from_profiles(profiles: list[AttendeeProfile]) -> CRMPlatform | None:
    """The MeetingClassifier picks a CRMPlatform when it can. When it
    couldn't, we still want to attempt an account lookup — pick the
    first known platform attached to an attendee."""
    for p in profiles:
        ext = p.crm_contact_external_id or p.crm_account_external_id
        if ext and ext.startswith("contact"):
            return None  # not enough signal to pick a platform
    return None


def _render_body(brief: MeetingBrief, detected: MeetingDetected) -> str:
    parts: list[str] = []
    parts.append(f"*When:* {detected.start_at.isoformat()}")
    parts.append(f"*Account context:* {brief.account_context}")
    if brief.last_touch_summary:
        parts.append(f"*Last touch:* {brief.last_touch_summary}")
    if brief.open_commitments:
        parts.append("*Open commitments:*\n" + "\n".join(f"• {c}" for c in brief.open_commitments))
    if brief.talking_points:
        parts.append("*Talking points:*\n" + "\n".join(f"• {tp}" for tp in brief.talking_points))
    if brief.recommended_cta:
        parts.append(f"*Recommended CTA:* {brief.recommended_cta}")
    if brief.meddic_gaps:
        parts.append("*MEDDIC gaps to address:*\n" + "\n".join(f"• {g}" for g in brief.meddic_gaps))
    if brief.flagged_unknown_attendees:
        parts.append(
            "*Unknown attendees:* "
            + ", ".join(brief.flagged_unknown_attendees)
            + " (no CRM record yet)"
        )
    return "\n\n".join(parts)


