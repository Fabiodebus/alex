"""OnboardingConversationFlow — drives the rep-facing onboarding sequence.

The flow is a tiny state machine over :class:`OnboardingStep`. Each
transition produces one rep-facing :class:`DeliveryRequest` (the
title + body + action buttons) dispatched via :class:`OutputRouter`.
The Slack bot renders the buttons; the bot's action handlers call
back into :class:`OAuthOrchestrator` and this flow's
``on_connector_completed`` / ``on_connector_failed`` / ``on_skip``
entry points.

The blueprint's "no documentation, no IT involvement" rule lives in
the copy: every message is plain-language and stays in the DM.
"""
from __future__ import annotations

from uuid import UUID

import structlog

from ..schemas import (
    ConnectorConnectionStatus,
    DeliveryRequest,
    OAuthCompletion,
    OnboardingConnector,
    OnboardingState,
    OnboardingStep,
    OutputType,
)
from ..tenant_context import tenant_scope
from .onboarding_state_repo import OnboardingStateRepo
from .output_router import OutputRouter

log = structlog.get_logger(__name__)


# Ordered connector sequence. Required-to-completion connectors come
# first; Krisp is optional and the conversation lets the rep skip it.
_CONNECTOR_SEQUENCE: list[OnboardingConnector] = [
    OnboardingConnector.CLOSE,
    OnboardingConnector.GOOGLE,
    OnboardingConnector.KRISP,
]
_OPTIONAL_CONNECTORS: set[OnboardingConnector] = {OnboardingConnector.KRISP}

_STEP_FOR_CONNECTOR: dict[OnboardingConnector, OnboardingStep] = {
    OnboardingConnector.CLOSE: OnboardingStep.CONNECT_CLOSE,
    OnboardingConnector.GOOGLE: OnboardingStep.CONNECT_GOOGLE,
    OnboardingConnector.KRISP: OnboardingStep.CONNECT_KRISP,
}


class OnboardingConversationFlow:
    def __init__(
        self,
        *,
        state_repo: OnboardingStateRepo,
        output_router: OutputRouter,
    ) -> None:
        self._state_repo = state_repo
        self._output_router = output_router

    # ------------------------------------------------------------------
    # Entry points called by the slash command / OAuth orchestrator.
    # ------------------------------------------------------------------
    async def start(self, *, tenant_id: UUID, rep_id: UUID) -> OnboardingState:
        state = await self._state_repo.get_or_create(tenant_id=tenant_id, rep_id=rep_id)
        if state.current_step is OnboardingStep.WELCOME and not state.completed_steps:
            await self._post_welcome(tenant_id=tenant_id, rep_id=rep_id)
            state = await self._advance_to_next_connector(
                tenant_id=tenant_id, rep_id=rep_id, just_completed=None
            )
        return state

    async def on_connector_completed(
        self, *, completion: OAuthCompletion
    ) -> OnboardingState:
        if not completion.success:
            return await self.on_connector_failed(completion=completion)
        with tenant_scope(completion.tenant_id):
            return await self._advance_to_next_connector(
                tenant_id=completion.tenant_id,
                rep_id=completion.rep_id,
                just_completed=completion.connector,
            )

    async def on_connector_failed(
        self, *, completion: OAuthCompletion
    ) -> OnboardingState:
        state = await self._state_repo.get(
            tenant_id=completion.tenant_id, rep_id=completion.rep_id
        )
        if state is None:
            raise RuntimeError(
                "OnboardingConversationFlow.on_connector_failed without a started state"
            )
        await self._post_retry(
            tenant_id=completion.tenant_id,
            rep_id=completion.rep_id,
            connector=completion.connector,
            reason=completion.failure_reason or "unknown_error",
        )
        return state

    async def skip_connector(
        self,
        *,
        tenant_id: UUID,
        rep_id: UUID,
        connector: OnboardingConnector,
    ) -> OnboardingState:
        """Optional-connector skip path. Marks the row + advances."""
        if connector not in _OPTIONAL_CONNECTORS:
            raise ValueError(f"Connector {connector.value} is not optional")
        await self._state_repo.update_connector(
            tenant_id=tenant_id,
            rep_id=rep_id,
            connector=connector,
            status=ConnectorConnectionStatus.SKIPPED,
        )
        with tenant_scope(tenant_id):
            return await self._advance_to_next_connector(
                tenant_id=tenant_id, rep_id=rep_id, just_completed=connector
            )

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------
    async def _advance_to_next_connector(
        self,
        *,
        tenant_id: UUID,
        rep_id: UUID,
        just_completed: OnboardingConnector | None,
    ) -> OnboardingState:
        state = await self._state_repo.get(tenant_id=tenant_id, rep_id=rep_id)
        if state is None:
            raise RuntimeError("missing onboarding_state row")
        next_connector = self._next_pending_connector(state)
        if next_connector is None:
            new_state = await self._state_repo.advance_step(
                tenant_id=tenant_id,
                rep_id=rep_id,
                new_step=OnboardingStep.INGESTING,
                complete_previous=(
                    _STEP_FOR_CONNECTOR[just_completed] if just_completed else None
                ),
            )
            await self._post_all_set(tenant_id=tenant_id, rep_id=rep_id)
            return new_state

        new_state = await self._state_repo.advance_step(
            tenant_id=tenant_id,
            rep_id=rep_id,
            new_step=_STEP_FOR_CONNECTOR[next_connector],
            complete_previous=(
                _STEP_FOR_CONNECTOR[just_completed] if just_completed else None
            ),
        )
        await self._post_connector_prompt(
            tenant_id=tenant_id, rep_id=rep_id, connector=next_connector
        )
        return new_state

    def _next_pending_connector(
        self, state: OnboardingState
    ) -> OnboardingConnector | None:
        for connector in _CONNECTOR_SEQUENCE:
            status = state.connector_status.get(connector)
            if status is None:
                return connector
            if status.status in (
                ConnectorConnectionStatus.NOT_STARTED,
                ConnectorConnectionStatus.PENDING,
                ConnectorConnectionStatus.FAILED,
            ):
                return connector
        return None

    # ------------------------------------------------------------------
    # Rep-facing posts (all flow through OutputRouter)
    # ------------------------------------------------------------------
    async def _post_welcome(self, *, tenant_id: UUID, rep_id: UUID) -> None:
        await self._deliver(
            tenant_id=tenant_id,
            rep_id=rep_id,
            output_id=f"onboarding:welcome:{rep_id}",
            title="Welcome to Alex 👋",
            body=(
                "I'll get you set up in under 15 minutes — no docs, no IT. "
                "We'll connect three tools so I can be useful from day one:\n"
                "• *Close* — so I can read your pipeline and propose updates\n"
                "• *Google* — for email + calendar context\n"
                "• *Krisp.ai* (optional) — to ingest meeting recordings\n\n"
                "Ready when you are."
            ),
            actions=[],
            output_type=OutputType.NOTIFICATION,
        )

    async def _post_connector_prompt(
        self,
        *,
        tenant_id: UUID,
        rep_id: UUID,
        connector: OnboardingConnector,
    ) -> None:
        copy = _CONNECTOR_COPY[connector]
        actions = [
            {
                "action_id": f"onboarding.connect.{connector.value}",
                "label": copy["connect_label"],
                "value": {
                    "connector": connector.value,
                    "tenant_id": str(tenant_id),
                    "rep_id": str(rep_id),
                },
                "style": "primary",
            }
        ]
        if connector in _OPTIONAL_CONNECTORS:
            actions.append(
                {
                    "action_id": f"onboarding.skip.{connector.value}",
                    "label": "Skip for now",
                    "value": {
                        "connector": connector.value,
                        "tenant_id": str(tenant_id),
                        "rep_id": str(rep_id),
                    },
                }
            )
        await self._deliver(
            tenant_id=tenant_id,
            rep_id=rep_id,
            output_id=f"onboarding:prompt:{connector.value}:{rep_id}",
            title=copy["title"],
            body=copy["body"],
            actions=actions,
            output_type=OutputType.NOTIFICATION,
        )

    async def _post_retry(
        self,
        *,
        tenant_id: UUID,
        rep_id: UUID,
        connector: OnboardingConnector,
        reason: str,
    ) -> None:
        copy = _CONNECTOR_COPY[connector]
        await self._deliver(
            tenant_id=tenant_id,
            rep_id=rep_id,
            output_id=f"onboarding:retry:{connector.value}:{rep_id}",
            title=f"Hmm — that didn't work for {copy['short_name']}",
            body=(
                f"I couldn't complete the {copy['short_name']} connection ({reason}). "
                "No worries — tap below to try again. Nothing has been saved."
            ),
            actions=[
                {
                    "action_id": f"onboarding.connect.{connector.value}",
                    "label": f"Try connecting {copy['short_name']} again",
                    "value": {
                        "connector": connector.value,
                        "tenant_id": str(tenant_id),
                        "rep_id": str(rep_id),
                    },
                    "style": "primary",
                }
            ],
            output_type=OutputType.NOTIFICATION,
        )

    async def _post_all_set(self, *, tenant_id: UUID, rep_id: UUID) -> None:
        await self._deliver(
            tenant_id=tenant_id,
            rep_id=rep_id,
            output_id=f"onboarding:ingesting:{rep_id}",
            title="All set — I'm learning the ropes now ✨",
            body=(
                "Connections are live. I'm reading recent deals, emails, and calendar "
                "events in the background so my first output is useful, not generic. "
                "This typically takes a few minutes; you'll hear from me again as "
                "soon as something's worth your attention."
            ),
            actions=[],
            output_type=OutputType.NOTIFICATION,
        )

    async def _deliver(
        self,
        *,
        tenant_id: UUID,
        rep_id: UUID,
        output_id: str,
        title: str,
        body: str,
        actions: list[dict[str, object]],
        output_type: OutputType,
    ) -> None:
        with tenant_scope(tenant_id):
            await self._output_router.deliver(
                DeliveryRequest(
                    tenant_id=tenant_id,
                    rep_id=rep_id,
                    output_id=output_id,
                    output_type=output_type,
                    title=title,
                    body=body,
                    metadata={
                        "onboarding": True,
                        "actions": actions,
                    },
                )
            )


# ---------------------------------------------------------------------------
# Copy
# ---------------------------------------------------------------------------
_CONNECTOR_COPY: dict[OnboardingConnector, dict[str, str]] = {
    OnboardingConnector.CLOSE: {
        "short_name": "Close",
        "title": "Connect Close CRM",
        "body": (
            "I'll read your pipeline and propose CRM updates — never write without your "
            "approval. You can revoke access any time."
        ),
        "connect_label": "Connect Close",
    },
    OnboardingConnector.GOOGLE: {
        "short_name": "Google",
        "title": "Connect Google (Gmail + Calendar)",
        "body": (
            "Gmail for thread context and outbound drafts; Calendar for meeting prep "
            "and post-meeting follow-ups. I only read what's relevant to your "
            "deals."
        ),
        "connect_label": "Connect Google",
    },
    OnboardingConnector.KRISP: {
        "short_name": "Krisp.ai",
        "title": "Connect Krisp.ai (optional)",
        "body": (
            "If you record meetings with Krisp.ai, I can ingest the transcripts and "
            "write better follow-ups. Totally optional — you can skip this and add "
            "it later."
        ),
        "connect_label": "Connect Krisp.ai",
    },
}
