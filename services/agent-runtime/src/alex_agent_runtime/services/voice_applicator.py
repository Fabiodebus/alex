"""VoiceApplicator — render the per-rep style into a prompt fragment.

The draft-generation feature workflows (Post-Meeting Follow-Up
Drafting, Personalized Outbound & Nurture — both later WOs) call
:meth:`VoiceApplicator.apply` with the active ``rep_id`` and an
``AccountContext``. The applicator:

1. Pulls the current :class:`VoiceProfile` from
   :class:`VoiceProfileStore`.
2. Picks the language sub-profile from the account context (defaults
   to ``en`` when no hint is present).
3. Resolves the German Sie/Du register from the account context's
   ``register_hint`` (rep's prior choice on the account) or the
   profile's accumulated default; ``MIXED`` is the safe fall-through.
4. Renders a stable Markdown bullet list of voice characteristics
   (greetings, sign-offs, signatures, things to avoid, tone scores,
   register) — only patterns weighted above the visibility threshold
   are surfaced so a thin profile doesn't pollute the prompt.
5. Returns the prompt fragment plus a :class:`VoiceApplication`
   metadata blob so the caller can tag the generated draft with the
   profile version that produced it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable
from uuid import UUID

import structlog
from pydantic import BaseModel, Field

from ..schemas import (
    GermanRegister,
    VoiceApplication,
    VoiceLanguage,
    VoiceLanguageProfile,
    VoicePatternStat,
    VoiceProfile,
)
from .voice_profile_store import VoiceProfileStore
from .voice_updater import PROMPT_RENDER_THRESHOLD

log = structlog.get_logger(__name__)


class AccountContext(BaseModel):
    """Per-account hints the applicator uses to resolve language +
    register at apply time."""

    account_external_id: str | None = None
    language: VoiceLanguage = VoiceLanguage.EN
    register_hint: GermanRegister | None = Field(
        default=None,
        description="Override the profile's de_register when the rep has "
        "already settled on Sie/Du for this account.",
    )
    country_hint: str | None = None  # 'DE' / 'AT' / 'CH' for regional voice cues


class VoiceApplicationResult(BaseModel):
    prompt_fragment: str
    application: VoiceApplication


class VoiceApplicator:
    def __init__(self, *, store: VoiceProfileStore) -> None:
        self._store = store

    async def apply(
        self,
        *,
        tenant_id: UUID,
        rep_id: UUID,
        context: AccountContext | None = None,
    ) -> VoiceApplicationResult:
        ctx = context or AccountContext()
        profile = await self._store.get_current(tenant_id=tenant_id, rep_id=rep_id)
        sub_profile = profile.languages.get(ctx.language) or VoiceLanguageProfile()
        register = _resolve_register(profile=sub_profile, ctx=ctx)
        fragment = _render_fragment(
            profile=sub_profile, language=ctx.language, register=register, ctx=ctx
        )
        application = VoiceApplication(
            rep_id=rep_id,
            profile_version=profile.version,
            language=ctx.language,
            de_register=register if ctx.language is VoiceLanguage.DE else None,
            account_external_id=ctx.account_external_id,
            applied_at=datetime.now(timezone.utc),
        )
        log.info(
            "voice_applicator.applied",
            rep_id=str(rep_id),
            profile_version=profile.version,
            language=ctx.language.value,
            register=register.value if register else None,
        )
        return VoiceApplicationResult(prompt_fragment=fragment, application=application)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _resolve_register(
    *, profile: VoiceLanguageProfile, ctx: AccountContext
) -> GermanRegister:
    # Caller-supplied account hint always wins — it represents the
    # rep's prior choice on this specific account.
    if ctx.register_hint is not None:
        return ctx.register_hint
    return profile.de_register


def _render_fragment(
    *,
    profile: VoiceLanguageProfile,
    language: VoiceLanguage,
    register: GermanRegister,
    ctx: AccountContext,
) -> str:
    lines: list[str] = []
    lines.append(_voice_heading(profile=profile, language=language))
    greetings = _top_patterns(profile.greetings)
    if greetings:
        lines.append(f"- Preferred greetings: {greetings}")
    signoffs = _top_patterns(profile.signoffs)
    if signoffs:
        lines.append(f"- Preferred sign-offs: {signoffs}")
    sigs = _top_patterns(profile.signature_phrases)
    if sigs:
        lines.append(f"- Signature phrases: {sigs}")
    forbiddens = _top_patterns(profile.forbidden_phrases)
    if forbiddens:
        lines.append(f"- Avoid: {forbiddens}")
    lines.append(
        f"- Tone: formality {_score(profile.formality)}, warmth {_score(profile.warmth)}, "
        f"directness {_score(profile.directness)}, brevity {_score(profile.brevity)}"
    )
    if language is VoiceLanguage.DE:
        lines.append(f"- German register: address the reader using {register.value}")
        if ctx.country_hint:
            lines.append(f"- Regional context: {ctx.country_hint}")
    return "\n".join(lines)


def _voice_heading(*, profile: VoiceLanguageProfile, language: VoiceLanguage) -> str:
    if profile.sample_count == 0:
        return (
            f"You are drafting in {language.value}. The rep's voice profile is empty — "
            "fall back to neutral, professional DACH B2B style until signal accumulates."
        )
    return (
        f"You are drafting in {language.value}. The rep's voice profile "
        f"(based on {profile.sample_count} observed drafts) prefers:"
    )


def _top_patterns(patterns: Iterable[VoicePatternStat]) -> str:
    visible = [p for p in patterns if p.weight >= PROMPT_RENDER_THRESHOLD]
    visible.sort(key=lambda p: p.weight, reverse=True)
    if not visible:
        return ""
    return ", ".join(f'"{p.phrase}" ({p.weight:.2f})' for p in visible[:5])


def _score(value: float) -> str:
    if value >= 0.8:
        return "high"
    if value >= 0.6:
        return "med-high"
    if value >= 0.4:
        return "balanced"
    if value >= 0.2:
        return "med-low"
    return "low"


# Re-export to keep the upstream type names handy without an extra
# import for callers of this module.
__all__ = [
    "AccountContext",
    "VoiceApplicator",
    "VoiceApplicationResult",
    "VoiceProfile",
]
