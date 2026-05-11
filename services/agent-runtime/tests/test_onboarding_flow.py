"""Integration tests for the Phase 3 onboarding flow.

Covers the full happy path (start → connect Close → connect Google →
skip Krisp → ingesting), the retry path on an OAuth failure, and the
no-double-emit guarantee on the welcome step.

The tests use a recording StubMessagingDeliveryClient via the real
OutputRouter so the rep-facing prompts are observable from the test
without spinning up a Slack workspace.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from alex_agent_runtime.schemas import (
    ConnectorConnectionStatus,
    DeliveryChannel,
    OnboardingConnector,
    OnboardingStep,
)
from alex_agent_runtime.services.delivery_preferences import DeliveryPreferenceRepo
from alex_agent_runtime.services.delivery_tracker import DeliveryTracker
from alex_agent_runtime.services.messaging_delivery_client import (
    StubMessagingDeliveryClient,
)
from alex_agent_runtime.services.oauth_orchestrator import OAuthOrchestrator
from alex_agent_runtime.services.oauth_provider import (
    OAuthProviderError,
    StubOAuthProvider,
)
from alex_agent_runtime.services.onboarding_conversation import (
    OnboardingConversationFlow,
)
from alex_agent_runtime.services.onboarding_state_repo import OnboardingStateRepo
from alex_agent_runtime.services.output_router import OutputRouter


def _build_world() -> tuple[
    OnboardingConversationFlow,
    OAuthOrchestrator,
    OnboardingStateRepo,
    StubMessagingDeliveryClient,
]:
    state_repo = OnboardingStateRepo()
    delivery_client = StubMessagingDeliveryClient()
    tracker = DeliveryTracker(escalation_seconds=1800)
    router = OutputRouter(
        delivery_client=delivery_client,
        preferences=DeliveryPreferenceRepo(),
        tracker=tracker,
    )
    flow = OnboardingConversationFlow(state_repo=state_repo, output_router=router)
    orchestrator = OAuthOrchestrator(
        provider=StubOAuthProvider(), state_repo=state_repo
    )
    return flow, orchestrator, state_repo, delivery_client


def _titles(client: StubMessagingDeliveryClient) -> list[str]:
    return [attempt.title for _channel, attempt in client.calls]


@pytest.mark.asyncio
async def test_full_happy_path(tenant: UUID, rep: UUID):
    flow, orchestrator, state_repo, client = _build_world()

    state = await flow.start(tenant_id=tenant, rep_id=rep)
    # Welcome + Connect Close prompt are posted.
    assert state.current_step is OnboardingStep.CONNECT_CLOSE
    titles = _titles(client)
    assert any("Welcome" in t for t in titles)
    assert any("Connect Close" in t for t in titles)

    # Connect Close.
    initiation = await orchestrator.initiate(
        tenant_id=tenant, rep_id=rep, connector=OnboardingConnector.CLOSE
    )
    completion = await orchestrator.handle_callback(
        tenant_id=tenant, rep_id=rep, state=initiation.state, code=None
    )
    assert completion.success
    state_after_close = await flow.on_connector_completed(completion=completion)
    assert state_after_close.current_step is OnboardingStep.CONNECT_GOOGLE
    assert any(t.startswith("Connect Google") for t in _titles(client))

    # Connect Google.
    initiation = await orchestrator.initiate(
        tenant_id=tenant, rep_id=rep, connector=OnboardingConnector.GOOGLE
    )
    completion = await orchestrator.handle_callback(
        tenant_id=tenant, rep_id=rep, state=initiation.state, code=None
    )
    state_after_google = await flow.on_connector_completed(completion=completion)
    assert state_after_google.current_step is OnboardingStep.CONNECT_KRISP

    # Skip Krisp (optional connector).
    state_after_skip = await flow.skip_connector(
        tenant_id=tenant, rep_id=rep, connector=OnboardingConnector.KRISP
    )
    assert state_after_skip.current_step is OnboardingStep.INGESTING

    # Final "All set" message landed.
    titles = _titles(client)
    assert any(t.startswith("All set") for t in titles)

    # Connector status reflects the journey.
    final = await state_repo.get(tenant_id=tenant, rep_id=rep)
    assert final is not None
    statuses: dict[OnboardingConnector, ConnectorConnectionStatus] = {
        c: s.status for c, s in final.connector_status.items()
    }
    assert statuses[OnboardingConnector.CLOSE] is ConnectorConnectionStatus.CONNECTED
    assert statuses[OnboardingConnector.GOOGLE] is ConnectorConnectionStatus.CONNECTED
    assert statuses[OnboardingConnector.KRISP] is ConnectorConnectionStatus.SKIPPED


@pytest.mark.asyncio
async def test_retry_on_failed_oauth(tenant: UUID, rep: UUID):
    """If the provider fails the exchange, the flow posts a retry
    card and leaves the rep on the same step."""
    state_repo = OnboardingStateRepo()
    tracker = DeliveryTracker(escalation_seconds=1800)
    client = StubMessagingDeliveryClient()
    router = OutputRouter(
        delivery_client=client,
        preferences=DeliveryPreferenceRepo(),
        tracker=tracker,
    )
    flow = OnboardingConversationFlow(state_repo=state_repo, output_router=router)

    class _FlakyProvider(StubOAuthProvider):
        async def exchange(
            self,
            *,
            connector,
            state,
            code,
            tenant_id,
            rep_id,
        ) -> dict[str, Any]:
            raise OAuthProviderError("simulated provider failure")

    orchestrator = OAuthOrchestrator(provider=_FlakyProvider(), state_repo=state_repo)

    await flow.start(tenant_id=tenant, rep_id=rep)
    initiation = await orchestrator.initiate(
        tenant_id=tenant, rep_id=rep, connector=OnboardingConnector.CLOSE
    )
    completion = await orchestrator.handle_callback(
        tenant_id=tenant, rep_id=rep, state=initiation.state, code=None
    )
    assert not completion.success

    state_after_retry = await flow.on_connector_completed(completion=completion)
    # Still on CONNECT_CLOSE, retry card was posted.
    assert state_after_retry.current_step is OnboardingStep.CONNECT_CLOSE
    titles = _titles(client)
    assert any(t.startswith("Hmm — that didn't work for Close") for t in titles)

    # Connector marked FAILED.
    state = await state_repo.get(tenant_id=tenant, rep_id=rep)
    assert state is not None
    assert (
        state.connector_status[OnboardingConnector.CLOSE].status
        is ConnectorConnectionStatus.FAILED
    )


@pytest.mark.asyncio
async def test_start_is_idempotent(tenant: UUID, rep: UUID):
    """Calling start twice shouldn't re-post welcome — the rep doesn't
    want two identical messages."""
    flow, _orch, _repo, client = _build_world()
    await flow.start(tenant_id=tenant, rep_id=rep)
    first_count = len(client.calls)
    await flow.start(tenant_id=tenant, rep_id=rep)
    # No new deliveries on the second start (state was already past WELCOME).
    assert len(client.calls) == first_count


@pytest.mark.asyncio
async def test_unknown_state_returns_safe_failure(tenant: UUID, rep: UUID):
    state_repo = OnboardingStateRepo()
    orchestrator = OAuthOrchestrator(
        provider=StubOAuthProvider(), state_repo=state_repo
    )
    completion = await orchestrator.handle_callback(
        tenant_id=tenant, rep_id=rep, state="some-bogus-state", code=None
    )
    assert not completion.success
    assert completion.failure_reason == "oauth_state_not_recognised"


@pytest.mark.asyncio
async def test_skip_connector_rejects_required(tenant: UUID, rep: UUID):
    flow, _orch, _repo, _client = _build_world()
    await flow.start(tenant_id=tenant, rep_id=rep)
    with pytest.raises(ValueError):
        await flow.skip_connector(
            tenant_id=tenant, rep_id=rep, connector=OnboardingConnector.CLOSE
        )


@pytest.mark.asyncio
async def test_messaging_delivery_uses_slack_channel(tenant: UUID, rep: UUID):
    """Default DeliveryPreference is Slack — the stub client should
    receive Slack-channel calls."""
    flow, _orch, _repo, client = _build_world()
    await flow.start(tenant_id=tenant, rep_id=rep)
    channels = {call[0] for call in client.calls}
    assert channels == {DeliveryChannel.SLACK}
