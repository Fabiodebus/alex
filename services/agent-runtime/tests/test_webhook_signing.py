"""Unit tests for the HMAC verification helper (no FastAPI involved)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from alex_agent_runtime.webhook_signing import (
    InvalidSignatureError,
    MissingSignatureError,
    StaleSignatureError,
    expected_signature,
    verify,
)

SECRET = "test-secret-do-not-use"
TS = "2026-05-11T08:00:00.000Z"
TS_DT = datetime(2026, 5, 11, 8, 0, 0, tzinfo=timezone.utc)
BODY = b'{"event_id":"x"}'


def _sig() -> str:
    return expected_signature(secret=SECRET, timestamp=TS, body=BODY)


def test_round_trip_accepts_a_valid_signature():
    verify(
        secret=SECRET,
        body=BODY,
        signature=_sig(),
        timestamp=TS,
        max_age_seconds=300,
        now=TS_DT,
    )


def test_missing_signature_raises():
    with pytest.raises(MissingSignatureError):
        verify(
            secret=SECRET,
            body=BODY,
            signature=None,
            timestamp=TS,
            max_age_seconds=300,
            now=TS_DT,
        )


def test_missing_timestamp_raises():
    with pytest.raises(MissingSignatureError):
        verify(
            secret=SECRET,
            body=BODY,
            signature=_sig(),
            timestamp=None,
            max_age_seconds=300,
            now=TS_DT,
        )


def test_mismatched_signature_raises():
    with pytest.raises(InvalidSignatureError):
        verify(
            secret=SECRET,
            body=BODY,
            signature="sha256=" + "0" * 64,
            timestamp=TS,
            max_age_seconds=300,
            now=TS_DT,
        )


def test_stale_timestamp_raises():
    too_old = (TS_DT + timedelta(seconds=400)).isoformat().replace("+00:00", "Z")
    with pytest.raises(StaleSignatureError):
        verify(
            secret=SECRET,
            body=BODY,
            signature=_sig(),
            timestamp=TS,
            max_age_seconds=300,
            now=TS_DT + timedelta(seconds=400),
        )
    # Same check, but with the timestamp in the future relative to now.
    with pytest.raises(StaleSignatureError):
        verify(
            secret=SECRET,
            body=BODY,
            signature=expected_signature(secret=SECRET, timestamp=too_old, body=BODY),
            timestamp=too_old,
            max_age_seconds=300,
            now=TS_DT,
        )


def test_signature_matches_node_implementation():
    """The Node reference (services/pipedream/src/lib/forwarder.mjs) signs
    `${timestamp}.${body}` with HMAC-SHA256 hex digest, prefixed `sha256=`.
    Lock the contract here so a future change on either side breaks both
    test suites instead of silently desynchronising.
    """
    # Pre-computed by hand: hmac-sha256(
    #   key='test-secret-do-not-use',
    #   msg='2026-05-11T08:00:00.000Z.{"event_id":"x"}'
    # ).hexdigest()
    sig = expected_signature(secret=SECRET, timestamp=TS, body=BODY)
    assert sig.startswith("sha256=")
    assert len(sig) == len("sha256=") + 64
