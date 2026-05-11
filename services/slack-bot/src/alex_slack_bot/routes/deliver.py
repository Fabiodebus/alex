"""POST /deliver — Agent Runtime → Slack DM."""
from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException, Request, status
from slack_sdk.errors import SlackApiError

from ..schemas import AgentOutput
from ..services.block_kit import render_agent_output

log = structlog.get_logger(__name__)
router = APIRouter()


@router.post("/deliver", status_code=status.HTTP_202_ACCEPTED)
async def post_deliver(payload: AgentOutput, request: Request) -> dict[str, object]:
    bolt_app = request.app.state.bolt_app
    client = bolt_app.client  # AsyncWebClient

    channel_id = payload.dm_channel_id
    if not channel_id:
        try:
            opened = await client.conversations_open(users=payload.slack_user_id)
        except SlackApiError as exc:
            log.warning("deliver.conversations_open_failed", error=str(exc))
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"error": "conversations_open_failed", "slack_error": exc.response["error"]},
            ) from exc
        channel_id = opened["channel"]["id"]

    blocks = render_agent_output(payload)
    try:
        result = await client.chat_postMessage(
            channel=channel_id,
            blocks=blocks,
            text=payload.title,  # fallback text for notifications
        )
    except SlackApiError as exc:
        log.warning("deliver.post_message_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "post_message_failed", "slack_error": exc.response["error"]},
        ) from exc

    log.info(
        "deliver.posted",
        task_id=str(payload.task_id),
        channel_id=channel_id,
        slack_user_id=payload.slack_user_id,
    )
    return {
        "task_id": str(payload.task_id),
        "slack_user_id": payload.slack_user_id,
        "channel_id": channel_id,
        "ts": result.get("ts"),
    }
