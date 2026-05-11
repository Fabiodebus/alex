from __future__ import annotations

import pytest

from alex_agent_runtime.config import Settings
from alex_agent_runtime.services.agent_backend import (
    ClaudeAgentBackend,
    StubAgentBackend,
    build_default_backend,
)


def test_build_default_backend_returns_stub_without_api_key():
    settings = Settings(anthropic_api_key="")
    backend = build_default_backend(settings)
    assert isinstance(backend, StubAgentBackend)
    assert backend.name == "stub"


def test_build_default_backend_returns_claude_when_key_present():
    settings = Settings(anthropic_api_key="sk-ant-test")
    backend = build_default_backend(settings)
    assert isinstance(backend, ClaudeAgentBackend)
    assert backend.name == "claude-agent-sdk"


@pytest.mark.asyncio
async def test_stub_backend_returns_deterministic_response():
    backend = StubAgentBackend()
    response = await backend.run("hello")
    assert response.backend == "stub"
    assert "hello" in response.text
