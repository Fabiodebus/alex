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

    slack_bot_host: str = "0.0.0.0"
    slack_bot_port: int = 8001

    slack_signing_secret: str = ""
    slack_bot_token: str = ""

    alex_agent_runtime_url: str = ""
    alex_pipedream_oauth_relay_url: str = ""
    alex_webhook_secret: str = ""
    webhook_signature_header: str = "X-Alex-Signature"
    webhook_timestamp_header: str = "X-Alex-Timestamp"
    webhook_signature_max_age_seconds: int = 300

    # Reference OAuth provider — Google covers Gmail / Calendar / Drive.
    oauth_google_client_id: str = ""
    oauth_google_client_secret: str = ""
    oauth_google_redirect_uri: str = "http://localhost:8001/oauth/callback"
    oauth_state_secret: str = Field(
        default="",
        description="Used to HMAC-sign the OAuth `state` parameter so we can verify it on callback.",
    )

    # Onboarding (WO #15). Demo mode: the slash command uses this as the
    # tenant_id when the Slack team isn't already mapped to a runtime
    # tenant. Set per-deployment.
    alex_demo_tenant_id: str = ""

    @property
    def webhook_signing_enforced(self) -> bool:
        return bool(self.alex_webhook_secret)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
