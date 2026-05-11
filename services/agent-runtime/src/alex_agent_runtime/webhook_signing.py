"""HMAC-SHA256 webhook signature verification.

Mirrors the Node implementation in ``services/pipedream/src/lib/forwarder.mjs``.
The signed string is ``f"{timestamp}.{raw_body}"`` so leaking a signature
without the secret can't be replayed against a different timestamp.
"""
from __future__ import annotations

import hmac
from datetime import datetime, timedelta, timezone
from hashlib import sha256

SIGNATURE_PREFIX = "sha256="


class SignatureError(Exception):
    """Base class for signature verification failures."""


class MissingSignatureError(SignatureError):
    pass


class InvalidSignatureError(SignatureError):
    pass


class StaleSignatureError(SignatureError):
    pass


def expected_signature(*, secret: str, timestamp: str, body: bytes) -> str:
    if not secret:
        raise ValueError("secret must be non-empty")
    payload = f"{timestamp}.{body.decode('utf-8')}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), payload, sha256).hexdigest()
    return f"{SIGNATURE_PREFIX}{digest}"


def verify(
    *,
    secret: str,
    body: bytes,
    signature: str | None,
    timestamp: str | None,
    max_age_seconds: int,
    now: datetime | None = None,
) -> None:
    """Raise on any verification failure; return None on success."""
    if not signature or not timestamp:
        raise MissingSignatureError(
            "X-Alex-Signature and X-Alex-Timestamp headers are both required"
        )
    try:
        ts_dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidSignatureError(f"X-Alex-Timestamp is not ISO-8601: {timestamp}") from exc
    if ts_dt.tzinfo is None:
        ts_dt = ts_dt.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if abs((current - ts_dt).total_seconds()) > max_age_seconds:
        raise StaleSignatureError(
            f"X-Alex-Timestamp drift exceeds {max_age_seconds}s (got {timestamp})"
        )
    expected = expected_signature(secret=secret, timestamp=timestamp, body=body)
    if not hmac.compare_digest(expected, signature):
        raise InvalidSignatureError("signature mismatch")
