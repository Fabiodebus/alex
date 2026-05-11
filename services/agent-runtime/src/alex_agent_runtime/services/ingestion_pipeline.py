"""IngestionPipeline — initial background backfill for a newly-onboarded rep.

Triggered after the OAuth flow confirms CRM + email connections (the
:class:`alex_agent_runtime.routes.connections.post_status` callback can
chain into this, though the WO leaves the conversational onboarding
flow that orchestrates the trigger to a separate WO). Steps:

1. Pull a batch of recent records via :class:`IngestionProvider`.
2. Route each record into the right memory tier via :class:`MemoryStore`.
3. Publish ``ingestion.complete`` so the onboarding flow can fire the
   first proactive output.

The pipeline is *idempotent* by virtue of `MemoryStore.write`'s
content-hash dedup — re-running the same backfill produces no new rows.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import structlog

from ..config import Settings, get_settings
from ..schemas import (
    IngestedRecord,
    IngestedRecordKind,
    IngestionBatch,
    IngestionComplete,
    IngestionResult,
    MemoryTier,
    MemoryWrite,
)
from .event_bus import EventBus
from .ingestion_provider import IngestionProvider
from .memory_store import MemoryStore

log = structlog.get_logger(__name__)


# Records that don't have a deal/account anchor yet fall back to org tier
# so the rep at least has some baseline context. Feature WOs can later
# refine this mapping (e.g. resolve account_external_id to an Account row).
_DEFAULT_TIER_BY_KIND: dict[IngestedRecordKind, MemoryTier] = {
    IngestedRecordKind.CRM_OPPORTUNITY: MemoryTier.DEAL,
    IngestedRecordKind.CRM_CONTACT: MemoryTier.ACCOUNT,
    IngestedRecordKind.EMAIL_THREAD: MemoryTier.DEAL,
    IngestedRecordKind.CALL_RECORDING: MemoryTier.DEAL,
}


class IngestionPipeline:
    def __init__(
        self,
        *,
        provider: IngestionProvider,
        memory_store: MemoryStore,
        event_bus: EventBus,
        settings: Settings | None = None,
    ) -> None:
        self._provider = provider
        self._memory_store = memory_store
        self._event_bus = event_bus
        self._settings = settings or get_settings()

    async def run(
        self,
        *,
        tenant_id: UUID,
        rep_id: UUID,
        since_days: int | None = None,
    ) -> IngestionResult:
        started_at = datetime.now(timezone.utc)
        days = since_days or self._settings.ingestion_default_since_days

        log.info("ingestion_pipeline.start", tenant_id=str(tenant_id), rep_id=str(rep_id), since_days=days)

        try:
            batch = await self._provider.fetch_batch(
                tenant_id=tenant_id, rep_id=rep_id, since_days=days
            )
        except Exception as exc:
            log.exception("ingestion_pipeline.fetch_failed", error=str(exc))
            result = IngestionResult(
                tenant_id=tenant_id,
                rep_id=rep_id,
                records_processed=0,
                memories_written=0,
                memories_deduplicated=0,
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
                errors=[f"provider_fetch_failed: {exc}"],
            )
            return result

        capped_batch = self._cap_recordings(batch)
        memories_written = 0
        memories_deduplicated = 0
        errors: list[str] = []

        for record in capped_batch.records:
            try:
                written, was_new = await self._route_record(
                    tenant_id=tenant_id, rep_id=rep_id, record=record
                )
                if written:
                    if was_new:
                        memories_written += 1
                    else:
                        memories_deduplicated += 1
            except Exception as exc:  # surface per-record errors but keep going
                log.warning(
                    "ingestion_pipeline.record_failed",
                    external_id=record.external_id,
                    kind=record.kind.value,
                    error=str(exc),
                )
                errors.append(f"{record.kind.value}:{record.external_id}: {exc}")

        result = IngestionResult(
            tenant_id=tenant_id,
            rep_id=rep_id,
            records_processed=len(capped_batch.records),
            memories_written=memories_written,
            memories_deduplicated=memories_deduplicated,
            started_at=started_at,
            completed_at=datetime.now(timezone.utc),
            errors=errors,
        )
        await self._event_bus.publish(
            "ingestion.complete",
            IngestionComplete(tenant_id=tenant_id, rep_id=rep_id, result=result),
        )
        log.info(
            "ingestion_pipeline.done",
            tenant_id=str(tenant_id),
            rep_id=str(rep_id),
            written=memories_written,
            deduplicated=memories_deduplicated,
            errors=len(errors),
        )
        return result

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _cap_recordings(self, batch: IngestionBatch) -> IngestionBatch:
        cap = self._settings.ingestion_recording_cap
        if cap <= 0:
            return batch
        recordings = [r for r in batch.records if r.kind is IngestedRecordKind.CALL_RECORDING]
        if len(recordings) <= cap:
            return batch
        # Keep the `cap` most recent recordings by occurred_at descending;
        # drop the rest. Non-recording records pass through unchanged.
        recordings_sorted = sorted(
            recordings,
            key=lambda r: r.occurred_at or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        kept_recording_ids = {r.external_id for r in recordings_sorted[:cap]}
        kept_records = [
            r
            for r in batch.records
            if r.kind is not IngestedRecordKind.CALL_RECORDING
            or r.external_id in kept_recording_ids
        ]
        return IngestionBatch(
            tenant_id=batch.tenant_id,
            rep_id=batch.rep_id,
            fetched_at=batch.fetched_at,
            records=kept_records,
        )

    async def _route_record(
        self,
        *,
        tenant_id: UUID,
        rep_id: UUID,
        record: IngestedRecord,
    ) -> tuple[bool, bool]:
        """Route a single record into MemoryStore. Returns (written, was_new).

        was_new is False when the write returned the existing row via dedup.
        """
        tier = _DEFAULT_TIER_BY_KIND[record.kind]
        owner_id = self._resolve_owner_id(tier=tier, rep_id=rep_id, record=record)
        if owner_id is None and tier in (MemoryTier.DEAL, MemoryTier.ACCOUNT):
            # No deal/account anchor available yet; demote to org tier as
            # a transitional bucket so the rep still has *some* context.
            tier = MemoryTier.ORG
            owner_id = None

        attributes = {
            **record.attributes,
            "ingested_external_id": record.external_id,
            "ingested_kind": record.kind.value,
        }
        result = await self._memory_store.write_with_status(
            tenant_id=tenant_id,
            write=MemoryWrite(
                tier=tier,
                owner_id=owner_id,
                kind=record.kind.value,
                content=record.content,
                attributes=attributes,
                source_uri=record.attributes.get("source_uri"),
            ),
            index_embeddings=True,
        )
        return True, result.inserted

    @staticmethod
    def _resolve_owner_id(
        *, tier: MemoryTier, rep_id: UUID, record: IngestedRecord
    ) -> UUID | None:
        """Map a record to an owner_id when possible.

        At ingestion time we don't yet have local Account / Deal rows for
        every external identifier, so the scaffold returns ``None`` for
        deal/account tiers — the caller demotes to org tier. Feature WOs
        will plug in resolvers that turn ``account_external_id`` etc. into
        real ``accounts.id`` UUIDs.
        """
        if tier is MemoryTier.REP:
            return rep_id
        return None
