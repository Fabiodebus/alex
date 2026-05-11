"""Shared scaffolding for the Phase 4 proactive composers.

Every composer (MeetingBriefComposer, FollowUpDraftComposer,
CRMNoteComposer) follows the same four-step lifecycle:

1. **gather_context** — pull the memory + CRM + transcript context
   the composer needs to write a useful output.
2. **build_prompt** — turn that context into a Claude-friendly prompt
   (system + user) the AgentBackend can run.
3. **call_backend** — invoke :class:`AgentBackend.run`. Stub backend
   echoes a deterministic placeholder; real Claude generates the
   actual content.
4. **wrap** — hand the AgentBackend response to the composer's own
   post-processor, which produces a structured payload (MeetingBrief
   / FollowUpDraft / CRMNoteReview) + the human-facing AgentOutput
   the rep sees. The composer opens a PendingTask via ApprovalGate
   (or, in the brief case, emits the AgentOutput directly when no
   approval is required).

Idempotency lives in :meth:`ProactiveComposer.compose`: a memory
marker (``kind='composer.<name>.composed'``, attributes carry the
correlation key) gates re-runs so a duplicate ``MeetingDetected`` /
``MeetingCompleted`` event won't produce a duplicate output.

The base class is intentionally a small piece — the heavy lifting
lives in the per-composer subclasses.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import structlog

from ..schemas import AgentResponse, MemoryTier, MemoryWrite
from .agent_backend import AgentBackend
from .memory_store import MemoryStore

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class ComposerPrompt:
    system_prompt: str | None
    user_prompt: str
    max_turns: int = 1


@dataclass(slots=True)
class ComposerContext:
    """Bundle the composer hands forward from gather_context.

    Subclasses extend this with feature-specific fields (e.g.
    ``transcript`` for follow-up + CRM-note composers)."""

    tenant_id: UUID
    rep_id: UUID
    correlation_key: str  # used for the idempotency marker
    metadata: dict[str, Any] = field(default_factory=dict)


class ComposerError(RuntimeError):
    pass


class ProactiveComposer(ABC):
    """Abstract base. Subclasses implement gather_context + build_prompt
    + wrap and call ``compose``."""

    name: str = "composer"

    def __init__(
        self,
        *,
        agent_backend: AgentBackend,
        memory_store: MemoryStore,
    ) -> None:
        self._agent_backend = agent_backend
        self._memory_store = memory_store

    # ------------------------------------------------------------------
    # Subclass contract
    # ------------------------------------------------------------------
    @abstractmethod
    async def gather_context(self, trigger_payload: Any) -> ComposerContext: ...

    @abstractmethod
    async def build_prompt(self, context: ComposerContext) -> ComposerPrompt: ...

    @abstractmethod
    async def wrap(
        self, *, context: ComposerContext, response: AgentResponse
    ) -> Any: ...

    # ------------------------------------------------------------------
    # Lifecycle entry point
    # ------------------------------------------------------------------
    async def compose(self, trigger_payload: Any) -> Any:
        """Run the full pipeline. Returns whatever :meth:`wrap` returns,
        or ``None`` when the idempotency marker says we've already
        composed for this correlation key."""
        context = await self.gather_context(trigger_payload)
        already = await self._idempotency_check(
            tenant_id=context.tenant_id, key=context.correlation_key
        )
        if already:
            log.info(
                "composer.skip.already_composed",
                composer=self.name,
                correlation_key=context.correlation_key,
            )
            return None

        prompt = await self.build_prompt(context)
        response = await self._agent_backend.run(
            prompt.user_prompt,
            system_prompt=prompt.system_prompt,
            max_turns=prompt.max_turns,
        )
        result = await self.wrap(context=context, response=response)
        await self._record_idempotency(
            tenant_id=context.tenant_id,
            rep_id=context.rep_id,
            correlation_key=context.correlation_key,
        )
        log.info(
            "composer.composed",
            composer=self.name,
            tenant_id=str(context.tenant_id),
            rep_id=str(context.rep_id),
            correlation_key=context.correlation_key,
            backend=response.backend,
        )
        return result

    # ------------------------------------------------------------------
    # Idempotency — same memory store, different kind per composer
    # ------------------------------------------------------------------
    @property
    def _marker_kind(self) -> str:
        return f"composer.{self.name}.composed"

    async def _idempotency_check(
        self, *, tenant_id: UUID, key: str
    ) -> bool:
        rows = await self._memory_store.list_recent(
            tenant_id=tenant_id,
            tier=MemoryTier.ORG,
            owner_id=None,
            kinds_filter=[self._marker_kind],
            limit=500,
        )
        for row in rows:
            if (row.attributes or {}).get("correlation_key") == key:
                return True
        return False

    async def _record_idempotency(
        self,
        *,
        tenant_id: UUID,
        rep_id: UUID,
        correlation_key: str,
    ) -> None:
        content = json.dumps(
            {
                "correlation_key": correlation_key,
                "rep_id": str(rep_id),
                "composed_at": datetime.now(timezone.utc).isoformat(),
            },
            separators=(",", ":"),
        )
        await self._memory_store.write_with_status(
            tenant_id=tenant_id,
            write=MemoryWrite(
                tier=MemoryTier.ORG,
                owner_id=None,
                kind=self._marker_kind,
                content=content,
                attributes={
                    "correlation_key": correlation_key,
                    "composer": self.name,
                    "rep_id": str(rep_id),
                },
                source_uri=f"composer://{self.name}/{correlation_key}",
            ),
            index_embeddings=False,
        )
