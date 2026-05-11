# Alex Slack Bot

The Slack delivery surface for Alex. Stateless Slack Bolt + FastAPI service that:

- Receives `AgentOutput` payloads from the Agent Runtime on `POST /deliver` and renders them as interactive Block Kit DMs to the rep.
- Handles Slack interactive component callbacks (`approve` / `edit` / `discard` / `feedback`) — ACKs Slack within 3 seconds, forwards an `ApprovalCallback` (HMAC-signed) to the Agent Runtime's `/callbacks` endpoint.
- Processes OAuth callback redirects (`GET /oauth/callback`) for CRM/email/calendar connections during onboarding; exchanges the auth code for a token and forwards the `OAuthToken` (HMAC-signed) to the Pipedream `oauth_relay` workflow.
- Exposes `GET /oauth/start` returning a state-bound provider auth URL the Slack onboarding flow links to.

All AI content and state live in the Agent Runtime + Data Layer; this service holds nothing.

## Layout

```
src/alex_slack_bot/
├── main.py                # FastAPI app + Bolt mount + lifespan
├── config.py              # pydantic-settings
├── bolt_app.py            # AsyncApp + event/action/command handlers
├── middleware.py          # AlexSignatureMiddleware on /deliver
├── schemas.py             # AgentOutput, ApprovalCallback, FeedbackEvent, OAuthToken
├── routes/
│   ├── deliver.py
│   ├── oauth.py
│   └── health.py
└── services/
    ├── block_kit.py       # AgentOutput → Block Kit blocks
    ├── signing.py         # X-Alex-Signature verify/sign helpers
    ├── runtime_client.py  # POST /callbacks on the Agent Runtime
    ├── pipedream_client.py# POST oauth_relay on Pipedream
    └── oauth_providers.py # Google reference provider — auth URL + token exchange
```

## Local development

```sh
cd services/slack-bot
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
[ "$(uname)" = "Darwin" ] && chflags nohidden .venv/lib/python3.12/site-packages/__editable__.alex_slack_bot-*.pth 2>/dev/null
uvicorn alex_slack_bot.main:app --reload --port 8001
```

Slack signing secret + bot token are loaded from `SLACK_SIGNING_SECRET` and `SLACK_BOT_TOKEN`. In dev without real Slack credentials the Bolt app still starts (Bolt validates inbound requests, not boot-time secrets), but events from real Slack won't reach it. The pytest suite mocks the Slack web client so no live credentials are needed.

## Configuration

| Variable | Default | Notes |
| --- | --- | --- |
| `SLACK_SIGNING_SECRET` | unset | Required for production; Bolt rejects unsigned inbound webhooks. |
| `SLACK_BOT_TOKEN` | unset | Required for production `chat.postMessage` calls. |
| `ALEX_AGENT_RUNTIME_URL` | unset | Base URL of the Agent Runtime; targets `/callbacks` for approvals. |
| `ALEX_PIPEDREAM_OAUTH_RELAY_URL` | unset | URL of the `oauth_relay` Pipedream workflow. |
| `ALEX_WEBHOOK_SECRET` | unset | Shared HMAC secret with the runtime + Pipedream sides; same env var name as the other services. |
| `OAUTH_GOOGLE_CLIENT_ID` / `OAUTH_GOOGLE_CLIENT_SECRET` / `OAUTH_GOOGLE_REDIRECT_URI` | unset | Reference OAuth provider config. Reuse the pattern for HubSpot, Microsoft, etc. |

## Out of scope (per WO #5)

- Teams bot (WO #6).
- Routing decisions across delivery channels (`DeliveryPreference` lives in Agent Runtime).
- Approval enforcement / business logic.
- Drafting / generation — content lives in `AgentOutput`.
