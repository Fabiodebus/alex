"""HMAC helpers — verify inbound from Agent Runtime, sign outbound to it / Pipedream.

Wire contract matches services/agent-runtime/.../webhook_signing.py and
services/pipedream/src/lib/forwarder.mjs: signed payload is
``f"{timestamp}.{body}"``, signature header is ``sha256=<hex>``.
"""
from __future__ import annotations

import hmac
from datetime import datetime, timezone
from hashlib import sha256

SIGNATURE_PREFIX = "sha256="


class SignatureError(Exception):
    pass


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


def sign_outbound(*, secret: str, body: bytes, timestamp: str | None = None) -> tuple[str, str]:
    """Return (signature, timestamp) for an outbound POST."""
    ts = timestamp or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    sig = expected_signature(secret=secret, timestamp=ts, body=body)
    return sig, ts
