"""CRMReader — canonical CRM read layer.

Two entry points:

1. ``handle(event)`` — wired into :class:`FeatureRouter` for
   ``crm.activity_logged`` events (the kind the Pipedream inbound
   workflows emit for HubSpot / Salesforce / Pipedrive / Close record
   updates). Pulls the canonical record out of the event payload,
   normalises it through the appropriate adapter, and caches it via
   :class:`MemoryStore`.
2. ``fetch_record(...)`` — pull-based. Feature workflows call this when
   they need a specific record. Memory hit → return cached. Miss → fall
   back to the Pipedream ``crm_fetch`` workflow via
   :class:`CRMFetchClient`, normalise, cache, return.

Caching policy: in this WO every record lands in ``MemoryTier.ORG``
with ``crm_platform`` / ``crm_external_id`` / ``crm_kind`` in
``attributes`` so a future resolver can promote opportunities to
``DealMemory`` once local ``deals`` / ``accounts`` rows exist. The
canonical record itself is the memory row's ``content`` (JSON-encoded)
so feature workflows can read it without an extra DB hop.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import structlog

from ..schemas import (
    CRMFetchRequest,
    CRMPlatform,
    CRMRecord,
    CRMRecordKind,
    CRMSyncResult,
    IntegrationEvent,
    MemoryRecord,
    MemoryTier,
    MemoryWrite,
)
from .crm_adapters import get_adapter
from .crm_fetch_client import CRMFetchClient
from .memory_store import MemoryStore

log = structlog.get_logger(__name__)


_DEFAULT_RECORD_KIND_BY_EVENT_HINT: dict[str, CRMRecordKind] = {
    "deal": CRMRecordKind.OPPORTUNITY,
    "opportunity": CRMRecordKind.OPPORTUNITY,
    "contact": CRMRecordKind.CONTACT,
    "company": CRMRecordKind.ACCOUNT,
    "account": CRMRecordKind.ACCOUNT,
}


class CRMReaderError(RuntimeError):
    pass


class CRMReader:
    def __init__(
        self,
        *,
        memory_store: MemoryStore,
        fetch_client: CRMFetchClient,
    ) -> None:
        self._memory_store = memory_store
        self._fetch_client = fetch_client

    # ------------------------------------------------------------------
    # Event-driven entry point — wired into FeatureRouter on lifespan.
    # ------------------------------------------------------------------
    async def handle_data_sync(self, event: IntegrationEvent) -> CRMSyncResult | None:
        """Normalise + cache a single CRMDataSync event payload."""
        try:
            platform = CRMPlatform(event.source)
        except ValueError:
            log.info(
                "crm_reader.skip.unsupported_source",
                source=event.source,
                event_id=event.event_id,
            )
            return None

        kind = _infer_record_kind(event.payload)
        if kind is None:
            log.info(
                "crm_reader.skip.unknown_kind",
                source=event.source,
                event_id=event.event_id,
                payload_keys=list(event.payload.keys()),
            )
            return None

        raw_record = _extract_raw_record(event.payload)
        adapter = get_adapter(platform)
        record = adapter.normalize(raw=raw_record, kind=kind)
        result = await self._cache(tenant_id=_tenant_from_event(event), record=record)
        log.info(
            "crm_reader.cached",
            platform=platform.value,
            kind=kind.value,
            external_id=record.external_id,
            deduplicated=result.deduplicated,
        )
        return result

    # ------------------------------------------------------------------
    # Pull-based entry point.
    # ------------------------------------------------------------------
    async def fetch_record(
        self,
        *,
        tenant_id: UUID,
        platform: CRMPlatform,
        kind: CRMRecordKind,
        external_id: str,
        force_refresh: bool = False,
    ) -> CRMRecord | None:
        if not force_refresh:
            cached = await self._lookup_cached(
                tenant_id=tenant_id, platform=platform, kind=kind, external_id=external_id
            )
            if cached is not None:
                return cached

        raw = await self._fetch_client.fetch(
            CRMFetchRequest(
                tenant_id=tenant_id,
                platform=platform,
                kind=kind,
                external_id=external_id,
            )
        )
        if not raw:
            return None
        record = get_adapter(platform).normalize(raw=raw, kind=kind)
        await self._cache(tenant_id=tenant_id, record=record)
        return record

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    async def _cache(self, *, tenant_id: UUID, record: CRMRecord) -> CRMSyncResult:
        content = json.dumps(record.model_dump(mode="json"), separators=(",", ":"), default=str)
        write_result = await self._memory_store.write_with_status(
            tenant_id=tenant_id,
            write=MemoryWrite(
                tier=MemoryTier.ORG,
                owner_id=None,
                kind=f"crm.{record.kind.value}",
                content=content,
                attributes={
                    "crm_platform": record.platform.value,
                    "crm_kind": record.kind.value,
                    "crm_external_id": record.external_id,
                    "account_external_id": record.account_external_id,
                },
                source_uri=f"{record.platform.value}://{record.kind.value}/{record.external_id}",
            ),
            index_embeddings=True,
        )
        return CRMSyncResult(
            platform=record.platform,
            kind=record.kind,
            external_id=record.external_id,
            cached=True,
            deduplicated=not write_result.inserted,
        )

    async def _lookup_cached(
        self,
        *,
        tenant_id: UUID,
        platform: CRMPlatform,
        kind: CRMRecordKind,
        external_id: str,
    ) -> CRMRecord | None:
        rows = await self._memory_store.list_recent(
            tenant_id=tenant_id,
            tier=MemoryTier.ORG,
            owner_id=None,
            kinds_filter=[f"crm.{kind.value}"],
            limit=200,
        )
        for row in rows:
            attrs = row.attributes
            if (
                attrs.get("crm_platform") == platform.value
                and attrs.get("crm_external_id") == external_id
            ):
                return _record_from_memory_row(row)
        return None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _infer_record_kind(payload: dict[str, Any]) -> CRMRecordKind | None:
    """Inbound payloads from the Pipedream normalizer carry a
    ``subscription_type`` like ``contact.propertyChange`` or
    ``deal.creation``; the first segment is the platform's object type.
    Fall back to ``record_type`` for richer payloads."""
    candidates: list[str] = []
    sub = payload.get("subscription_type") or payload.get("subscriptionType")
    if isinstance(sub, str) and "." in sub:
        candidates.append(sub.split(".", 1)[0].lower())
    for key in ("record_type", "object_type", "type"):
        value = payload.get(key)
        if isinstance(value, str):
            candidates.append(value.lower())
    for hint in candidates:
        if hint in _DEFAULT_RECORD_KIND_BY_EVENT_HINT:
            return _DEFAULT_RECORD_KIND_BY_EVENT_HINT[hint]
    return None


def _extract_raw_record(payload: dict[str, Any]) -> dict[str, Any]:
    """Pull the inner raw record out of the event payload.

    The Pipedream-side normalizer typically forwards the original CRM
    record under a top-level key (``record``/``record_data``/``object``).
    When the payload is itself the record (older shape), it passes
    through unchanged.
    """
    for key in ("record", "record_data", "object", "payload"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            return nested
    return payload


def _tenant_from_event(event: IntegrationEvent) -> UUID:
    """Pull the tenant uuid out of the event payload — it's plumbed in
    by the Pipedream inbound workflows alongside the HMAC headers."""
    payload_tenant = event.payload.get("tenant_id") if isinstance(event.payload, dict) else None
    if isinstance(payload_tenant, str):
        try:
            return UUID(payload_tenant)
        except ValueError as exc:
            raise CRMReaderError(f"invalid tenant_id in payload: {payload_tenant}") from exc
    raise CRMReaderError(
        "CRMDataSync event payload missing tenant_id; the Pipedream inbound "
        "workflow must include it before forwarding"
    )


def _record_from_memory_row(row: MemoryRecord) -> CRMRecord:
    return CRMRecord.model_validate(json.loads(row.content))
