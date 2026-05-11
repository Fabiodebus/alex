"""Fetch a meeting transcript on demand.

Used by FollowUpDraftComposer (WO #18) and CRMNoteComposer (WO #19).
Mirrors the CRMFetchClient pattern: a Protocol with Stub + Pipedream
implementations. ``stub`` returns synthetic transcript JSON so the
end-to-end flow runs in dev; ``pipedream`` POSTs (signed) to the
configured ``transcript_fetch`` workflow URL.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

import httpx
import structlog

from ..config import Settings, get_settings
from ..schemas import TranscriptRequest, TranscriptResult
from .pipedream_client import _sign

log = structlog.get_logger(__name__)


class TranscriptFetchError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0, body: object | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


@runtime_checkable
class TranscriptFetcher(Protocol):
    name: str

    async def fetch(self, request: TranscriptRequest) -> TranscriptResult | None: ...


class StubTranscriptFetcher:
    """Returns a deterministic synthetic transcript for any meeting.

    Production replaces this with a Pipedream workflow that hits the
    rep's recording tool (Krisp.ai / Granola / Fathom)."""

    name = "stub"

    async def fetch(self, request: TranscriptRequest) -> TranscriptResult | None:
        # An empty-string event id is the "we want a deliberate miss"
        # sentinel that tests rely on.
        if not request.calendar_event_id:
            return None
        synthetic = (
            f"[{request.calendar_event_id}] Synthetic transcript.\n\n"
            "Rep: Thanks for making time today.\n"
            "Buyer: Happy to. We're evaluating three vendors and shortlisting next week.\n"
            "Rep: Got it. What would tip the decision in our favour?\n"
            "Buyer: Reliable EU data residency, clean MS Teams integration, "
            "and clear pricing for 250 seats.\n"
            "Rep: Understood. I'll send a one-pager + reference customer by Friday.\n"
            "Buyer: Perfect. We'll loop in our security lead for the next call.\n"
        )
        return TranscriptResult(
            calendar_event_id=request.calendar_event_id,
            provider="stub",
            transcript=synthetic,
            language="en",
            speakers=["Rep", "Buyer"],
            fetched_at=datetime.now(timezone.utc),
        )


class PipedreamTranscriptFetcher:
    name = "pipedream"

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._http = client or httpx.AsyncClient(timeout=30.0)
        self._owned_http = client is None

    async def close(self) -> None:
        if self._owned_http:
            await self._http.aclose()

    async def fetch(self, request: TranscriptRequest) -> TranscriptResult | None:
        if not self._settings.alex_pipedream_transcript_fetch_url:
            raise TranscriptFetchError(
                "ALEX_PIPEDREAM_TRANSCRIPT_FETCH_URL is unset"
            )
        body = json.dumps(request.model_dump(mode="json"), separators=(",", ":")).encode("utf-8")
        ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        headers = {
            "Content-Type": "application/json",
            "X-Tenant-Id": str(request.tenant_id),
            "X-Alex-Timestamp": ts,
        }
        if self._settings.alex_webhook_secret:
            headers["X-Alex-Signature"] = _sign(
                self._settings.alex_webhook_secret, ts, body.decode("utf-8")
            )
        response = await self._http.post(
            self._settings.alex_pipedream_transcript_fetch_url,
            content=body,
            headers=headers,
        )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            try:
                parsed = response.json()
            except ValueError:
                parsed = None
            raise TranscriptFetchError(
                f"transcript_fetch returned {response.status_code}",
                status=response.status_code,
                body=parsed,
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise TranscriptFetchError(
                f"transcript_fetch returned non-JSON: {exc}",
                status=response.status_code,
            ) from exc
        return TranscriptResult.model_validate(data)


def build_default_transcript_fetcher(settings: Settings | None = None) -> TranscriptFetcher:
    s = settings or get_settings()
    if s.transcript_fetch_provider == "pipedream":
        log.info("transcript_fetcher.selected", provider="pipedream")
        return PipedreamTranscriptFetcher(s)
    log.warning("transcript_fetcher.selected", provider="stub")
    return StubTranscriptFetcher()
