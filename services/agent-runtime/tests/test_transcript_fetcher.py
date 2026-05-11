"""Unit tests for TranscriptFetcher stub."""
from __future__ import annotations

from uuid import uuid4

import pytest

from alex_agent_runtime.schemas import TranscriptRequest, VoiceLanguage
from alex_agent_runtime.services.transcript_fetcher import StubTranscriptFetcher


@pytest.mark.asyncio
async def test_stub_returns_synthetic_transcript():
    fetcher = StubTranscriptFetcher()
    result = await fetcher.fetch(
        TranscriptRequest(
            tenant_id=uuid4(), rep_id=uuid4(), calendar_event_id="evt-real"
        )
    )
    assert result is not None
    assert result.calendar_event_id == "evt-real"
    assert "Synthetic transcript" in result.transcript
    assert result.language is VoiceLanguage.EN


@pytest.mark.asyncio
async def test_stub_returns_none_for_empty_event_id():
    fetcher = StubTranscriptFetcher()
    result = await fetcher.fetch(
        TranscriptRequest(
            tenant_id=uuid4(), rep_id=uuid4(), calendar_event_id=""
        )
    )
    assert result is None
