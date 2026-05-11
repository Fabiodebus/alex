"""Embedding model abstraction.

Mirrors the AgentBackend pattern from WO #2 — `EmbeddingClient` is a
Protocol; ``build_default_embedding_client`` returns the real OpenAI
adapter when ``OPENAI_API_KEY`` is set, and a deterministic stub
otherwise so dev environments still write coherent vectors (the data
layer's HNSW index requires fixed-dim non-null vectors).
"""
from __future__ import annotations

import hashlib
import math
from typing import Protocol, runtime_checkable

import structlog

from ..config import Settings, get_settings

log = structlog.get_logger(__name__)


@runtime_checkable
class EmbeddingClient(Protocol):
    name: str
    dim: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class StubEmbeddingClient:
    """Deterministic content-derived vectors.

    Produces ``dim``-length pseudo-vectors by seeding a hash-based PRNG
    from the input string. Two identical inputs produce identical
    outputs, so cosine similarity ranking still distinguishes
    near-duplicates from unrelated text — enough to validate the
    end-to-end retrieve() path without an OpenAI key.
    """

    name = "stub"

    def __init__(self, dim: int = 1536) -> None:
        self.dim = dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        seed = hashlib.sha256(text.encode("utf-8")).digest()
        vector: list[float] = []
        i = 0
        while len(vector) < self.dim:
            # Take 4 bytes at a time, treat as unsigned int, map to [-1, 1].
            chunk = seed[(i * 4) % len(seed) : (i * 4) % len(seed) + 4]
            if len(chunk) < 4:
                # Re-extend via SHA-256 of the prior chunk for long dim.
                seed = hashlib.sha256(seed).digest()
                i = 0
                continue
            n = int.from_bytes(chunk, "big", signed=False)
            vector.append(((n / 2**32) * 2.0) - 1.0)
            i += 1
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]


class OpenAIEmbeddingClient:
    """Adapter for OpenAI-compatible embeddings APIs (works with Anthropic-
    backed gateways that mimic the OpenAI schema too)."""

    name = "openai"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.dim = settings.embedding_dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        # Lazy import: the openai SDK adds tens of MB; only require it when
        # an API key is actually configured.
        from openai import AsyncOpenAI  # type: ignore[import-not-found]

        client = AsyncOpenAI(
            api_key=self._settings.openai_api_key,
            base_url=self._settings.openai_base_url or None,
        )
        try:
            response = await client.embeddings.create(
                model=self._settings.embedding_model,
                input=texts,
            )
        finally:
            # AsyncOpenAI has no public close in older versions; best-effort.
            close = getattr(client, "close", None)
            if close is not None:
                await close()
        return [item.embedding for item in response.data]


def build_default_embedding_client(settings: Settings | None = None) -> EmbeddingClient:
    s = settings or get_settings()
    if s.has_real_embedding_client:
        log.info(
            "embedding_client.selected",
            backend="openai",
            model=s.embedding_model,
            dim=s.embedding_dim,
        )
        return OpenAIEmbeddingClient(s)
    log.warning("embedding_client.selected", backend="stub", dim=s.embedding_dim)
    return StubEmbeddingClient(dim=s.embedding_dim)
