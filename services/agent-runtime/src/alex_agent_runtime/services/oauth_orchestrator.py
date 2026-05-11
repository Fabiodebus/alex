"""OAuthOrchestrator — coordinates the per-connector OAuth dance.

Two entry points:

* :meth:`initiate` — the Slack bot calls this when the rep clicks
  "Connect Close" (or any other connector). It asks the
  :class:`OAuthProvider` to start a flow, records the ``state``
  token under ``onboarding_state.pending_oauth`` so the eventual
  callback can be correlated, and returns the URL to visit. In
  stub mode the URL points back at the runtime's
  ``/onboarding/oauth/stub_complete`` endpoint, which the Slack
  bot can short-circuit so the rep never leaves the DM.

* :meth:`handle_callback` — the Messaging Surface OAuth redirect
  handler or the stub-complete endpoint calls this with the ``state``
  + ``code`` it received from the provider. The orchestrator swaps
  the code for tokens, persists the ``token_ref`` against the
  rep's onboarding state, and returns a :class:`OAuthCompletion`
  so the conversation flow can advance.

Failure modes (provider error, unknown state, expired) return a
non-success :class:`OAuthCompletion` rather than raising, so the
conversation flow can render a retry message.
"""
from __future__ import annotations

from uuid import UUID

import structlog

from ..schemas import (
    ConnectorConnectionStatus,
    OAuthCompletion,
    OAuthInitiation,
    OnboardingConnector,
)
from ..tenant_context import tenant_scope
from .oauth_provider import OAuthProvider, OAuthProviderError
from .onboarding_state_repo import OnboardingStateRepo

log = structlog.get_logger(__name__)


class OAuthOrchestratorError(RuntimeError):
    pass


class OAuthOrchestrator:
    def __init__(
        self,
        *,
        provider: OAuthProvider,
        state_repo: OnboardingStateRepo,
    ) -> None:
        self._provider = provider
        self._state_repo = state_repo

    async def initiate(
        self,
        *,
        tenant_id: UUID,
        rep_id: UUID,
        connector: OnboardingConnector,
    ) -> OAuthInitiation:
        try:
            initiation = await self._provider.initiate(
                connector=connector, tenant_id=tenant_id, rep_id=rep_id
            )
        except OAuthProviderError as exc:
            log.warning(
                "oauth_orchestrator.initiate_failed",
                connector=connector.value,
                error=str(exc),
            )
            raise

        # Ensure the rep has an onboarding row to attach the state to.
        await self._state_repo.get_or_create(tenant_id=tenant_id, rep_id=rep_id)
        await self._state_repo.record_pending_oauth(
            tenant_id=tenant_id,
            rep_id=rep_id,
            connector=connector,
            state=initiation.state,
        )
        await self._state_repo.update_connector(
            tenant_id=tenant_id,
            rep_id=rep_id,
            connector=connector,
            status=ConnectorConnectionStatus.PENDING,
        )
        log.info(
            "oauth_orchestrator.initiated",
            connector=connector.value,
            rep_id=str(rep_id),
            stub=initiation.stub,
        )
        return initiation

    async def handle_callback(
        self,
        *,
        tenant_id: UUID,
        rep_id: UUID,
        state: str,
        code: str | None,
    ) -> OAuthCompletion:
        connector = await self._state_repo.consume_pending_oauth(
            tenant_id=tenant_id, rep_id=rep_id, state=state
        )
        if connector is None:
            log.warning(
                "oauth_orchestrator.unknown_state",
                rep_id=str(rep_id),
                state=state,
            )
            return OAuthCompletion(
                connector=OnboardingConnector.CLOSE,  # placeholder
                success=False,
                failure_reason="oauth_state_not_recognised",
                rep_id=rep_id,
                tenant_id=tenant_id,
            )
        try:
            result = await self._provider.exchange(
                connector=connector,
                state=state,
                code=code,
                tenant_id=tenant_id,
                rep_id=rep_id,
            )
        except OAuthProviderError as exc:
            log.warning(
                "oauth_orchestrator.exchange_failed",
                connector=connector.value,
                rep_id=str(rep_id),
                error=str(exc),
            )
            with tenant_scope(tenant_id):
                await self._state_repo.update_connector(
                    tenant_id=tenant_id,
                    rep_id=rep_id,
                    connector=connector,
                    status=ConnectorConnectionStatus.FAILED,
                    failure_reason=str(exc),
                )
            return OAuthCompletion(
                connector=connector,
                success=False,
                failure_reason=str(exc),
                rep_id=rep_id,
                tenant_id=tenant_id,
            )

        token_ref = result.get("token_ref")
        if not isinstance(token_ref, str):
            await self._state_repo.update_connector(
                tenant_id=tenant_id,
                rep_id=rep_id,
                connector=connector,
                status=ConnectorConnectionStatus.FAILED,
                failure_reason="missing_token_ref_in_provider_response",
            )
            return OAuthCompletion(
                connector=connector,
                success=False,
                failure_reason="missing_token_ref_in_provider_response",
                rep_id=rep_id,
                tenant_id=tenant_id,
            )

        await self._state_repo.update_connector(
            tenant_id=tenant_id,
            rep_id=rep_id,
            connector=connector,
            status=ConnectorConnectionStatus.CONNECTED,
            token_ref=token_ref,
        )
        log.info(
            "oauth_orchestrator.connected",
            connector=connector.value,
            rep_id=str(rep_id),
        )
        return OAuthCompletion(
            connector=connector,
            success=True,
            token_ref=token_ref,
            rep_id=rep_id,
            tenant_id=tenant_id,
        )

    async def mark_skipped(
        self,
        *,
        tenant_id: UUID,
        rep_id: UUID,
        connector: OnboardingConnector,
    ) -> None:
        """For optional connectors (e.g. Krisp). Records the rep's
        choice without firing the provider."""
        await self._state_repo.update_connector(
            tenant_id=tenant_id,
            rep_id=rep_id,
            connector=connector,
            status=ConnectorConnectionStatus.SKIPPED,
        )
