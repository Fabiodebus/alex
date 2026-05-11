"""Test fixtures.

These tests run against the live Postgres reachable via ``DATABASE_URL``
(the same connection the runtime uses in dev). Each test gets its own
tenant + rep so they don't collide. The schema must already be migrated
(``alembic upgrade head`` from ``services/data-layer``); the tests will
not run migrations themselves.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from alex_agent_runtime.config import get_settings
from alex_agent_runtime.db import admin_session, dispose_engine, init_engine
from alex_agent_runtime.main import create_app
from alex_agent_runtime.tenant_context import tenant_scope


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def _engine():
    init_engine(get_settings())
    yield
    await dispose_engine()


@pytest_asyncio.fixture
async def tenant(_engine) -> AsyncIterator[UUID]:
    """Insert a fresh tenant, yield its id, hard-delete on teardown."""
    tenant_id = uuid4()
    async with admin_session() as session:
        await session.execute(
            text(
                "INSERT INTO tenants (id, name) VALUES (:id, :name)"
            ),
            {"id": str(tenant_id), "name": f"test-{tenant_id}"},
        )
    yield tenant_id
    async with admin_session(allow_audit_purge=True) as session:
        await session.execute(
            text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)}
        )


@pytest_asyncio.fixture
async def rep(tenant: UUID) -> UUID:
    rep_id = uuid4()
    with tenant_scope(tenant):
        from alex_agent_runtime.db import transactional_session

        async with transactional_session() as session:
            await session.execute(
                text(
                    "INSERT INTO reps (id, tenant_id, email, display_name) "
                    "VALUES (:id, :tenant_id, :email, :name)"
                ),
                {
                    "id": str(rep_id),
                    "tenant_id": str(tenant),
                    "email": f"test-{rep_id}@example.com",
                    "name": "Test Rep",
                },
            )
    return rep_id


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app()
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
