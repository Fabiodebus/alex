"""POST /api/messages — Bot Framework activity webhook.

Forwards the inbound Activity to the configured CloudAdapter, which
verifies the auth token, deserialises the activity, runs the bot's
on_turn pipeline, and writes back any reply.
"""
from __future__ import annotations

from fastapi import APIRouter, Request, Response, status
from botbuilder.schema import Activity

router = APIRouter()


@router.post("/api/messages", status_code=status.HTTP_200_OK)
async def post_messages(request: Request) -> Response:
    auth_header = request.headers.get("Authorization", "")
    activity_json = await request.json()
    activity = Activity().deserialize(activity_json)

    adapter = request.app.state.adapter
    bot = request.app.state.bot
    invoke_response = await adapter.process_activity(auth_header, activity, bot.on_turn)
    if invoke_response is not None:
        return Response(
            content=invoke_response.body,
            status_code=invoke_response.status,
            media_type="application/json",
        )
    return Response(status_code=status.HTTP_200_OK)
