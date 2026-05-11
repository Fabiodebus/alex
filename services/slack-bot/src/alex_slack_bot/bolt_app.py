"""Slack Bolt async app with the rep-facing handlers.

The Bolt app is constructed by ``create_bolt_app`` so tests can swap
configuration and inject mock clients via ``installation_store``/
``client``. Handlers stay thin: they ACK Slack within 3s, defer
substantive work to the runtime via :class:`RuntimeClient`.
"""
from __future__ import annotations

import json
from typing import Any

import structlog
from slack_bolt.async_app import AsyncApp

from .config import Settings, get_settings
from .schemas import ApprovalCallback, CallbackAction, FeedbackEvent
from .services.runtime_client import RuntimeClient

log = structlog.get_logger(__name__)


def create_bolt_app(
    *,
    settings: Settings | None = None,
    runtime_client_factory=lambda s: RuntimeClient(s),
) -> tuple[AsyncApp, RuntimeClient]:
    settings = settings or get_settings()
    app = AsyncApp(
        token=settings.slack_bot_token or "xoxb-placeholder",
        signing_secret=settings.slack_signing_secret or "placeholder-signing-secret",
        # Skip Slack's request-time signing-secret verification when the
        # placeholder is in play (tests + initial smoke). In production
        # SLACK_SIGNING_SECRET must be set.
        request_verification_enabled=bool(settings.slack_signing_secret),
    )
    runtime_client = runtime_client_factory(settings)

    @app.action({"action_id": "alex.approve"})
    async def on_approve(ack, body, client):
        await ack()
        await _handle_action(body=body, action=CallbackAction.APPROVE, runtime_client=runtime_client)

    @app.action({"action_id": "alex.discard"})
    async def on_discard(ack, body):
        await ack()
        await _handle_action(body=body, action=CallbackAction.DISCARD, runtime_client=runtime_client)

    @app.action({"action_id": "alex.edit"})
    async def on_edit(ack, body):
        await ack()
        await _handle_action(body=body, action=CallbackAction.EDIT, runtime_client=runtime_client)

    @app.action({"action_id": "alex.feedback"})
    async def on_feedback(ack, body):
        await ack()
        payload = _parse_button_value(body)
        if payload is None:
            return
        rating_str = payload.get("rating", "1")
        try:
            rating = int(rating_str)
        except (TypeError, ValueError):
            rating = 1
        event = FeedbackEvent(
            task_id=payload["task_id"],
            rep_id=payload["rep_id"],
            rating=rating,
            note=payload.get("note"),
        )
        tenant_id = _resolve_tenant(body) or payload.get("tenant_id", "")
        await runtime_client.post_feedback(tenant_id=tenant_id, event=event)

    @app.event("app_mention")
    async def on_app_mention(event, say):  # noqa: ARG001 — say is the standard Bolt argument
        await say(
            text=(
                "Hi! I'm Alex. I'll DM you proactively when there's something to review. "
                "Use `/alex help` for what I can do."
            )
        )

    @app.command("/alex")
    async def on_slash_alex(ack, respond, command):  # noqa: ARG001
        await ack()
        text = (command.get("text") or "").strip()
        if text in {"help", ""}:
            await respond(
                response_type="ephemeral",
                text=(
                    "Commands:\n"
                    "• `/alex prefs` — see your delivery preferences\n"
                    "• `/alex pause` — pause non-urgent DMs"
                ),
            )
        elif text == "prefs":
            await respond(response_type="ephemeral", text="Delivery preferences are stored in TenantConfig — settings UI coming with WO #5b.")
        elif text == "pause":
            await respond(response_type="ephemeral", text="Pause acknowledged (handled by the Agent Runtime's TenantConfig).")
        else:
            await respond(response_type="ephemeral", text=f"Unknown command: `{text}`. Try `/alex help`.")

    return app, runtime_client


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
async def _handle_action(
    *,
    body: dict[str, Any],
    action: CallbackAction,
    runtime_client: RuntimeClient,
) -> None:
    payload = _parse_button_value(body)
    if payload is None:
        log.warning("slack.action.no_value_payload", body_keys=list(body.keys()))
        return
    callback = ApprovalCallback(
        task_id=payload["task_id"],
        rep_id=payload["rep_id"],
        action=action,
    )
    tenant_id = _resolve_tenant(body) or payload.get("tenant_id", "")
    try:
        await runtime_client.post_approval_callback(tenant_id=tenant_id, callback=callback)
    except Exception as exc:  # pragma: no cover — Bolt swallows exceptions otherwise
        log.exception("slack.action.runtime_forward_failed", error=str(exc))
        raise


def _parse_button_value(body: dict[str, Any]) -> dict[str, str] | None:
    actions = body.get("actions") or []
    if not actions:
        return None
    raw = actions[0].get("value")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _resolve_tenant(body: dict[str, Any]) -> str | None:
    # When the AgentOutput is delivered we embed the tenant in the
    # button value blob (see render_agent_output). Some richer payloads
    # carry it in `team.id`; preferring the value blob keeps the
    # tenant_id we know about over Slack's team identifier.
    payload = _parse_button_value(body)
    if payload and "tenant_id" in payload:
        return payload["tenant_id"]
    return body.get("team", {}).get("id")
