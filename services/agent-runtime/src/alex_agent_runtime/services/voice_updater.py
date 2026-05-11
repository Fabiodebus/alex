"""VoiceUpdater — progressive weighted updates of the per-rep VoiceProfile.

Subscribes to two EventBus topics produced by the Approval Workflow:

* ``approval.approved`` — only consumed when the payload carries an
  :class:`EditDiff`. A clean approve (no diff) confirms the profile
  without modification, per the blueprint.
* ``approval.discarded`` — wrapped into a :class:`DiscardSignal` and
  used as a negative signal: the discarded body's greeting / sign-off
  / phrases lose weight, but the profile is never reset.

Update model is the EWMA chosen by scoping: ``alpha = max(min_alpha,
1 / (1 + sample_count * decay))``. The first edit contributes ~10%
weight; ten edits compound. Recurrent patterns climb in weight while
one-offs decay below the rendering threshold. The constants live in
:class:`Settings` so we can tune post-launch.
"""
from __future__ import annotations

from typing import Iterable
from uuid import UUID

import structlog

from ..config import Settings, get_settings
from ..schemas import (
    DiscardSignal,
    EditDiff,
    TaskApproved,
    TaskDiscarded,
    VoiceLanguage,
    VoiceLanguageProfile,
    VoicePatternStat,
    VoiceProfile,
    VoiceSignal,
)
from ..tenant_context import tenant_scope
from .event_bus import EventBus
from .voice_profile_store import VoiceProfileStore
from .voice_signal_extractor import VoiceSignalExtractor

log = structlog.get_logger(__name__)


TOPIC_VOICE_PROFILE_UPDATED = "voice.profile_updated"

# Patterns with weight below this disappear from the rendered prompt
# fragment (they're still in the row for history). Tunable later.
_PROMPT_RENDER_THRESHOLD = 0.05
# Cap how many patterns we keep per category so the row + prompt stay
# bounded as a long-tenured rep accumulates many edits.
_MAX_PATTERNS_PER_CATEGORY = 12


class VoiceUpdater:
    def __init__(
        self,
        *,
        store: VoiceProfileStore,
        extractor: VoiceSignalExtractor,
        settings: Settings | None = None,
    ) -> None:
        self._store = store
        self._extractor = extractor
        self._settings = settings or get_settings()
        self._event_bus: EventBus | None = None  # set via attach_updater()

    # ------------------------------------------------------------------
    # Public API — feature WOs may call these directly bypassing the bus.
    # ------------------------------------------------------------------
    async def apply_edit_diff(
        self,
        *,
        tenant_id: UUID,
        rep_id: UUID,
        diff: EditDiff,
    ) -> VoiceProfile:
        before_body = _coerce_body(diff.before)
        after_body = _coerce_body(diff.after)
        signal = self._extractor.extract(before=before_body, after=after_body)
        return await self._apply(
            tenant_id=tenant_id, rep_id=rep_id, signal=signal, is_discard=False
        )

    async def apply_discard(
        self,
        *,
        tenant_id: UUID,
        rep_id: UUID,
        signal: DiscardSignal,
    ) -> VoiceProfile:
        # Discard semantics: every greeting / sign-off / phrase in the
        # discarded body is unwanted. We extract with after="" so the
        # body's content lands in the ``removed_*`` channels of the
        # signal; the updater's discard branch then drives the
        # appropriate forbidden_phrases boost.
        extracted = self._extractor.extract(before=signal.discarded_body, after="")
        return await self._apply(
            tenant_id=tenant_id, rep_id=rep_id, signal=extracted, is_discard=True
        )

    # ------------------------------------------------------------------
    # EventBus adapters
    # ------------------------------------------------------------------
    async def _on_approval_approved(self, event: TaskApproved) -> None:
        if event.edit_diff is None:
            # Clean approve confirms the profile without modifying it.
            return
        with tenant_scope(event.tenant_id):
            await self.apply_edit_diff(
                tenant_id=event.tenant_id, rep_id=event.rep_id, diff=event.edit_diff
            )

    async def _on_approval_discarded(self, event: TaskDiscarded) -> None:
        body = ""
        # TaskDiscarded carries the rep's feedback string but not the
        # discarded draft body. We try to pull the body from the
        # original payload if a feature workflow plumbed it through;
        # otherwise the feedback string itself is the only signal we
        # have. Both cases are common enough that we let it through.
        body = event.feedback or ""
        if not body:
            return
        signal = DiscardSignal(
            tenant_id=event.tenant_id,
            rep_id=event.rep_id,
            task_id=event.task_id,
            discarded_body=body,
            feedback=event.feedback,
        )
        with tenant_scope(event.tenant_id):
            await self.apply_discard(
                tenant_id=event.tenant_id, rep_id=event.rep_id, signal=signal
            )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _apply(
        self,
        *,
        tenant_id: UUID,
        rep_id: UUID,
        signal: VoiceSignal,
        is_discard: bool,
    ) -> VoiceProfile:
        current = await self._store.get_current(tenant_id=tenant_id, rep_id=rep_id)
        lang_profile = current.languages.get(signal.language) or VoiceLanguageProfile()
        alpha = self._alpha_for(lang_profile.sample_count)

        # Greetings / sign-offs: ``added`` raises weight, ``removed``
        # lowers it. Discard uses the same rule because the extractor
        # populates ``removed_*`` channels with the discarded body's
        # content (after="").
        new_greetings = _blend_patterns(
            existing=lang_profile.greetings,
            added=signal.added_greetings,
            removed=signal.removed_greetings,
            alpha=alpha,
        )
        new_signoffs = _blend_patterns(
            existing=lang_profile.signoffs,
            added=signal.added_signoffs,
            removed=signal.removed_signoffs,
            alpha=alpha,
        )

        if is_discard:
            # The mid-body lines of a discarded draft are unwanted;
            # they go into forbidden_phrases. signature_phrases stays
            # untouched — a discard doesn't tell us what TO say.
            new_phrases = list(lang_profile.signature_phrases)
            new_forbidden = _blend_patterns(
                existing=lang_profile.forbidden_phrases,
                added=signal.removed_phrases,
                removed=[],
                alpha=alpha,
            )
            # Brevity / register signals from a discard aren't reliable.
            new_brevity = lang_profile.brevity
            new_register = lang_profile.de_register
        else:
            # Edit: rep's mid-body additions become signature_phrases;
            # mid-body removals become forbidden_phrases.
            new_phrases = _blend_patterns(
                existing=lang_profile.signature_phrases,
                added=signal.added_phrases,
                removed=[],
                alpha=alpha,
            )
            new_forbidden = _blend_patterns(
                existing=lang_profile.forbidden_phrases,
                added=signal.removed_phrases,
                removed=[],
                alpha=alpha,
            )
            new_brevity = _clip01(
                (1 - alpha) * lang_profile.brevity
                + alpha * (0.5 - 0.5 * signal.length_delta_ratio)
            )
            new_register = lang_profile.de_register
            if (
                signal.language is VoiceLanguage.DE
                and signal.de_register_signal is not None
            ):
                new_register = signal.de_register_signal

        # Sample count climbs on positive signal only — discards do
        # not graduate the rep's profile to a "more confident" state.
        sample_delta = 0 if is_discard else 1
        new_lang = lang_profile.model_copy(
            update={
                "greetings": new_greetings,
                "signoffs": new_signoffs,
                "signature_phrases": new_phrases,
                "forbidden_phrases": new_forbidden,
                "brevity": new_brevity,
                "de_register": new_register,
                "sample_count": lang_profile.sample_count + sample_delta,
            }
        )

        new_languages = dict(current.languages)
        new_languages[signal.language] = new_lang

        new_profile = current.model_copy(
            update={
                "version": current.version + 1,
                "languages": new_languages,
            }
        )
        stored = await self._store.put(
            tenant_id=tenant_id, rep_id=rep_id, profile=new_profile
        )
        log.info(
            "voice_updater.applied",
            rep_id=str(rep_id),
            language=signal.language.value,
            is_discard=is_discard,
            new_version=stored.version,
            alpha=alpha,
        )
        if self._event_bus is not None:
            await self._event_bus.publish(
                TOPIC_VOICE_PROFILE_UPDATED,
                {
                    "tenant_id": str(tenant_id),
                    "rep_id": str(rep_id),
                    "version": stored.version,
                    "language": signal.language.value,
                    "is_discard": is_discard,
                },
            )
        return stored

    def _alpha_for(self, sample_count: int) -> float:
        min_alpha = self._settings.voice_update_min_alpha
        decay = self._settings.voice_update_decay
        return max(min_alpha, 1.0 / (1.0 + sample_count * decay))


def attach_updater(*, bus: EventBus, updater: VoiceUpdater) -> None:
    from .approval_handler import TOPIC_APPROVAL_APPROVED, TOPIC_APPROVAL_DISCARDED

    updater._event_bus = bus
    bus.subscribe(TOPIC_APPROVAL_APPROVED, updater._on_approval_approved)
    bus.subscribe(TOPIC_APPROVAL_DISCARDED, updater._on_approval_discarded)


# ---------------------------------------------------------------------------
# Pattern blending
# ---------------------------------------------------------------------------
def _blend_patterns(
    *,
    existing: Iterable[VoicePatternStat],
    added: Iterable[str],
    removed: Iterable[str],
    alpha: float,
) -> list[VoicePatternStat]:
    """Apply the EWMA update.

    For each existing pattern p: ``w' = (1 - alpha) * w``.
    For each added pattern p:    ``w' += alpha``.
    For each removed pattern p:  ``w' -= alpha``.
    Negative weights are clipped to 0; weights are clipped to 1.
    Patterns ending up at 0 are kept until pruning hits the cap so a
    decayed pattern can climb back if it recurs.
    """
    table: dict[str, float] = {p.phrase: p.weight for p in existing}
    decayed: dict[str, float] = {
        phrase: (1 - alpha) * weight for phrase, weight in table.items()
    }
    for phrase in added:
        clean = phrase.strip()
        if not clean:
            continue
        decayed[clean] = _clip01((decayed.get(clean) or 0.0) + alpha)
    for phrase in removed:
        clean = phrase.strip()
        if not clean:
            continue
        decayed[clean] = _clip01((decayed.get(clean) or 0.0) - alpha)

    # Keep the top-N by weight so the row + prompt stay bounded.
    sorted_items = sorted(decayed.items(), key=lambda kv: kv[1], reverse=True)[
        :_MAX_PATTERNS_PER_CATEGORY
    ]
    return [VoicePatternStat(phrase=phrase, weight=weight) for phrase, weight in sorted_items]


def _clip01(value: float) -> float:
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return round(value, 4)


def _coerce_body(payload: dict) -> str:
    """Pull the rep-edited body text out of an :class:`EditDiff` half.

    The Approval Workflow plumbs the full ``payload``/``edited_output``
    dict through; feature WOs that store the draft as ``{"body": "..."}``
    or a string-only payload both work."""
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return ""
    body = payload.get("body")
    if isinstance(body, str):
        return body
    # Fallback: stringify any "draft" / "text" / "content" key.
    for key in ("draft", "text", "content"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return ""


# Public re-export for the prompt-fragment threshold so the applicator
# can use the same constant.
PROMPT_RENDER_THRESHOLD = _PROMPT_RENDER_THRESHOLD
