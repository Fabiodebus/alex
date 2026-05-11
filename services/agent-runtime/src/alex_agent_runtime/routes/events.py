"""POST /events — inbound IntegrationEvent webhook."""
from __future__ import annotations

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from ..schemas import IntegrationEvent
from ..services.event_processor import EventProcessor

router = APIRouter()


@router.post("/events", status_code=status.HTTP_202_ACCEPTED)
async def post_events(event: IntegrationEvent, request: Request) -> JSONResponse:
    processor: EventProcessor = request.app.state.event_processor
    result = await processor.process(event)
    return JSONResponse(
        status_code=status.HTTP_200_OK if result.deduplicated else status.HTTP_202_ACCEPTED,
        content={
            "accepted": result.accepted,
            "deduplicated": result.deduplicated,
            "handler": result.handler,
            "event_id": result.event_id,
        },
    )
