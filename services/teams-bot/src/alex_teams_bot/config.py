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

    teams_bot_host: str = "0.0.0.0"
    teams_bot_port: int = 8002

    microsoft_app_id: str = ""
    microsoft_app_password: str = ""
    microsoft_app_type: str = "MultiTenant"
    microsoft_app_tenant_id: str = ""

    alex_agent_runtime_url: str = ""
    alex_pipedream_oauth_relay_url: str = ""
    alex_webhook_secret: str = ""
    webhook_signature_header: str = "X-Alex-Signature"
    webhook_timestamp_header: str = "X-Alex-Timestamp"
    webhook_signature_max_age_seconds: int = 300

    oauth_google_client_id: str = ""
    oauth_google_client_secret: str = ""
    oauth_google_redirect_uri: str = "http://localhost:8002/oauth/callback"
    oauth_state_secret: str = Field(default="")

    @property
    def webhook_signing_enforced(self) -> bool:
        return bool(self.alex_webhook_secret)

    @property
    def has_microsoft_credentials(self) -> bool:
        return bool(self.microsoft_app_id and self.microsoft_app_password)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
