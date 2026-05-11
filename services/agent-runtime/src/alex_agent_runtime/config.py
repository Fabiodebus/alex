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

    @property
    def has_real_agent_backend(self) -> bool:
        return bool(self.anthropic_api_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
