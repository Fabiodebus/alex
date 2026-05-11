"""AlexSignatureMiddleware — verifies the HMAC on /deliver."""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .config import get_settings
from .services.signing import (
    InvalidSignatureError,
    MissingSignatureError,
    SignatureError,
    StaleSignatureError,
    verify,
)

_SIGNED_PATH_PREFIXES: tuple[str, ...] = ("/deliver",)


class AlexSignatureMiddleware(BaseHTTPMiddleware):
    """Reject `/deliver` requests that aren't signed with the shared secret.

    Slack-side inbound requests on `/slack/events` are verified by Slack
    Bolt's own signing-secret check; this middleware covers only the
    Agent Runtime → slack-bot push.
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
        return await call_next(request)
