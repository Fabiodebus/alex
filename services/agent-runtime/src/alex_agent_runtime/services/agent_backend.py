"""`AgentBackend` abstraction wrapping the Claude Agent SDK.

ADR-001 of the Agent Runtime blueprint mandates that the AI backend live
behind a swappable interface so a self-hosted open-weights model can be
substituted for an enterprise tier without touching the memory or
integration layers. This module defines that interface plus two concrete
implementations:

* ``ClaudeAgentBackend`` — talks to Anthropic's Claude via the Claude
  Agent SDK. Picked up automatically when ``ANTHROPIC_API_KEY`` is set.
* ``StubAgentBackend`` — used when no API key is configured, so the
  service still boots in dev environments without keys. Returns a
  deterministic placeholder so tests can assert on shape.

Call sites must not depend on either concrete class — they should accept
``AgentBackend`` and let ``build_default_backend()`` decide.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

import structlog

from ..config import Settings, get_settings
from ..schemas import AgentResponse

log = structlog.get_logger(__name__)


@runtime_checkable
class AgentBackend(Protocol):
    """Minimal contract every backend must satisfy."""

    name: str

    async def run(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        max_turns: int = 1,
    ) -> AgentResponse: ...


class StubAgentBackend:
    """Fallback used when no real backend is configured."""

    name = "stub"

    async def run(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        max_turns: int = 1,
    ) -> AgentResponse:
        log.warning(
            "agent_backend.stub_called",
            reason="ANTHROPIC_API_KEY not set",
            prompt_chars=len(prompt),
        )
        return AgentResponse(
            text=f"[stub backend; configure ANTHROPIC_API_KEY] {prompt[:200]}",
            backend=self.name,
        )


class ClaudeAgentBackend:
    """Claude Agent SDK adapter.

    The SDK ships an async ``query()`` generator that streams typed
    messages back. We collect text blocks and the final ``ResultMessage``
    cost into a single ``AgentResponse``.
    """

    name = "claude-agent-sdk"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def run(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        max_turns: int = 1,
    ) -> AgentResponse:
        # Import lazily so the package imports even on systems that don't
        # have the SDK's runtime (Node + claude CLI) installed.
        from claude_agent_sdk import (  # type: ignore[import-not-found]
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            TextBlock,
            query,
        )

        env: dict[str, str] = {"ANTHROPIC_API_KEY": self._settings.anthropic_api_key}
        if self._settings.anthropic_base_url:
            env["ANTHROPIC_BASE_URL"] = self._settings.anthropic_base_url

        options = ClaudeAgentOptions(
            system_prompt=system_prompt,
            max_turns=max_turns,
            model=self._settings.anthropic_model,
            env=env,
        )

        chunks: list[str] = []
        cost: float | None = None
        raw: dict[str, object] = {}

        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)
            elif isinstance(message, ResultMessage):
                cost = getattr(message, "total_cost_usd", None)
                raw["result"] = getattr(message, "__dict__", {})

        return AgentResponse(
            text="".join(chunks),
            cost_usd=cost,
            raw=raw,
            backend=self.name,
        )


def build_default_backend(settings: Settings | None = None) -> AgentBackend:
    s = settings or get_settings()
    if s.has_real_agent_backend:
        log.info(
            "agent_backend.selected",
            backend="claude-agent-sdk",
            model=s.anthropic_model,
            eu_base_url=bool(s.anthropic_base_url),
        )
        return ClaudeAgentBackend(s)
    log.warning("agent_backend.selected", backend="stub")
    return StubAgentBackend()
