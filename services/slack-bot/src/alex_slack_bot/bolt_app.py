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
        if text == "onboard":
            await _start_onboarding(
                respond=respond, command=command, runtime_client=runtime_client
            )
            return
        if text in {"help", ""}:
            await respond(
                response_type="ephemeral",
                text=(
                    "Commands:\n"
                    "• `/alex onboard` — set up Alex (~15 min, conversational)\n"
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

    # ----- Onboarding action handlers (action_id like onboarding.connect.close) ----
    for connector in ("close", "google", "krisp"):
        app.action({"action_id": f"onboarding.connect.{connector}"})(
            _build_connect_handler(connector=connector, runtime_client=runtime_client)
        )
    for connector in ("krisp",):
        app.action({"action_id": f"onboarding.skip.{connector}"})(
            _build_skip_handler(connector=connector, runtime_client=runtime_client)
        )

    return app, runtime_client


# ---------------------------------------------------------------------------
# Onboarding handlers
# ---------------------------------------------------------------------------
async def _start_onboarding(*, respond, command, runtime_client: RuntimeClient) -> None:
    """Forward `/alex onboard` to the runtime's start_for_slack_user.

    The runtime resolves (or provisions) the rep, kicks off the
    conversation flow, and immediately POSTs the welcome card back to
    the bot's `/deliver` endpoint. We just acknowledge so Slack closes
    the slash command UI."""
    settings = get_settings()
    tenant_id = settings.alex_demo_tenant_id or command.get("team_id") or ""
    if not tenant_id:
        await respond(response_type="ephemeral", text="Couldn't resolve workspace.")
        return
    slack_user_id = command.get("user_id", "")
    try:
        await runtime_client.post_onboarding_start(
            tenant_id=tenant_id,
            slack_user_id=slack_user_id,
            slack_team_id=command.get("team_id"),
            display_name=command.get("user_name"),
        )
    except Exception as exc:  # pragma: no cover — surfaced to the rep
        log.exception("slack.onboarding.start_failed", error=str(exc))
        await respond(
            response_type="ephemeral",
            text=f"Couldn't kick off onboarding: {exc}",
        )
        return
    await respond(
        response_type="ephemeral",
        text="I'll DM you the first step in a moment.",
    )


def _build_connect_handler(*, connector: str, runtime_client: RuntimeClient):
    async def _handler(ack, body):
        await ack()
        payload = _parse_button_value(body) or {}
        tenant_id = payload.get("tenant_id") or get_settings().alex_demo_tenant_id or ""
        rep_id = payload.get("rep_id")
        if not tenant_id or not rep_id:
            log.warning("slack.onboarding.connect.no_payload", connector=connector)
            return
        try:
            response = await runtime_client.post_onboarding_initiate(
                tenant_id=tenant_id,
                rep_id=rep_id,
                connector=connector,
            )
        except Exception as exc:
            log.exception("slack.onboarding.initiate_failed", connector=connector, error=str(exc))
            return
        # In stub mode the runtime returns a stub URL we can short-circuit
        # — fetching it completes the OAuth round-trip locally. In real
        # OAuth mode we'd post the URL back to the rep so they can
        # authorise in the browser.
        if response.get("stub"):
            try:
                await runtime_client.get_url(
                    url=str(response["authorize_url"]),
                    tenant_id=tenant_id,
                )
            except Exception as exc:
                log.exception(
                    "slack.onboarding.stub_complete_failed",
                    connector=connector,
                    error=str(exc),
                )

    _handler.__qualname__ = f"on_onboarding_connect_{connector}"
    return _handler


def _build_skip_handler(*, connector: str, runtime_client: RuntimeClient):
    async def _handler(ack, body):
        await ack()
        payload = _parse_button_value(body) or {}
        tenant_id = payload.get("tenant_id") or get_settings().alex_demo_tenant_id or ""
        rep_id = payload.get("rep_id")
        if not tenant_id or not rep_id:
            log.warning("slack.onboarding.skip.no_payload", connector=connector)
            return
        try:
            await runtime_client.post_onboarding_skip(
                tenant_id=tenant_id,
                rep_id=rep_id,
                connector=connector,
            )
        except Exception as exc:
            log.exception("slack.onboarding.skip_failed", connector=connector, error=str(exc))

    _handler.__qualname__ = f"on_onboarding_skip_{connector}"
    return _handler


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
