"""Unit tests for the EmailSendClient stub."""
from __future__ import annotations

from uuid import uuid4

import pytest

from alex_agent_runtime.schemas import EmailSendRequest
from alex_agent_runtime.services.email_send_client import StubEmailSendClient


@pytest.mark.asyncio
async def test_stub_records_and_returns_success():
    client = StubEmailSendClient()
    req = EmailSendRequest(
        tenant_id=uuid4(),
        rep_id=uuid4(),
        to=["buyer@acme.example"],
        subject="Quick follow-up",
        body="Hi Sam,\n\nNext step is X.\n\nBest regards",
        idempotency_key="task:abc",
    )
    result = await client.send(req)
    assert result.delivered is True
    assert result.provider == "stub"
    assert client.calls and client.calls[0].subject == "Quick follow-up"


@pytest.mark.asyncio
async def test_stub_assigns_synthetic_message_id():
    client = StubEmailSendClient()
    result = await client.send(
        EmailSendRequest(
            tenant_id=uuid4(),
            rep_id=uuid4(),
            to=["buyer@acme.example"],
            subject="s",
            body="b",
            idempotency_key="task:xyz",
        )
    )
    assert result.provider_message_id == "stub-task:xyz"
