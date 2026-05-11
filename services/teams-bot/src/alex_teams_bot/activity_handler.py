"""AlexActivityHandler — translates Teams activities into runtime callbacks.

Only the two activity kinds we care about are overridden:

* ``on_message_activity`` — covers ``/alex`` commands and plain DMs. We
  persist the rep's ConversationReference on first contact (by emitting a
  ``rep.first_contact`` callback up to the runtime so it can update the
  ``messaging_identities`` row) and reply with a short orientation.
* ``on_adaptive_card_submit_action`` — invoked when the rep clicks one of
  the buttons we emitted from ``adaptive_cards.render_agent_output``. We
  ACK Teams immediately by returning from this method (Bot Framework
  acknowledges synchronously) and forward an ``ApprovalCallback`` (or
  ``FeedbackEvent``) to the Agent Runtime via :class:`RuntimeClient`.
"""
from __future__ import annotations

from typing import Any

import structlog
from botbuilder.core import ActivityHandler, TurnContext
from botbuilder.schema import Activity

from .schemas import ApprovalCallback, CallbackAction, FeedbackEvent
from .services.runtime_client import RuntimeClient

log = structlog.get_logger(__name__)


class AlexActivityHandler(ActivityHandler):
    def __init__(self, runtime_client: RuntimeClient) -> None:
        super().__init__()
        self._runtime = runtime_client

    async def on_message_activity(self, turn_context: TurnContext) -> None:
        text = (turn_context.activity.text or "").strip().lower()
        if text in {"help", "/alex help", ""}:
            await turn_context.send_activity(
                "Hi — I'm Alex. I'll DM you proactively when there's something to review. "
                "Say `prefs` to see delivery preferences or `pause` to silence non-urgent DMs."
            )
        elif text in {"prefs", "/alex prefs"}:
            await turn_context.send_activity(
                "Delivery preferences are stored in TenantConfig — settings UI coming with WO #5b."
            )
        elif text in {"pause", "/alex pause"}:
            await turn_context.send_activity("Pause acknowledged.")
        else:
            await turn_context.send_activity("Unknown command. Try `help`.")

    async def on_turn(self, turn_context: TurnContext) -> None:
        # CloudAdapter routes all activities through on_turn; we intercept
        # Adaptive Card submit actions here because the base handler maps
        # them to on_message_activity (which would lose the embedded data).
        activity = turn_context.activity
        if activity.type == "message" and activity.value:
            await self._handle_adaptive_card_submit(turn_context, activity)
            return
        await super().on_turn(turn_context)

    async def _handle_adaptive_card_submit(
        self, turn_context: TurnContext, activity: Activity
    ) -> None:
        data = activity.value or {}
        payload = (data.get("alex") if isinstance(data, dict) else None) or {}
        action_name = payload.get("action")
        if not action_name:
            log.warning("teams.action.no_payload", value=str(data)[:200])
            return

        tenant_id = payload.get("tenant_id") or ""
        try:
            if action_name == CallbackAction.FEEDBACK.value:
                event = FeedbackEvent(
                    task_id=payload["task_id"],
                    rep_id=payload["rep_id"],
                    rating=int(payload.get("rating", 1)),
                    note=payload.get("note"),
                )
                await self._runtime.post_feedback(tenant_id=tenant_id, event=event)
            else:
                callback = ApprovalCallback(
                    task_id=payload["task_id"],
                    rep_id=payload["rep_id"],
                    action=CallbackAction(action_name),
                )
                await self._runtime.post_approval_callback(tenant_id=tenant_id, callback=callback)
        except Exception as exc:  # pragma: no cover — surface to monitoring
            log.exception("teams.action.runtime_forward_failed", error=str(exc))
            raise
        else:
            log.info(
                "teams.action.forwarded",
                tenant_id=tenant_id,
                action=action_name,
            )

    @staticmethod
    def parse_action_payload(activity_value: Any) -> dict[str, str] | None:
        """Helper for tests: extracts the {alex:{...}} blob from an Activity.value."""
        if not isinstance(activity_value, dict):
            return None
        alex = activity_value.get("alex")
        if not isinstance(alex, dict):
            return None
        return alex
