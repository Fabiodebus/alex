"""POST /deliver — Agent Runtime → Teams DM via proactive message."""
from __future__ import annotations

import structlog
from botbuilder.core import MessageFactory, TurnContext
from botbuilder.schema import ConversationReference
from fastapi import APIRouter, HTTPException, Request, status

from ..config import get_settings
from ..schemas import AgentOutput
from ..services.adaptive_cards import render_agent_output

log = structlog.get_logger(__name__)
router = APIRouter()


@router.post("/deliver", status_code=status.HTTP_202_ACCEPTED)
async def post_deliver(payload: AgentOutput, request: Request) -> dict[str, object]:
    adapter = request.app.state.adapter
    settings = get_settings()

    try:
        reference = ConversationReference().deserialize(payload.conversation_reference)
    except Exception as exc:  # pragma: no cover — input shape errors
        log.warning("deliver.bad_reference", error=str(exc))
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_conversation_reference"},
        ) from exc

    attachment = render_agent_output(payload)

    delivered: dict[str, object] = {}

    async def _send(turn_context: TurnContext) -> None:
        message = MessageFactory.attachment(attachment)
        result = await turn_context.send_activity(message)
        if result is not None:
            delivered["id"] = result.id

    try:
        await adapter.continue_conversation(
            reference,
            _send,
            settings.microsoft_app_id or None,
        )
    except Exception as exc:
        log.warning("deliver.continue_conversation_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "teams_delivery_failed"},
        ) from exc

    log.info(
        "deliver.posted",
        task_id=str(payload.task_id),
        message_id=delivered.get("id"),
    )
    return {
        "task_id": str(payload.task_id),
        "message_id": delivered.get("id"),
    }
