"""IngestionProvider — pulls a batch of source records for a rep backfill.

The Pipedream Integration Layer owns the live data sources (HubSpot,
Gmail, Gong, …); this module is the agent-runtime's view of "give me
the last N days of activity for this rep, normalised into our
``IngestionBatch`` shape". Two implementations:

* ``StubIngestionProvider`` — returns deterministic synthetic records.
  The default in dev / tests so the IngestionPipeline can be exercised
  end-to-end without a live Pipedream workspace.
* ``PipedreamIngestionProvider`` — POSTs an HMAC-signed request to the
  ``ingestion_batch`` Pipedream workflow and parses the response.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable
from uuid import UUID

import httpx
import structlog

from ..config import Settings, get_settings
from ..schemas import IngestedRecord, IngestedRecordKind, IngestionBatch
from .pipedream_client import _sign  # reuse the HMAC contract from WO #4

log = structlog.get_logger(__name__)


class IngestionProviderError(RuntimeError):
    pass


@runtime_checkable
class IngestionProvider(Protocol):
    name: str

    async def fetch_batch(
        self, *, tenant_id: UUID, rep_id: UUID, since_days: int
    ) -> IngestionBatch: ...


class StubIngestionProvider:
    name = "stub"

    async def fetch_batch(
        self, *, tenant_id: UUID, rep_id: UUID, since_days: int
    ) -> IngestionBatch:
        now = datetime.now(timezone.utc)
        records: list[IngestedRecord] = [
            IngestedRecord(
                kind=IngestedRecordKind.CRM_OPPORTUNITY,
                external_id=f"deal-{rep_id}-1",
                content="Deal: Acme Corp — Stage: Discovery — Amount: €120,000",
                occurred_at=now - timedelta(days=2),
                attributes={"account_external_id": "acct-acme", "stage": "discovery"},
            ),
            IngestedRecord(
                kind=IngestedRecordKind.CRM_CONTACT,
                external_id=f"contact-{rep_id}-1",
                content="Contact: Bob (VP Sales, Acme Corp) — bob@acme.com",
                occurred_at=now - timedelta(days=2),
                attributes={"account_external_id": "acct-acme", "email": "bob@acme.com"},
            ),
            IngestedRecord(
                kind=IngestedRecordKind.EMAIL_THREAD,
                external_id=f"thread-{rep_id}-1",
                content="Email thread: pricing question, Bob asked about tier 2 vs tier 3.",
                occurred_at=now - timedelta(days=1),
                attributes={"account_external_id": "acct-acme", "from": "bob@acme.com"},
            ),
            IngestedRecord(
                kind=IngestedRecordKind.CALL_RECORDING,
                external_id=f"call-{rep_id}-1",
                content="Call transcript: Discovery call, Bob signaled interest in custom SLAs.",
                occurred_at=now - timedelta(hours=5),
                attributes={"account_external_id": "acct-acme", "duration_seconds": 1800},
            ),
        ]
        return IngestionBatch(
            tenant_id=tenant_id,
            rep_id=rep_id,
            fetched_at=now,
            records=records,
        )


class PipedreamIngestionProvider:
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

    async def fetch_batch(
        self, *, tenant_id: UUID, rep_id: UUID, since_days: int
    ) -> IngestionBatch:
        if not self._settings.alex_pipedream_ingestion_url:
            raise IngestionProviderError(
                "ALEX_PIPEDREAM_INGESTION_URL is unset; cannot run live ingestion"
            )
        payload = {
            "tenant_id": str(tenant_id),
            "rep_id": str(rep_id),
            "since_days": since_days,
            "recording_cap": self._settings.ingestion_recording_cap,
        }
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        headers = {
            "Content-Type": "application/json",
            "X-Tenant-Id": str(tenant_id),
            "X-Alex-Timestamp": timestamp,
        }
        if self._settings.alex_webhook_secret:
            headers["X-Alex-Signature"] = _sign(
                self._settings.alex_webhook_secret, timestamp, body.decode("utf-8")
            )
        response = await self._http.post(
            self._settings.alex_pipedream_ingestion_url,
            content=body,
            headers=headers,
        )
        if response.status_code >= 400:
            raise IngestionProviderError(
                f"Pipedream ingestion_batch returned {response.status_code}: "
                f"{response.text[:200]}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise IngestionProviderError(
                f"Pipedream ingestion_batch returned non-JSON body: {exc}"
            ) from exc
        # Tolerate the workflow returning records as a flat array or under
        # a `records` key.
        if isinstance(data, list):
            data = {"records": data}
        return IngestionBatch.model_validate(
            {
                "tenant_id": str(tenant_id),
                "rep_id": str(rep_id),
                "fetched_at": data.get("fetched_at", timestamp),
                "records": data.get("records", []),
            }
        )


def build_default_ingestion_provider(
    settings: Settings | None = None,
) -> IngestionProvider:
    s = settings or get_settings()
    if s.ingestion_provider == "pipedream":
        log.info("ingestion_provider.selected", provider="pipedream")
        return PipedreamIngestionProvider(s)
    log.warning("ingestion_provider.selected", provider="stub")
    return StubIngestionProvider()
