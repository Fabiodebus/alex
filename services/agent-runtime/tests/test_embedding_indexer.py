from __future__ import annotations

import pytest

from alex_agent_runtime.services.embedding_indexer import EmbeddingIndexer
from alex_agent_runtime.services.embedding_client import StubEmbeddingClient


def test_split_emits_overlapping_chunks():
    indexer = EmbeddingIndexer(StubEmbeddingClient(dim=8))
    chunks = indexer._split("ABCDEFGHIJ", chunk_chars=4, overlap=1)
    assert [c.text for c in chunks] == ["ABCD", "DEFG", "GHIJ"]
    assert [c.chunk_index for c in chunks] == [0, 1, 2]


def test_split_handles_empty_content():
    indexer = EmbeddingIndexer(StubEmbeddingClient(dim=8))
    assert indexer._split("", chunk_chars=100, overlap=10) == []


def test_split_rejects_invalid_overlap():
    indexer = EmbeddingIndexer(StubEmbeddingClient(dim=8))
    with pytest.raises(ValueError):
        indexer._split("abc", chunk_chars=4, overlap=4)
    with pytest.raises(ValueError):
        indexer._split("abc", chunk_chars=4, overlap=-1)


@pytest.mark.asyncio
async def test_stub_embedding_is_deterministic_and_normalised():
    client = StubEmbeddingClient(dim=64)
    a, b = await client.embed(["hello world", "hello world"])
    assert a == b
    # Roughly unit-norm (allow small float error).
    norm = sum(v * v for v in a) ** 0.5
    assert abs(norm - 1.0) < 1e-6


@pytest.mark.asyncio
async def test_stub_embedding_different_texts_differ():
    client = StubEmbeddingClient(dim=64)
    a, b = await client.embed(["hello", "goodbye"])
    assert a != b
