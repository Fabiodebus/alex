"""Async-safe tenant context.

Every request the FastAPI app handles binds the tenant id from the
``X-Tenant-Id`` header into this contextvar. Background jobs and the
scheduler must set the contextvar manually before performing tenant-scoped
work; ``transactional_session()`` raises if no tenant is bound when it is
asked to start a transaction.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from uuid import UUID

_current_tenant: ContextVar[UUID | None] = ContextVar("alex_current_tenant", default=None)


class MissingTenantError(RuntimeError):
    """Raised when tenant-scoped code runs without a bound tenant id."""


def set_current_tenant(tenant_id: UUID) -> None:
    _current_tenant.set(tenant_id)


def current_tenant() -> UUID:
    value = _current_tenant.get()
    if value is None:
        raise MissingTenantError(
            "No tenant bound to the current context. "
            "Inbound requests must carry a tenant header; jobs must call "
            "set_current_tenant() before performing tenant-scoped work."
        )
    return value


def current_tenant_or_none() -> UUID | None:
    return _current_tenant.get()


@contextmanager
def tenant_scope(tenant_id: UUID):
    """Bind a tenant for the lifetime of a `with` block (jobs, tests)."""
    token = _current_tenant.set(tenant_id)
    try:
        yield
    finally:
        _current_tenant.reset(token)
