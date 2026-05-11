"""Unit tests for the deterministic VoiceSignalExtractor."""
from __future__ import annotations

import pytest

from alex_agent_runtime.schemas import GermanRegister, VoiceLanguage
from alex_agent_runtime.services.voice_signal_extractor import VoiceSignalExtractor


def test_detects_english_greeting_swap():
    extractor = VoiceSignalExtractor()
    before = "Hello Sam,\n\nThanks for your time today.\n\nBest regards,\nAlex"
    after = "Hi Sam,\n\nThanks for your time today.\n\nBest regards,\nAlex"
    signal = extractor.extract(before=before, after=after)
    assert signal.language is VoiceLanguage.EN
    assert signal.added_greetings == ["Hi Sam"]
    assert signal.removed_greetings == ["Hello Sam"]
    assert signal.added_signoffs == []
    assert signal.removed_signoffs == []


def test_detects_english_signoff_swap():
    extractor = VoiceSignalExtractor()
    before = "Hi Sam,\n\nQuick check-in.\n\nBest regards"
    after = "Hi Sam,\n\nQuick check-in.\n\nCheers"
    signal = extractor.extract(before=before, after=after)
    assert signal.added_signoffs == ["Cheers"]
    assert signal.removed_signoffs == ["Best regards"]


def test_detects_german_via_umlaut_and_signoff():
    extractor = VoiceSignalExtractor()
    before = "Hallo Frau Müller,\n\nVielen Dank für unser Gespräch.\n\nGrüße"
    after = "Sehr geehrte Frau Müller,\n\nVielen Dank für unser Gespräch.\n\nMit freundlichen Grüßen"
    signal = extractor.extract(before=before, after=after)
    assert signal.language is VoiceLanguage.DE
    assert signal.added_greetings == ["Sehr geehrte Frau Müller"]
    assert signal.removed_greetings == ["Hallo Frau Müller"]
    assert signal.added_signoffs == ["Mit freundlichen Grüßen"]


def test_detects_sie_register():
    extractor = VoiceSignalExtractor()
    after = (
        "Sehr geehrte Frau Müller,\n\n"
        "vielen Dank für unser Gespräch. Ich melde mich bei Ihnen mit den vereinbarten Unterlagen.\n\n"
        "Mit freundlichen Grüßen"
    )
    signal = extractor.extract(before="", after=after)
    assert signal.language is VoiceLanguage.DE
    assert signal.de_register_signal is GermanRegister.SIE


def test_detects_du_register():
    extractor = VoiceSignalExtractor()
    after = (
        "Hallo Sam,\n\n"
        "danke für unseren Austausch heute. Ich schicke dir gleich die Unterlagen.\n\n"
        "Grüße"
    )
    signal = extractor.extract(before="", after=after)
    assert signal.language is VoiceLanguage.DE
    assert signal.de_register_signal is GermanRegister.DU


def test_length_delta_ratio_negative_when_rep_trims():
    extractor = VoiceSignalExtractor()
    before = "x" * 200
    after = "x" * 100
    signal = extractor.extract(before=before, after=after)
    assert signal.length_delta_ratio == pytest.approx(-0.5, abs=0.01)


def test_phrase_diff_excludes_greetings_and_signoffs():
    """Mid-body line changes show up in added/removed_phrases."""
    extractor = VoiceSignalExtractor()
    before = (
        "Hi Sam,\n\nLet's catch up next week.\n\nBest regards"
    )
    after = (
        "Hi Sam,\n\nLet's chat early next week.\n\nBest regards"
    )
    signal = extractor.extract(before=before, after=after)
    assert "Let's chat early next week." in signal.added_phrases
    assert "Let's catch up next week." in signal.removed_phrases
    # Greetings/sign-offs aren't duplicated in the phrase delta.
    assert "Hi Sam," not in signal.added_phrases
    assert "Hi Sam," not in signal.removed_phrases


def test_no_signal_when_bodies_identical():
    extractor = VoiceSignalExtractor()
    same = "Hi Sam,\n\nThanks!\n\nBest regards"
    signal = extractor.extract(before=same, after=same)
    assert signal.added_greetings == []
    assert signal.removed_greetings == []
    assert signal.added_signoffs == []
    assert signal.removed_signoffs == []
    assert signal.added_phrases == []
    assert signal.removed_phrases == []
    assert signal.length_delta_ratio == 0.0
