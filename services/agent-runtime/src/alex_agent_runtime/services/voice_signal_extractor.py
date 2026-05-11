"""Deterministic VoiceSignal extractor.

Compares an Alex-generated draft ("before") with the rep-edited
version ("after") and produces a :class:`VoiceSignal` carrying the
greeting / sign-off / phrase deltas. No LLM call, no external state:
the extractor is a pure function so the EWMA update is reproducible
and the tests can assert on exact signal output.

The regex patterns cover the greetings and sign-offs commonly used in
English and German DACH business correspondence. A future WO can
swap this for an LLM-driven extractor without touching the updater
or the store — both consume :class:`VoiceSignal`.
"""
from __future__ import annotations

import re
from typing import Iterable

import structlog

from ..schemas import GermanRegister, VoiceLanguage, VoiceSignal

log = structlog.get_logger(__name__)


# Greeting / sign-off patterns, normalised to lowercase. The leading
# anchor on each pattern is intentional — we want to match the first
# line of the body and not an in-text mention.
_GREETING_RE_EN = re.compile(
    r"^(hi|hello|hey|dear|good (morning|afternoon|evening))[ ,]",
    re.IGNORECASE,
)
_GREETING_RE_DE = re.compile(
    r"^(hallo|hi|guten (morgen|tag|abend)|sehr geehrte[rs]?|lieber|liebe|moin)[ ,]",
    re.IGNORECASE,
)
_SIGNOFF_RE_EN = re.compile(
    r"\b(best regards|kind regards|cheers|thanks|thank you|best|sincerely|warmly|talk soon)\b",
    re.IGNORECASE,
)
_SIGNOFF_RE_DE = re.compile(
    r"\b(beste gr[üu]ße|viele gr[üu]ße|mit freundlichen gr[üu]ßen|herzliche gr[üu]ße|gr[üu]ße)\b",
    re.IGNORECASE,
)

# Cheap language signal: presence of these characters or stopwords
# tips the language to German. We deliberately don't bring in a heavy
# language-detection dependency for v1.
_DE_CHAR_HINT = re.compile(r"[äöüß]", re.IGNORECASE)
_DE_STOPWORDS = re.compile(
    r"\b(und|der|die|das|nicht|sind|haben|für|mit|nach|ihr|sie|wir|sehr|bitte)\b",
    re.IGNORECASE,
)

_SIE_HINT = re.compile(r"\b(Sie|Ihnen|Ihre[nm]?)\b")
_DU_HINT = re.compile(r"\b(du|dir|dich|dein[em]?|deine[nrs]?)\b", re.IGNORECASE)


class VoiceSignalExtractor:
    """Pure-function extractor. Holds no state."""

    def extract(self, *, before: str, after: str) -> VoiceSignal:
        language = self._detect_language(after) or self._detect_language(before) or VoiceLanguage.EN

        added_greetings, removed_greetings = self._diff_first_line_greetings(
            before=before, after=after, language=language
        )
        added_signoffs, removed_signoffs = self._diff_last_line_signoffs(
            before=before, after=after, language=language
        )
        added_lines, removed_lines = self._diff_line_set(before=before, after=after)

        # Trim the line-level diff so the signal doesn't carry greeting
        # / sign-off lines twice. The phrase-level signal is meant for
        # mid-body style cues, not the salutation/signature.
        added_phrases = self._strip_greetings_and_signoffs(added_lines, language=language)
        removed_phrases = self._strip_greetings_and_signoffs(removed_lines, language=language)

        length_delta_ratio = _length_delta_ratio(before=before, after=after)
        de_register = (
            self._detect_register(after) if language is VoiceLanguage.DE else None
        )

        return VoiceSignal(
            language=language,
            added_greetings=added_greetings,
            removed_greetings=removed_greetings,
            added_signoffs=added_signoffs,
            removed_signoffs=removed_signoffs,
            added_phrases=added_phrases,
            removed_phrases=removed_phrases,
            length_delta_ratio=length_delta_ratio,
            de_register_signal=de_register,
        )

    # ------------------------------------------------------------------
    # Detection helpers
    # ------------------------------------------------------------------
    def _detect_language(self, text: str) -> VoiceLanguage | None:
        if not text:
            return None
        if _DE_CHAR_HINT.search(text):
            return VoiceLanguage.DE
        # Count stopword hits; >= 2 distinct German stopwords -> German.
        if len({m.group(0).lower() for m in _DE_STOPWORDS.finditer(text)}) >= 2:
            return VoiceLanguage.DE
        return VoiceLanguage.EN

    def _detect_register(self, text: str) -> GermanRegister | None:
        sie = bool(_SIE_HINT.search(text))
        du = bool(_DU_HINT.search(text))
        if sie and not du:
            return GermanRegister.SIE
        if du and not sie:
            return GermanRegister.DU
        if sie and du:
            return GermanRegister.MIXED
        return None

    # ------------------------------------------------------------------
    # Diff helpers
    # ------------------------------------------------------------------
    def _diff_first_line_greetings(
        self, *, before: str, after: str, language: VoiceLanguage
    ) -> tuple[list[str], list[str]]:
        regex = _GREETING_RE_DE if language is VoiceLanguage.DE else _GREETING_RE_EN
        bg = _match_first_line(before, regex)
        ag = _match_first_line(after, regex)
        if bg == ag:
            return [], []
        added = [ag] if ag else []
        removed = [bg] if bg else []
        return added, removed

    def _diff_last_line_signoffs(
        self, *, before: str, after: str, language: VoiceLanguage
    ) -> tuple[list[str], list[str]]:
        regex = _SIGNOFF_RE_DE if language is VoiceLanguage.DE else _SIGNOFF_RE_EN
        bs = _match_last_signoff(before, regex)
        as_ = _match_last_signoff(after, regex)
        if bs == as_:
            return [], []
        added = [as_] if as_ else []
        removed = [bs] if bs else []
        return added, removed

    def _diff_line_set(self, *, before: str, after: str) -> tuple[list[str], list[str]]:
        before_lines = {ln.strip() for ln in before.splitlines() if ln.strip()}
        after_lines = {ln.strip() for ln in after.splitlines() if ln.strip()}
        added = sorted(after_lines - before_lines)
        removed = sorted(before_lines - after_lines)
        return added, removed

    def _strip_greetings_and_signoffs(
        self, lines: Iterable[str], *, language: VoiceLanguage
    ) -> list[str]:
        greeting_re = _GREETING_RE_DE if language is VoiceLanguage.DE else _GREETING_RE_EN
        signoff_re = _SIGNOFF_RE_DE if language is VoiceLanguage.DE else _SIGNOFF_RE_EN
        out: list[str] = []
        for line in lines:
            if greeting_re.search(line) or signoff_re.search(line):
                continue
            out.append(line)
        return out


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------
def _match_first_line(text: str, regex: re.Pattern[str]) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if regex.search(stripped):
            return stripped.rstrip(",")
        return None
    return None


def _match_last_signoff(text: str, regex: re.Pattern[str]) -> str | None:
    last: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        m = regex.search(stripped)
        if m:
            last = m.group(0).strip()
    return last


def _length_delta_ratio(*, before: str, after: str) -> float:
    before_len = max(len(before.strip()), 1)
    after_len = len(after.strip())
    ratio = (after_len - before_len) / before_len
    if ratio > 1.0:
        return 1.0
    if ratio < -1.0:
        return -1.0
    return round(ratio, 4)
