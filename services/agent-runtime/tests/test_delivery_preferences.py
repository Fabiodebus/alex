"""Tests for the tenant_config-backed DeliveryPreferenceRepo."""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from alex_agent_runtime.schemas import DeliveryChannel
from alex_agent_runtime.services.delivery_preferences import DeliveryPreferenceRepo


@pytest.mark.asyncio
async def test_unconfigured_falls_back_to_slack(tenant: UUID, rep: UUID):
    repo = DeliveryPreferenceRepo()
    channel = await repo.get_channel(
        tenant_id=tenant, rep_id=rep, output_type="approval_request"
    )
    assert channel is DeliveryChannel.SLACK


@pytest.mark.asyncio
async def test_explicit_override_wins(tenant: UUID, rep: UUID):
    repo = DeliveryPreferenceRepo()
    await repo.set_channel(
        tenant_id=tenant,
        rep_id=rep,
        output_type="approval_request",
        channel=DeliveryChannel.TEAMS,
    )
    channel = await repo.get_channel(
        tenant_id=tenant, rep_id=rep, output_type="approval_request"
    )
    assert channel is DeliveryChannel.TEAMS


@pytest.mark.asyncio
async def test_rep_default_resolves_for_unknown_output_type(tenant: UUID, rep: UUID):
    repo = DeliveryPreferenceRepo()
    await repo.set_channel(
        tenant_id=tenant,
        rep_id=rep,
        output_type="*",
        channel=DeliveryChannel.TEAMS,
    )
    channel = await repo.get_channel(
        tenant_id=tenant, rep_id=rep, output_type="meeting_prep"
    )
    assert channel is DeliveryChannel.TEAMS


@pytest.mark.asyncio
async def test_preferences_are_per_rep(tenant: UUID, rep: UUID):
    """A second rep with no entry still gets the global default."""
    repo = DeliveryPreferenceRepo()
    await repo.set_channel(
        tenant_id=tenant,
        rep_id=rep,
        output_type="approval_request",
        channel=DeliveryChannel.TEAMS,
    )
    other_rep = uuid4()
    channel = await repo.get_channel(
        tenant_id=tenant, rep_id=other_rep, output_type="approval_request"
    )
    assert channel is DeliveryChannel.SLACK
