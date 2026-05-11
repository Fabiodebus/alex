"""Inbound request middleware: signature verification + tenant resolution."""
from __future__ import annotations

from uuid import UUID

import structlog
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .config import get_settings
from .tenant_context import set_current_tenant
from .webhook_signing import (
    InvalidSignatureError,
    MissingSignatureError,
    SignatureError,
    StaleSignatureError,
    verify,
)

log = structlog.get_logger(__name__)

# Routes that don't require tenant context (health checks, metrics).
_TENANT_FREE_PATHS: frozenset[str] = frozenset({"/healthz", "/readyz", "/metrics"})

# Routes that require a signed body when the secret is configured. Health
# probes and OpenAPI docs are excluded so they can be hit unauthenticated.
_SIGNED_PATH_PREFIXES: tuple[str, ...] = ("/events", "/callbacks")


class WebhookSignatureMiddleware(BaseHTTPMiddleware):
    """Verify the HMAC signature on inbound webhook posts.

    When ``ALEX_WEBHOOK_SECRET`` is empty (default in dev), the middleware
    is a no-op so curl-driven smoke tests work without ceremony. When
    set, the middleware rejects unsigned requests, signature mismatches,
    and timestamps outside the freshness window.
    """

    async def dispatch(self, request: Request, call_next):
        settings = get_settings()
        if not settings.webhook_signing_enforced:
            return await call_next(request)
        if not any(request.url.path.startswith(p) for p in _SIGNED_PATH_PREFIXES):
            return await call_next(request)

        body = await request.body()
        signature = request.headers.get(settings.webhook_signature_header)
        timestamp = request.headers.get(settings.webhook_timestamp_header)
        try:
            verify(
                secret=settings.alex_webhook_secret,
                body=body,
                signature=signature,
                timestamp=timestamp,
                max_age_seconds=settings.webhook_signature_max_age_seconds,
            )
        except MissingSignatureError as exc:
            return JSONResponse(status_code=401, content={"error": "missing_signature", "detail": str(exc)})
        except StaleSignatureError as exc:
            return JSONResponse(status_code=401, content={"error": "stale_signature", "detail": str(exc)})
        except InvalidSignatureError as exc:
            return JSONResponse(status_code=401, content={"error": "invalid_signature", "detail": str(exc)})
        except SignatureError as exc:
            return JSONResponse(status_code=401, content={"error": "signature_error", "detail": str(exc)})

        # Starlette's BaseHTTPMiddleware consumes the body via request.body()
        # and re-streams it to the downstream app via its own buffer, so
        # the route handler still receives the original payload — no manual
        # receive-callable rewrap needed here.
        return await call_next(request)


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
