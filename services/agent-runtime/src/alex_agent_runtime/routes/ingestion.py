"""POST /ingestion/start — kick off the initial backfill for a rep.

Typically called by the onboarding flow after the rep has connected
their CRM + email integrations via OAuth. Always signed (same HMAC
contract as `/events` and `/connections`) so internal callers and the
messaging surface are authenticated uniformly.
"""
from __future__ import annotations

from fastapi import APIRouter, Request, status

from ..schemas import IngestionResult, IngestionStartRequest

router = APIRouter(prefix="/ingestion")


@router.post("/start", response_model=IngestionResult, status_code=status.HTTP_202_ACCEPTED)
async def post_start(payload: IngestionStartRequest, request: Request) -> IngestionResult:
    pipeline = request.app.state.ingestion_pipeline
    return await pipeline.run(
        tenant_id=payload.tenant_id,
        rep_id=payload.rep_id,
        since_days=payload.since_days,
    )
