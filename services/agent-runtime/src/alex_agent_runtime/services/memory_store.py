"""MemoryStore — CRUD over the four memory tiers + semantic retrieval.

This is the single read/write entry point for all feature workflows.
Every call binds ``app.tenant_id`` via ``transactional_session`` so the
data layer's row-level security policies enforce tenant isolation. Rep
memory is isolated per rep by default; cross-rep sharing inside a tenant
requires ``tenant_config.key = 'org_share_rep_memories'`` set to truthy.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, get_settings
from ..db import transactional_session
from ..schemas import (
    MemoryContext,
    MemoryRecord,
    MemorySnippet,
    MemorySummary,
    MemoryTier,
    MemoryWrite,
)
from ..tenant_context import tenant_scope
from .embedding_client import EmbeddingClient
from .embedding_indexer import EmbeddingIndexer, _vector_literal

log = structlog.get_logger(__name__)


# Per-tier table metadata.
_TIER_TABLES: dict[MemoryTier, dict[str, str]] = {
    MemoryTier.REP: {
        "memory_table": "rep_memories",
        "embedding_table": "rep_memory_embeddings",
        "owner_column": "rep_id",
    },
    MemoryTier.DEAL: {
        "memory_table": "deal_memories",
        "embedding_table": "deal_memory_embeddings",
        "owner_column": "deal_id",
    },
    MemoryTier.ACCOUNT: {
        "memory_table": "account_memories",
        "embedding_table": "account_memory_embeddings",
        "owner_column": "account_id",
    },
    MemoryTier.ORG: {
        "memory_table": "org_memories",
        "embedding_table": "org_memory_embeddings",
        "owner_column": None,
    },
}

_OWNER_REQUIRED_TIERS: frozenset[MemoryTier] = frozenset(
    {MemoryTier.REP, MemoryTier.DEAL, MemoryTier.ACCOUNT}
)


class MemoryStoreError(RuntimeError):
    pass


@dataclass(slots=True, frozen=True)
class WriteResult:
    """Returned by :meth:`MemoryStore.write_with_status`.

    ``inserted=True`` means a brand-new row was created; ``False``
    means the unique-index conflict path fired and ``record`` is the
    pre-existing row. Both cases still trigger embedding indexing
    (idempotent by chunk_text).
    """

    record: "MemoryRecord"
    inserted: bool


def _content_hash(kind: str, content: str) -> str:
    """Hash includes ``kind`` so the same physical text used for different
    purposes (e.g. as ``voice_sample`` vs ``interaction_note``) stays as
    two distinct memory rows. Without this distinction, callers using
    ``kinds_filter`` would see surprising results."""
    payload = f"{kind}\n{content.strip()}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _is_truthy_share_value(value: Any) -> bool:
    """Strict parser for the ``tenant_config.org_share_rep_memories``
    value. Plain ``bool(value)`` is wrong: a stored ``"false"`` string
    (in JSON or as a raw cell) would evaluate ``True``. We accept only
    the explicit truthy spellings."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "on", "enabled"}
    if isinstance(value, dict):
        return _is_truthy_share_value(value.get("enabled", False))
    return False


def _coerce_jsonb(value: dict[str, Any]) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


class MemoryStore:
    """Single read/write surface for all four memory tiers."""

    def __init__(
        self,
        *,
        embedding_client: EmbeddingClient,
        settings: Settings | None = None,
    ) -> None:
        self._embedding_client = embedding_client
        self._settings = settings or get_settings()
        self._indexer = EmbeddingIndexer(embedding_client)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    async def write(
        self,
        *,
        tenant_id: UUID,
        write: MemoryWrite,
        index_embeddings: bool = True,
    ) -> MemoryRecord:
        """Insert-or-dedup a memory row; return just the record.

        For callers that need to know whether the row was freshly
        inserted vs hit dedup (e.g. the IngestionPipeline reporting),
        use :meth:`write_with_status` instead.
        """
        result = await self.write_with_status(
            tenant_id=tenant_id, write=write, index_embeddings=index_embeddings
        )
        return result.record

    async def write_with_status(
        self,
        *,
        tenant_id: UUID,
        write: MemoryWrite,
        index_embeddings: bool = True,
    ) -> WriteResult:
        """As :meth:`write`, but returns a :class:`WriteResult` exposing
        whether the row was newly inserted (``inserted=True``) or the
        unique-index conflict path returned a pre-existing row.

        Dedup key: ``(tenant_id, owner_id, sha256(kind + content))`` —
        enforced both in the application and by the partial unique
        indexes from migration ``0005``. The ``INSERT ... ON CONFLICT DO
        NOTHING`` makes concurrent writers race-free.
        """
        meta = _TIER_TABLES[write.tier]
        memory_table = meta["memory_table"]
        owner_column = meta["owner_column"]
        if owner_column is None and write.owner_id is not None:
            raise MemoryStoreError(
                f"org-tier writes must not set owner_id (got {write.owner_id})"
            )
        if owner_column is not None and write.owner_id is None:
            raise MemoryStoreError(
                f"{write.tier.value}-tier writes require owner_id"
            )

        # Always overwrite any caller-supplied content_hash — the dedup
        # key must derive from the actual content + kind, not from
        # whatever attributes the caller passed in.
        attributes = dict(write.attributes)
        attributes["content_hash"] = _content_hash(write.kind, write.content)

        with tenant_scope(tenant_id):
            async with transactional_session() as session:
                row = await session.execute(
                    text(self._insert_sql(memory_table, owner_column)),
                    {
                        "owner_id": str(write.owner_id) if write.owner_id else None,
                        "kind": write.kind,
                        "content": write.content,
                        "attributes": _coerce_jsonb(attributes),
                        "source_uri": write.source_uri,
                    },
                )
                inserted = row.one_or_none()
                if inserted is None:
                    existing = await self._find_by_content_hash(
                        session,
                        memory_table=memory_table,
                        owner_column=owner_column,
                        owner_id=write.owner_id,
                        content_hash=attributes["content_hash"],
                    )
                    if existing is None:
                        raise MemoryStoreError(
                            f"INSERT conflict on {memory_table} but no matching row found"
                        )
                    log.info(
                        "memory_store.dedup",
                        tier=write.tier.value,
                        memory_id=str(existing.id),
                    )
                    if index_embeddings:
                        await self._indexer.index(
                            session=session,
                            tier=write.tier,
                            source_id=str(existing.id),
                            content=write.content,
                            chunk_chars=self._settings.embedding_chunk_chars,
                            overlap=self._settings.embedding_chunk_overlap,
                        )
                    return WriteResult(record=existing, inserted=False)

                record = self._row_to_record(write.tier, inserted, owner_column=owner_column)

                if index_embeddings:
                    await self._indexer.index(
                        session=session,
                        tier=write.tier,
                        source_id=str(record.id),
                        content=write.content,
                        chunk_chars=self._settings.embedding_chunk_chars,
                        overlap=self._settings.embedding_chunk_overlap,
                    )

        log.info(
            "memory_store.written",
            tier=write.tier.value,
            memory_id=str(record.id),
            owner_id=str(write.owner_id) if write.owner_id else None,
        )
        return WriteResult(record=record, inserted=True)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    async def list_recent(
        self,
        *,
        tenant_id: UUID,
        tier: MemoryTier,
        owner_id: UUID | None = None,
        limit: int = 20,
        kinds_filter: Sequence[str] | None = None,
    ) -> list[MemoryRecord]:
        """Most-recent memory rows for the given scope.

        Rep / deal / account tiers require an explicit ``owner_id`` —
        omitting it would silently expose every owner's memory inside
        the tenant. Callers that genuinely want a cross-owner scan must
        do it through ``retrieve()`` with the appropriate
        ``MemoryContext``.
        """
        if tier in _OWNER_REQUIRED_TIERS and owner_id is None:
            raise MemoryStoreError(
                f"{tier.value}-tier list_recent requires owner_id"
            )

        meta = _TIER_TABLES[tier]
        memory_table = meta["memory_table"]
        owner_column = meta["owner_column"]
        sql, params = self._select_recent_sql(
            memory_table=memory_table,
            owner_column=owner_column,
            owner_id=owner_id,
            kinds_filter=kinds_filter,
            limit=limit,
        )
        with tenant_scope(tenant_id):
            async with transactional_session() as session:
                rows = await session.execute(text(sql), params)
                return [self._row_to_record(tier, r, owner_column=owner_column) for r in rows]

    async def retrieve(self, context: MemoryContext) -> MemorySummary:
        share_rep_memories_across_org = await self._is_org_sharing_enabled(context.tenant_id)
        summary = MemorySummary(
            tenant_id=context.tenant_id,
            rep_id=context.rep_id,
            deal_id=context.deal_id,
            account_id=context.account_id,
        )

        # Compute the query vector at most once; reuse across tiers.
        query_vector: list[float] | None = None
        if context.query_text:
            embeddings = await self._embedding_client.embed([context.query_text])
            query_vector = embeddings[0]

        for tier in context.tiers:
            owner_id = self._owner_for_tier(context, tier)
            # Rep tier under org sharing: drop the per-rep filter so every
            # rep's memory inside the tenant is in scope.
            if tier is MemoryTier.REP and share_rep_memories_across_org:
                if context.rep_id is None:
                    log.warning(
                        "memory_store.retrieve.org_share_without_rep",
                        tenant_id=str(context.tenant_id),
                    )
                owner_id = None
            # Deal / account tiers require an explicit id from the context;
            # there's no "all deals" view at this layer.
            if owner_id is None and tier in (MemoryTier.DEAL, MemoryTier.ACCOUNT):
                continue
            # Rep tier without sharing AND without a rep_id in context yields
            # nothing visible — skip rather than fetch every rep's memory.
            if (
                tier is MemoryTier.REP
                and not share_rep_memories_across_org
                and context.rep_id is None
            ):
                continue
            isolate_rep = tier is MemoryTier.REP and not share_rep_memories_across_org
            snippets = await self._retrieve_tier(
                tenant_id=context.tenant_id,
                tier=tier,
                owner_id=owner_id,
                query_vector=query_vector,
                kinds_filter=context.kinds_filter,
                k=context.k_per_tier,
                isolate_rep=isolate_rep,
                rep_id=context.rep_id,
            )
            summary.by_tier[tier] = snippets
        return summary

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    async def _retrieve_tier(
        self,
        *,
        tenant_id: UUID,
        tier: MemoryTier,
        owner_id: UUID | None,
        query_vector: list[float] | None,
        kinds_filter: Sequence[str] | None,
        k: int,
        isolate_rep: bool,
        rep_id: UUID | None,
    ) -> list[MemorySnippet]:
        meta = _TIER_TABLES[tier]
        memory_table = meta["memory_table"]
        embedding_table = meta["embedding_table"]
        owner_column = meta["owner_column"]

        params: dict[str, Any] = {"k": k}
        where_clauses: list[str] = ["m.deleted_at IS NULL"]
        if owner_column is not None and owner_id is not None:
            where_clauses.append(f"m.{owner_column} = :owner_id")
            params["owner_id"] = str(owner_id)
        elif isolate_rep and tier is MemoryTier.REP and rep_id is not None:
            where_clauses.append("m.rep_id = :rep_id")
            params["rep_id"] = str(rep_id)
        if kinds_filter:
            where_clauses.append("m.kind = ANY(:kinds)")
            params["kinds"] = list(kinds_filter)

        where_sql = " AND ".join(where_clauses)

        with tenant_scope(tenant_id):
            async with transactional_session() as session:
                if query_vector is not None:
                    params["vec"] = _vector_literal(query_vector)
                    sql = (
                        f"SELECT m.id, m.tenant_id, "
                        f"{('m.' + owner_column) if owner_column else 'NULL::uuid'} AS owner_id, "
                        f"m.kind, m.content, m.attributes, m.source_uri, "
                        f"m.created_at, m.updated_at, "
                        f"e.chunk_text, "
                        f"1.0 - (e.content_vector <=> CAST(:vec AS vector)) AS similarity "
                        f"FROM {embedding_table} e "
                        f"JOIN {memory_table} m ON m.id = e.source_id "
                        f"WHERE {where_sql} "
                        f"ORDER BY e.content_vector <=> CAST(:vec AS vector) "
                        f"LIMIT :k"
                    )
                else:
                    sql = (
                        f"SELECT m.id, m.tenant_id, "
                        f"{('m.' + owner_column) if owner_column else 'NULL::uuid'} AS owner_id, "
                        f"m.kind, m.content, m.attributes, m.source_uri, "
                        f"m.created_at, m.updated_at, "
                        f"NULL::text AS chunk_text, NULL::float AS similarity "
                        f"FROM {memory_table} m "
                        f"WHERE {where_sql} "
                        f"ORDER BY m.updated_at DESC "
                        f"LIMIT :k"
                    )
                rows = await session.execute(text(sql), params)
                snippets: list[MemorySnippet] = []
                for r in rows:
                    record = MemoryRecord(
                        id=r.id,
                        tier=tier,
                        tenant_id=r.tenant_id,
                        owner_id=r.owner_id,
                        kind=r.kind,
                        content=r.content,
                        attributes=dict(r.attributes or {}),
                        source_uri=r.source_uri,
                        created_at=r.created_at,
                        updated_at=r.updated_at,
                    )
                    snippets.append(
                        MemorySnippet(
                            memory=record,
                            chunk_text=r.chunk_text or record.content,
                            similarity=r.similarity,
                        )
                    )
                return snippets

    async def _is_org_sharing_enabled(self, tenant_id: UUID) -> bool:
        with tenant_scope(tenant_id):
            async with transactional_session() as session:
                row = await session.execute(
                    text(
                        "SELECT value FROM tenant_config WHERE key = 'org_share_rep_memories'"
                    )
                )
                value = row.scalar_one_or_none()
        if value is None:
            return self._settings.default_share_rep_memories_across_org
        return _is_truthy_share_value(value)

    async def _find_by_content_hash(
        self,
        session: AsyncSession,
        *,
        memory_table: str,
        owner_column: str | None,
        owner_id: UUID | None,
        content_hash: str,
    ) -> MemoryRecord | None:
        params: dict[str, Any] = {"content_hash": content_hash}
        owner_clause = ""
        if owner_column is not None and owner_id is not None:
            owner_clause = f" AND {owner_column} = :owner_id"
            params["owner_id"] = str(owner_id)
        owner_select = owner_column if owner_column is not None else "NULL::uuid"
        sql = (
            f"SELECT id, tenant_id, {owner_select} AS owner_id, "
            f"kind, content, attributes, source_uri, created_at, updated_at "
            f"FROM {memory_table} "
            f"WHERE deleted_at IS NULL "
            f"AND attributes->>'content_hash' = :content_hash{owner_clause} "
            f"LIMIT 1"
        )
        row = await session.execute(text(sql), params)
        r = row.one_or_none()
        if r is None:
            return None
        tier = next(t for t, meta in _TIER_TABLES.items() if meta["memory_table"] == memory_table)
        return MemoryRecord(
            id=r.id,
            tier=tier,
            tenant_id=r.tenant_id,
            owner_id=r.owner_id,
            kind=r.kind,
            content=r.content,
            attributes=dict(r.attributes or {}),
            source_uri=r.source_uri,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )

    @staticmethod
    def _insert_sql(memory_table: str, owner_column: str | None) -> str:
        """``INSERT ... ON CONFLICT DO NOTHING RETURNING ...``.

        The conflict target is implicit — Postgres matches against any
        applicable unique index (the partial unique indexes from
        migration 0005 cover the dedup keys per tier). Returns no row
        when the insert was skipped; the caller then fetches the
        existing row.
        """
        if owner_column is None:
            return (
                f"INSERT INTO {memory_table} "
                f"(tenant_id, kind, content, attributes, source_uri) "
                f"VALUES (current_setting('app.tenant_id')::uuid, :kind, :content, "
                f"CAST(:attributes AS jsonb), :source_uri) "
                f"ON CONFLICT DO NOTHING "
                f"RETURNING id, tenant_id, NULL::uuid AS owner_id, kind, content, "
                f"attributes, source_uri, created_at, updated_at"
            )
        return (
            f"INSERT INTO {memory_table} "
            f"(tenant_id, {owner_column}, kind, content, attributes, source_uri) "
            f"VALUES (current_setting('app.tenant_id')::uuid, :owner_id, :kind, :content, "
            f"CAST(:attributes AS jsonb), :source_uri) "
            f"ON CONFLICT DO NOTHING "
            f"RETURNING id, tenant_id, {owner_column} AS owner_id, kind, content, "
            f"attributes, source_uri, created_at, updated_at"
        )

    @staticmethod
    def _select_recent_sql(
        *,
        memory_table: str,
        owner_column: str | None,
        owner_id: UUID | None,
        kinds_filter: Sequence[str] | None,
        limit: int,
    ) -> tuple[str, dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        where_clauses = ["deleted_at IS NULL"]
        if owner_column is not None and owner_id is not None:
            where_clauses.append(f"{owner_column} = :owner_id")
            params["owner_id"] = str(owner_id)
        if kinds_filter:
            where_clauses.append("kind = ANY(:kinds)")
            params["kinds"] = list(kinds_filter)
        owner_select = owner_column if owner_column is not None else "NULL::uuid"
        sql = (
            f"SELECT id, tenant_id, {owner_select} AS owner_id, kind, content, "
            f"attributes, source_uri, created_at, updated_at "
            f"FROM {memory_table} "
            f"WHERE {' AND '.join(where_clauses)} "
            f"ORDER BY updated_at DESC LIMIT :limit"
        )
        return sql, params

    @staticmethod
    def _row_to_record(tier: MemoryTier, row: Any, *, owner_column: str | None) -> MemoryRecord:
        return MemoryRecord(
            id=row.id,
            tier=tier,
            tenant_id=row.tenant_id,
            owner_id=row.owner_id if owner_column is not None else None,
            kind=row.kind,
            content=row.content,
            attributes=dict(row.attributes or {}),
            source_uri=row.source_uri,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _owner_for_tier(context: MemoryContext, tier: MemoryTier) -> UUID | None:
        return {
            MemoryTier.REP: context.rep_id,
            MemoryTier.DEAL: context.deal_id,
            MemoryTier.ACCOUNT: context.account_id,
            MemoryTier.ORG: None,
        }[tier]
