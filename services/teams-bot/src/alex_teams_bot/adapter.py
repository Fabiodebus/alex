"""Bot Framework adapter wiring.

Uses ``BotFrameworkAdapter`` (the multi-tenant adapter that ships in
``botbuilder-core``) rather than the newer ``CloudAdapter`` because the
latter lives in ``botbuilder-integration-aiohttp`` and would force an
aiohttp server alongside our FastAPI app. ``BotFrameworkAdapter``
exposes the two methods we need — ``process_activity`` and
``continue_conversation`` — and is fully supported in 4.17.x.
"""
from __future__ import annotations

from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings

from .config import Settings, get_settings


def build_adapter(settings: Settings | None = None) -> BotFrameworkAdapter:
    settings = settings or get_settings()
    bot_settings = BotFrameworkAdapterSettings(
        app_id=settings.microsoft_app_id,
        app_password=settings.microsoft_app_password,
    )
    return BotFrameworkAdapter(bot_settings)
