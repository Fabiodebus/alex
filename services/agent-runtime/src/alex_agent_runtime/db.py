"""Async SQLAlchemy engine, session factory, and tenant-scoped transactions.

`transactional_session()` runs `SET LOCAL app.tenant_id = '<uuid>'` as the
first statement on a fresh transaction so every subsequent statement on
that connection is bound by the data-layer's row-level security policies.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .config import Settings, get_settings
from .tenant_context import current_tenant

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _to_async_url(url: str) -> str:
    """Return an asyncio-driver URL.

    Alembic uses `postgresql+psycopg://` (sync). The async session needs
    `postgresql+psycopg_async://` so SQLAlchemy picks the async driver.
    """
    if "+psycopg_async" in url:
        return url
    if url.startswith("postgresql+psycopg://"):
        return url.replace("postgresql+psycopg://", "postgresql+psycopg_async://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg_async://", 1)
    return url


def init_engine(settings: Settings | None = None) -> AsyncEngine:
    """Initialise the global engine. Idempotent."""
    global _engine, _session_factory
    if _engine is not None:
        return _engine
    settings = settings or get_settings()
    _engine = create_async_engine(_to_async_url(settings.database_url), pool_pre_ping=True)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


def session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        init_engine()
    assert _session_factory is not None  # for mypy
    return _session_factory


@asynccontextmanager
async def transactional_session() -> AsyncIterator[AsyncSession]:
    """Yield an `AsyncSession` inside a transaction with `app.tenant_id` set.

    Reads the tenant from the contextvar (raising MissingTenantError if
    unset) so every code path in the runtime is forced to declare its
    tenant scope.
    """
    tenant_id = current_tenant()
    factory = session_factory()
    async with factory() as session:
        async with session.begin():
            # `SET LOCAL` doesn't accept bind parameters; use the SQL function
            # variant so the uuid value goes through psycopg's parameter
            # quoting instead of string interpolation.
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": str(tenant_id)},
            )
            yield session


@asynccontextmanager
async def admin_session(*, allow_audit_purge: bool = False) -> AsyncIterator[AsyncSession]:
    """Tenant-bypassing session for system jobs (e.g., GDPR purge, scheduler bootstrap).

    Sets `app.allow_audit_purge` when explicitly requested. Use with care:
    callers must enforce their own authorization.
    """
    factory = session_factory()
    async with factory() as session:
        async with session.begin():
            if allow_audit_purge:
                await session.execute(
                    text("SELECT set_config('app.allow_audit_purge', 'true', true)")
                )
            yield session
