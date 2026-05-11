"""Inbound tenant resolution middleware."""
from __future__ import annotations

from uuid import UUID

import structlog
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .config import get_settings
from .tenant_context import set_current_tenant

log = structlog.get_logger(__name__)

# Routes that don't require tenant context (health checks, metrics).
_TENANT_FREE_PATHS: frozenset[str] = frozenset({"/healthz", "/readyz", "/metrics"})


class TenantHeaderMiddleware(BaseHTTPMiddleware):
    """Read the configured tenant header, parse it as UUID, bind to contextvar."""

    async def dispatch(self, request: Request, call_next):
        if request.url.path in _TENANT_FREE_PATHS:
            return await call_next(request)

        settings = get_settings()
        raw = request.headers.get(settings.tenant_header)
        if not raw:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "missing_tenant_header",
                    "header": settings.tenant_header,
                },
            )
        try:
            tenant_id = UUID(raw)
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_tenant_header", "value": raw},
            )

        set_current_tenant(tenant_id)
        log.debug("tenant_bound", tenant_id=str(tenant_id), path=request.url.path)
        return await call_next(request)
