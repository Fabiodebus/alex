"""Typed runtime configuration loaded from the environment / `.env`."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _find_dotenv() -> Path | None:
    here = Path(__file__).resolve()
    for candidate in (
        here.parents[2] / ".env",
        here.parents[3] / ".env",
        here.parents[4] / ".env",
    ):
        if candidate.is_file():
            return candidate
    return None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_find_dotenv(),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    database_url: str = Field(
        default="postgresql+psycopg://alex:alex_local_dev@localhost:5432/alex"
    )

    agent_runtime_host: str = "0.0.0.0"
    agent_runtime_port: int = 8000

    anthropic_api_key: str = ""
    anthropic_base_url: str = ""
    anthropic_model: str = "claude-sonnet-4-6"

    tenant_header: str = "X-Tenant-Id"
    scheduler_heartbeat_seconds: int = 60

    # Webhook signing — used by both inbound Pipedream events and (later)
    # Slack/Teams approval callbacks. When empty the middleware is a no-op
    # so dev environments can hit /events with curl without ceremony.
    alex_webhook_secret: str = ""
    webhook_signature_header: str = "X-Alex-Signature"
    webhook_timestamp_header: str = "X-Alex-Timestamp"
    webhook_signature_max_age_seconds: int = 300

    # Base URL for the Pipedream outbound workflow endpoints. Each
    # workflow has its own URL slug appended (e.g. /hubspot_crm_write).
    # Leave empty in tests; PipedreamClient raises PipedreamConfigError if
    # asked to dispatch without it.
    pipedream_base_url: str = ""

    # Embedding pipeline (WO #7). Dimension matches the data layer's
    # `*_embeddings.content_vector` column. Leave OPENAI_API_KEY empty in
    # dev — EmbeddingIndexer falls back to StubEmbeddingClient.
    openai_api_key: str = ""
    openai_base_url: str = ""
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536
    embedding_chunk_chars: int = 1800
    embedding_chunk_overlap: int = 200

    # Default per-tenant policy when no explicit TenantConfig row applies:
    # rep memory is rep-private. The MemoryStore reads the
    # `org_share_rep_memories` flag from TenantConfig on each retrieve;
    # this value is the fallback when the row is missing.
    default_share_rep_memories_across_org: bool = False

    # ------------------------------------------------------------------
    # WO #8 — MemorySummarizer + IngestionPipeline
    # ------------------------------------------------------------------
    # Source rows can be older than the latest summary by this many
    # seconds before we consider the summary stale enough to rebuild.
    summary_staleness_seconds: int = 60 * 60 * 6  # 6h
    # Cap on how many recent source rows feed a single summary (keeps
    # the prompt bounded).
    summary_source_limit: int = 20
    # Cron expression for the periodic full-rebuild pass run by the
    # SchedulerService. Empty string disables the periodic job; on-demand
    # calls always work.
    summary_cron: str = "*/30 * * * *"

    # Which IngestionProvider implementation to wire on lifespan. "stub"
    # returns deterministic synthetic data for tests; "pipedream" POSTs
    # (signed) to the configured workflow URL.
    ingestion_provider: str = "stub"
    alex_pipedream_ingestion_url: str = ""
    ingestion_default_since_days: int = 90
    ingestion_recording_cap: int = 5

    # WO #9 — CRMReader / on-demand CRM fetch via Pipedream `crm_fetch`.
    # The CRMReader uses ``stub`` by default for dev (returns no record
    # on cache miss so the test harness can decide). ``pipedream`` POSTs
    # to the configured workflow URL.
    crm_fetch_provider: str = "stub"
    alex_pipedream_crm_fetch_url: str = ""

    # WO #10 — CRMWriter / approved-write dispatch via Pipedream
    # `crm_write`. Symmetric to crm_fetch_provider. ``stub`` echoes
    # success locally so the runtime can be exercised end-to-end without
    # a live Pipedream workflow.
    crm_write_provider: str = "stub"
    alex_pipedream_crm_write_url: str = ""

    # WO #13 — Notification Delivery. ``stub`` records attempts
    # in-memory; ``http`` POSTs to the Slack / Teams messaging surfaces.
    messaging_delivery_provider: str = "stub"
    alex_slack_deliver_url: str = ""
    alex_teams_deliver_url: str = ""
    # Window after which an un-acknowledged delivery is escalated to
    # the next daily brief. The blueprint calls this "the configurable
    # retry window"; 30 minutes is the v1 default.
    delivery_escalation_seconds: int = 30 * 60
    delivery_max_attempts: int = 3

    # WO #14 — Voice Model EWMA tuning. ``alpha = max(min_alpha,
    # 1 / (1 + sample_count * decay))``. Defaults give a fresh profile
    # alpha=1.0 (first edit defines it), then 0.5 / 0.33 / 0.25 / ...
    # converging on min_alpha as the rep accumulates approved drafts.
    voice_update_min_alpha: float = 0.05
    voice_update_decay: float = 1.0

    @property
    def has_real_embedding_client(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def has_real_agent_backend(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def webhook_signing_enforced(self) -> bool:
        return bool(self.alex_webhook_secret)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
