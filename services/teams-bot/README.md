# Alex Teams Bot

Microsoft Teams delivery surface for Alex. Stateless FastAPI service that:

- Receives Bot Framework activities on `POST /api/messages` (messages + Adaptive Card submits) and routes them through `AlexActivityHandler`.
- Receives `AgentOutput` from the Agent Runtime on `POST /deliver`, looks up the rep's saved `ConversationReference`, and posts an Adaptive Card proactively via `adapter.continue_conversation`.
- Acknowledges Adaptive Card submits within 3 seconds, then forwards an HMAC-signed `ApprovalCallback` (or `FeedbackEvent`) to the Agent Runtime's `/callbacks` endpoint.
- Processes OAuth callback redirects (`GET /oauth/callback`) during onboarding; forwards the resulting `OAuthToken` to the Pipedream `oauth_relay` workflow.

All AI content and state live in the Agent Runtime + Data Layer; this service holds nothing platform-specific beyond the rep's `ConversationReference`, which is carried in each inbound `AgentOutput` payload from the runtime.

## Layout

```
src/alex_teams_bot/
├── main.py                # FastAPI app + bot adapter wiring
├── config.py              # pydantic-settings
├── activity_handler.py    # AlexActivityHandler (on_message, on_adaptive_card_invoke)
├── adapter.py             # CloudAdapter + ConfigurationBotFrameworkAuthentication
├── middleware.py          # AlexSignatureMiddleware on /deliver
├── schemas.py             # AgentOutput, ApprovalCallback, FeedbackEvent, OAuthToken
├── routes/
│   ├── messages.py        # POST /api/messages (Bot Framework activity webhook)
│   ├── deliver.py         # POST /deliver from agent-runtime
│   ├── oauth.py           # GET /oauth/start, GET /oauth/callback
│   └── health.py
└── services/
    ├── adaptive_cards.py  # AgentOutput → AdaptiveCard JSON
    ├── signing.py         # X-Alex-Signature verify/sign
    ├── runtime_client.py  # POST /callbacks on Agent Runtime
    ├── pipedream_client.py# POST oauth_relay on Pipedream
    └── oauth_providers.py # Google reference provider
```

## Local development

```sh
cd services/teams-bot
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
[ "$(uname)" = "Darwin" ] && chflags nohidden .venv/lib/python3.12/site-packages/__editable__.alex_teams_bot-*.pth 2>/dev/null
uvicorn alex_teams_bot.main:app --reload --port 8002
```

In dev without real Azure credentials the bot starts but inbound Bot Framework requests will 401 (the adapter rejects un-signed requests by default). The pytest suite drives the activity handler directly rather than through the adapter to keep tests offline.

## Configuration

| Variable | Default | Notes |
| --- | --- | --- |
| `MICROSOFT_APP_ID` | unset | Azure Bot registration app id. Required for production. |
| `MICROSOFT_APP_PASSWORD` | unset | Azure Bot registration secret. |
| `MICROSOFT_APP_TYPE` | `MultiTenant` | One of `MultiTenant`, `SingleTenant`, `UserAssignedMsi`. |
| `MICROSOFT_APP_TENANT_ID` | unset | Required for `SingleTenant`. |
| `ALEX_AGENT_RUNTIME_URL` | unset | Base URL the bot POSTs approval callbacks to. |
| `ALEX_PIPEDREAM_OAUTH_RELAY_URL` | unset | URL of the Pipedream `oauth_relay` workflow. |
| `ALEX_WEBHOOK_SECRET` | unset | Shared HMAC secret with the runtime + Pipedream sides. |
| `OAUTH_GOOGLE_*` | unset | Same reference provider config as the slack-bot. |

## Out of scope (per WO #6)

- Slack delivery (WO #5).
- Routing decisions across delivery channels (`DeliveryPreference` lives in the Agent Runtime).
- Approval enforcement / business logic.
- Drafting / generation — content lives in `AgentOutput`.
