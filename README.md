# Alex

AI Chief of Staff for B2B sales reps, built on the Claude Agent SDK. EU-hosted, Slack/Teams-native, GDPR-first.

## Repository Layout

This is a monorepo. Each service lives under `services/`.

| Path | Description |
| --- | --- |
| `services/data-layer/` | PostgreSQL + pgvector schema and Alembic migrations (WO #1). |
| `services/agent-runtime/` | Agent runtime built on the Claude Agent SDK (WO #2 — not yet implemented). |
| `services/pipedream/` | Pipedream integration layer — inbound workflows (WO #3) and outbound execution + OAuth relay (WO #4). |
| `services/slack-bot/` | Slack messaging surface (WO #5 — not yet implemented). |
| `services/teams-bot/` | Microsoft Teams messaging surface (WO #6 — not yet implemented). |

## Local development

A local Postgres + pgvector instance is provided via Docker Compose:

```sh
cp .env.example .env
docker compose up -d postgres
```

Then run service-specific commands from each `services/<name>/` directory. See `services/data-layer/README.md` for migration commands.

## Phase 1 status

- [x] WO #1 — Data Layer: Database Schema & Migrations
- [x] WO #2 — Agent Runtime: Core Service Scaffold
- [x] WO #3 — Pipedream Integration Layer: Inbound Event Workflows
- [x] WO #4 — Pipedream Integration Layer: Action Execution & OAuth Relay
- [ ] WO #5 — Messaging Surface: Slack App
- [ ] WO #6 — Messaging Surface: Teams Bot
