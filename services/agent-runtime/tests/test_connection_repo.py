"""Tests for the oauth_connections repository helpers."""
from __future__ import annotations

from uuid import UUID

import pytest

from alex_agent_runtime.schemas import ConnectionStatus, ConnectionStatusUpdate
from alex_agent_runtime.services.connection_repo import get_connection, upsert_connection


@pytest.mark.asyncio
async def test_upsert_then_get_round_trips(tenant: UUID, rep: UUID):
    update = ConnectionStatusUpdate(
        tenant_id=tenant,
        rep_id=rep,
        source="google",
        status=ConnectionStatus.CONNECTED,
        scopes=["gmail.send", "drive.file"],
        vault_ref="pd_vault::test::google",
    )
    view = await upsert_connection(update)
    assert view.status == ConnectionStatus.CONNECTED
    assert view.scopes == ["gmail.send", "drive.file"]

    fetched = await get_connection(tenant_id=tenant, rep_id=rep, source="google")
    assert fetched is not None
    assert fetched.status == ConnectionStatus.CONNECTED
    assert fetched.scopes == ["gmail.send", "drive.file"]


@pytest.mark.asyncio
async def test_upsert_updates_on_conflict(tenant: UUID, rep: UUID):
    await upsert_connection(
        ConnectionStatusUpdate(
            tenant_id=tenant,
            rep_id=rep,
            source="hubspot",
            status=ConnectionStatus.CONNECTED,
            scopes=["read", "write"],
        )
    )
    await upsert_connection(
        ConnectionStatusUpdate(
            tenant_id=tenant,
            rep_id=rep,
            source="hubspot",
            status=ConnectionStatus.EXPIRED,
            scopes=["read"],
        )
    )
    fetched = await get_connection(tenant_id=tenant, rep_id=rep, source="hubspot")
    assert fetched is not None
    assert fetched.status == ConnectionStatus.EXPIRED
    assert fetched.scopes == ["read"]


@pytest.mark.asyncio
async def test_get_missing_returns_none(tenant: UUID, rep: UUID):
    assert await get_connection(tenant_id=tenant, rep_id=rep, source="never_connected") is None
