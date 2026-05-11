"""EmbeddingIndexer — chunk + dedupe + embed + persist to ``*_embeddings`` tables.

Dedup happens at the chunk_text level: before embedding, we look up
existing rows for the same ``source_id`` and skip any chunk whose text
is already present. The HNSW index on ``content_vector`` is built by the
data-layer migration; this writer just inserts rows.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..schemas import EmbeddingChunk, MemoryTier
from .embedding_client import EmbeddingClient

log = structlog.get_logger(__name__)

_TIER_TO_EMBEDDING_TABLE: dict[MemoryTier, str] = {
    MemoryTier.REP: "rep_memory_embeddings",
    MemoryTier.DEAL: "deal_memory_embeddings",
    MemoryTier.ACCOUNT: "account_memory_embeddings",
    MemoryTier.ORG: "org_memory_embeddings",
}


@dataclass(slots=True)
class IndexedChunks:
    indexed: int
    skipped_duplicates: int


class EmbeddingIndexer:
    def __init__(self, client: EmbeddingClient) -> None:
        self._client = client

    async def index(
        self,
        *,
        session: AsyncSession,
        tier: MemoryTier,
        source_id: str,
        content: str,
        chunk_chars: int = 1800,
        overlap: int = 200,
    ) -> IndexedChunks:
        table = _TIER_TO_EMBEDDING_TABLE[tier]
        chunks = self._split(content, chunk_chars=chunk_chars, overlap=overlap)
        if not chunks:
            return IndexedChunks(indexed=0, skipped_duplicates=0)

        existing = await session.execute(
            text(f"SELECT chunk_text FROM {table} WHERE source_id = :sid"),
            {"sid": source_id},
        )
        existing_texts = {row[0] for row in existing}

        novel: list[EmbeddingChunk] = [c for c in chunks if c.text not in existing_texts]
        skipped = len(chunks) - len(novel)
        if not novel:
            return IndexedChunks(indexed=0, skipped_duplicates=skipped)

        vectors = await self._client.embed([c.text for c in novel])
        if len(vectors) != len(novel):
            raise RuntimeError(
                f"EmbeddingClient returned {len(vectors)} vectors for {len(novel)} chunks"
            )

        for chunk, vector in zip(novel, vectors, strict=True):
            await session.execute(
                text(
                    f"""
                    INSERT INTO {table}
                        (tenant_id, source_id, content_vector, model_name,
                         model_version, chunk_index, chunk_text)
                    VALUES
                        (current_setting('app.tenant_id')::uuid,
                         :sid,
                         CAST(:vec AS vector),
                         :model_name,
                         :model_version,
                         :chunk_index,
                         :chunk_text)
                    """
                ),
                {
                    "sid": source_id,
                    "vec": _vector_literal(vector),
                    "model_name": self._client.name,
                    "model_version": None,
                    "chunk_index": chunk.chunk_index,
                    "chunk_text": chunk.text,
                },
            )

        log.info(
            "embedding_indexer.indexed",
            tier=tier.value,
            source_id=source_id,
            indexed=len(novel),
            skipped=skipped,
        )
        return IndexedChunks(indexed=len(novel), skipped_duplicates=skipped)

    @staticmethod
    def _split(content: str, *, chunk_chars: int, overlap: int) -> list[EmbeddingChunk]:
        if not content:
            return []
        if chunk_chars <= 0:
            raise ValueError("chunk_chars must be positive")
        if overlap < 0 or overlap >= chunk_chars:
            raise ValueError("overlap must be in [0, chunk_chars)")
        stride = chunk_chars - overlap
        chunks: list[EmbeddingChunk] = []
        idx = 0
        for start in range(0, len(content), stride):
            piece = content[start : start + chunk_chars]
            if not piece.strip():
                continue
            chunks.append(EmbeddingChunk(text=piece, chunk_index=idx))
            idx += 1
            if start + chunk_chars >= len(content):
                break
        return chunks


def _vector_literal(vector: list[float]) -> str:
    """Format a Python list as the pgvector text input '[x, y, z, ...]'.

    Validates that every element is finite — pgvector rejects ``NaN`` /
    ``Inf`` at cast time with a generic error that would roll the
    surrounding write transaction back, so we surface it explicitly
    instead of letting a bad embedding silently abort a memory write.
    """
    for value in vector:
        if not math.isfinite(value):
            raise ValueError(
                f"vector contains non-finite value ({value}); refusing to write"
            )
    return "[" + ",".join(format(v, ".7g") for v in vector) + "]"
