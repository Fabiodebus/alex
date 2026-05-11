"""Outbound clients that hand off :class:`DeliveryAttempt` payloads
to the messaging surfaces.

Two production targets in v1:

* **Slack** — :class:`HttpMessagingDeliveryClient` POSTs to the
  slack-bot's ``/deliver`` endpoint (signed with the shared webhook
  secret; same wire contract as the CRM fetch/write clients).
* **Teams** — same shape, different URL.

Tests use :class:`StubMessagingDeliveryClient` which records every
attempt in-memory so assertions can verify the router behaviour
without spinning up the messaging service.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

import httpx
import structlog

from ..config import Settings, get_settings
from ..schemas import DeliveryAttempt, DeliveryChannel
from .pipedream_client import _sign

log = structlog.get_logger(__name__)


class MessagingDeliveryError(RuntimeError):
    def __init__(self, message: str, *, status: int, body: object | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


@runtime_checkable
class MessagingDeliveryClient(Protocol):
    name: str

    async def deliver(
        self,
        *,
        channel: DeliveryChannel,
        attempt: DeliveryAttempt,
    ) -> dict[str, object]: ...


class StubMessagingDeliveryClient:
    """In-memory client. Records calls; raises if configured to.

    Tests configure failure with :meth:`fail_next` to exercise the
    DeliveryTracker's failure path. Default behaviour is to return a
    ``delivered`` response."""

    name = "stub"

    def __init__(self) -> None:
        self.calls: list[tuple[DeliveryChannel, DeliveryAttempt]] = []
        self._next_failure: MessagingDeliveryError | None = None

    def fail_next(self, *, status: int = 502, message: str = "stubbed failure") -> None:
        self._next_failure = MessagingDeliveryError(message, status=status)

    async def deliver(
        self,
        *,
        channel: DeliveryChannel,
        attempt: DeliveryAttempt,
    ) -> dict[str, object]:
        self.calls.append((channel, attempt))
        if self._next_failure is not None:
            err, self._next_failure = self._next_failure, None
            raise err
        return {
            "backend": "stub",
            "channel": channel.value,
            "output_id": attempt.output_id,
            "delivered_at": datetime.now(timezone.utc).isoformat(),
        }


class HttpMessagingDeliveryClient:
    """Signed HTTP delivery to the messaging surfaces."""

    name = "http"

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._http = client or httpx.AsyncClient(timeout=15.0)
        self._owned_http = client is None

    async def close(self) -> None:
        if self._owned_http:
            await self._http.aclose()

    async def deliver(
        self,
        *,
        channel: DeliveryChannel,
        attempt: DeliveryAttempt,
    ) -> dict[str, object]:
        url = self._url_for(channel)
        if not url:
            raise MessagingDeliveryError(
                f"No URL configured for channel {channel.value}", status=0
            )
        payload = attempt.model_dump(mode="json")
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        headers = {
            "Content-Type": "application/json",
            "X-Tenant-Id": str(attempt.tenant_id),
            "X-Alex-Timestamp": timestamp,
        }
        if self._settings.alex_webhook_secret:
            headers["X-Alex-Signature"] = _sign(
                self._settings.alex_webhook_secret, timestamp, body.decode("utf-8")
            )
        response = await self._http.post(url, content=body, headers=headers)
        try:
            parsed = response.json()
        except ValueError:
            parsed = None
        if response.status_code >= 400:
            raise MessagingDeliveryError(
                f"{channel.value} delivery returned {response.status_code}",
                status=response.status_code,
                body=parsed,
            )
        if isinstance(parsed, dict):
            return parsed
        return {"raw": parsed}

    def _url_for(self, channel: DeliveryChannel) -> str:
        if channel is DeliveryChannel.SLACK:
            return self._settings.alex_slack_deliver_url
        if channel is DeliveryChannel.TEAMS:
            return self._settings.alex_teams_deliver_url
        return ""


def build_default_messaging_delivery_client(
    settings: Settings | None = None,
) -> MessagingDeliveryClient:
    s = settings or get_settings()
    if s.messaging_delivery_provider == "http":
        log.info("messaging_delivery_client.selected", provider="http")
        return HttpMessagingDeliveryClient(s)
    log.warning("messaging_delivery_client.selected", provider="stub")
    return StubMessagingDeliveryClient()
