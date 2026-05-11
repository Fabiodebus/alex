"""Tests for the tenant_config-backed TenantFlagRepo."""
from __future__ import annotations

from uuid import UUID

import pytest

from alex_agent_runtime.services.tenant_flags import FLAG_MEDDIC_ENABLED, TenantFlagRepo


@pytest.mark.asyncio
async def test_get_bool_falls_back_to_default(tenant: UUID):
    repo = TenantFlagRepo()
    assert (
        await repo.get_bool(
            tenant_id=tenant, flag=FLAG_MEDDIC_ENABLED, default=False
        )
        is False
    )


@pytest.mark.asyncio
async def test_set_then_get(tenant: UUID):
    repo = TenantFlagRepo()
    await repo.set_bool(tenant_id=tenant, flag=FLAG_MEDDIC_ENABLED, enabled=True)
    assert (
        await repo.get_bool(tenant_id=tenant, flag=FLAG_MEDDIC_ENABLED)
        is True
    )
    await repo.set_bool(tenant_id=tenant, flag=FLAG_MEDDIC_ENABLED, enabled=False)
    assert (
        await repo.get_bool(tenant_id=tenant, flag=FLAG_MEDDIC_ENABLED)
        is False
    )
