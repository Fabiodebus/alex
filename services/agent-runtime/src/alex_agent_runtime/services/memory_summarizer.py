"""MemorySummarizer — distill recent raw memories into prompt-injectable summaries.

For each (tier, owner_id) the summarizer reads the N most recent
non-summary memory rows, feeds them to the AgentBackend with a
deterministic system prompt, and writes the result back as a memory
row with ``kind="summary.<tier>"``. Subsequent ``MemoryStore.retrieve``
calls can either include the summary row alongside raw memories or
prefer it over the underlying detail — that policy lives in
the feature workflows, not here.

A summary row carries enough metadata in ``attributes`` for callers to
decide freshness:

* ``summarized_at`` — ISO timestamp
* ``source_ids`` — list of memory ids the summary covers
* ``last_source_at`` — newest ``updated_at`` of the sources
* ``staleness_threshold_seconds`` — what the writer considered fresh
* ``model_backend`` — which AgentBackend produced it
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import structlog

from ..config import Settings, get_settings
from ..schemas import (
    MemoryRecord,
    MemorySummaryUpdated,
    MemoryTier,
    MemoryWrite,
)
from .agent_backend import AgentBackend
from .event_bus import EventBus
from .memory_store import MemoryStore, MemoryStoreError

log = structlog.get_logger(__name__)


_SUMMARY_KIND_PREFIX = "summary."


def _summary_kind(tier: MemoryTier) -> str:
    return f"{_SUMMARY_KIND_PREFIX}{tier.value}"


def is_summary_kind(kind: str) -> bool:
    return kind.startswith(_SUMMARY_KIND_PREFIX)


class SummarizerError(RuntimeError):
    pass


class MemorySummarizer:
    """Tier-aware summarizer with on-demand and scheduled entry points."""

    def __init__(
        self,
        *,
        memory_store: MemoryStore,
        agent_backend: AgentBackend,
        event_bus: EventBus,
        settings: Settings | None = None,
    ) -> None:
        self._memory_store = memory_store
        self._agent_backend = agent_backend
        self._event_bus = event_bus
        self._settings = settings or get_settings()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def summarize_for(
        self,
        *,
        tenant_id: UUID,
        tier: MemoryTier,
        owner_id: UUID | None = None,
        force: bool = False,
    ) -> MemoryRecord | None:
        """(Re)summarize a single tier scope.

        Returns the new summary memory row, or ``None`` if there was no
        source content to summarize. Skips re-summarization when an
        existing summary is still within the staleness threshold (unless
        ``force=True``).
        """
        sources = await self._fetch_sources(tenant_id, tier, owner_id)
        if not sources:
            log.info(
                "memory_summarizer.no_sources",
                tier=tier.value,
                owner_id=str(owner_id) if owner_id else None,
            )
            return None

        if not force:
            existing = await self._latest_existing_summary(tenant_id, tier, owner_id)
            if existing is not None and not self._is_stale(existing, sources):
                log.info(
                    "memory_summarizer.fresh",
                    tier=tier.value,
                    owner_id=str(owner_id) if owner_id else None,
                    summary_id=str(existing.id),
                )
                return existing

        prompt, system_prompt = self._build_prompt(tier=tier, sources=sources)
        response = await self._agent_backend.run(
            prompt=prompt, system_prompt=system_prompt, max_turns=1
        )
        if not response.text.strip():
            raise SummarizerError("AgentBackend returned empty summary text")

        last_source_at = max((s.updated_at for s in sources), default=datetime.now(timezone.utc))
        attributes: dict[str, Any] = {
            "summarized_at": datetime.now(timezone.utc).isoformat(),
            "source_ids": [str(s.id) for s in sources],
            "last_source_at": last_source_at.isoformat(),
            "staleness_threshold_seconds": self._settings.summary_staleness_seconds,
            "model_backend": response.backend,
            "is_summary": True,
        }
        record = await self._memory_store.write(
            tenant_id=tenant_id,
            write=MemoryWrite(
                tier=tier,
                owner_id=owner_id,
                kind=_summary_kind(tier),
                content=response.text.strip(),
                attributes=attributes,
            ),
            index_embeddings=True,
        )
        await self._event_bus.publish(
            "memory.summary_updated",
            MemorySummaryUpdated(
                tenant_id=tenant_id,
                tier=tier,
                owner_id=owner_id,
                summary_memory_id=record.id,
                sources_summarized=len(sources),
            ),
        )
        log.info(
            "memory_summarizer.written",
            tier=tier.value,
            owner_id=str(owner_id) if owner_id else None,
            summary_id=str(record.id),
            sources=len(sources),
            backend=response.backend,
        )
        return record

    async def is_summary_stale(
        self,
        *,
        tenant_id: UUID,
        tier: MemoryTier,
        owner_id: UUID | None,
    ) -> bool:
        """Return True iff no fresh summary exists for the given scope."""
        existing = await self._latest_existing_summary(tenant_id, tier, owner_id)
        if existing is None:
            return True
        sources = await self._fetch_sources(tenant_id, tier, owner_id)
        if not sources:
            return False
        return self._is_stale(existing, sources)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    async def _fetch_sources(
        self,
        tenant_id: UUID,
        tier: MemoryTier,
        owner_id: UUID | None,
    ) -> list[MemoryRecord]:
        """Recent non-summary memory rows for a given scope."""
        rows = await self._memory_store.list_recent(
            tenant_id=tenant_id,
            tier=tier,
            owner_id=owner_id,
            limit=self._settings.summary_source_limit + 5,  # over-fetch to allow filtering
        )
        sources = [r for r in rows if not is_summary_kind(r.kind)]
        return sources[: self._settings.summary_source_limit]

    async def _latest_existing_summary(
        self,
        tenant_id: UUID,
        tier: MemoryTier,
        owner_id: UUID | None,
    ) -> MemoryRecord | None:
        rows = await self._memory_store.list_recent(
            tenant_id=tenant_id,
            tier=tier,
            owner_id=owner_id,
            kinds_filter=[_summary_kind(tier)],
            limit=1,
        )
        return rows[0] if rows else None

    def _is_stale(self, summary: MemoryRecord, sources: list[MemoryRecord]) -> bool:
        threshold = self._settings.summary_staleness_seconds
        summarized_at = _parse_iso(summary.attributes.get("summarized_at"))
        if summarized_at is None:
            return True
        newest_source = max((s.updated_at for s in sources), default=summarized_at)
        # Stale if a source has landed since the summary was produced AND
        # the gap exceeds the configured tolerance.
        return (newest_source - summarized_at).total_seconds() > threshold

    def _build_prompt(
        self,
        *,
        tier: MemoryTier,
        sources: list[MemoryRecord],
    ) -> tuple[str, str]:
        joined = "\n\n".join(
            f"[{i + 1}] kind={s.kind} updated_at={s.updated_at.isoformat()}\n{s.content}"
            for i, s in enumerate(sources)
        )
        system_prompt = (
            "You are Alex's memory summarizer. Given a set of raw memory "
            "rows for a single rep/deal/account/org, produce a compact, "
            "prompt-injectable summary that a downstream agent can use as "
            "context. Be factual, terse, and prefer bullet points over prose. "
            "Do not include speculation, opinions, or sales tactics — only "
            "what the source content supports."
        )
        prompt = (
            f"Tier: {tier.value}\n"
            f"Source rows ({len(sources)}):\n\n{joined}\n\n"
            "Produce the summary now."
        )
        return prompt, system_prompt


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
