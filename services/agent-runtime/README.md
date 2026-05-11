# Alex Agent Runtime

Core orchestration service for Alex. FastAPI application that:

- Receives normalized `IntegrationEvent` payloads from the Pipedream Integration Layer (`POST /events`) and routes them to feature workflows.
- Receives `ApprovalCallback` events from the Slack/Teams Messaging Surface (`POST /callbacks`) and transitions task state.
- Wraps the Claude Agent SDK in a swappable `AgentBackend` abstraction.
- Runs scheduled jobs (stalled deal detection, daily brief assembly, memory summarization) via APScheduler.
- Enforces tenant isolation by setting the `app.tenant_id` GUC on every DB transaction so the data-layer's RLS policies bind correctly.
- Writes a pre-execution `audit_log` entry for every approved external action.

## Local development

From the repo root, bring up the local Postgres + run migrations once:

```sh
cp .env.example .env
docker compose up -d postgres
( cd services/data-layer && pip install -e . && alembic upgrade head )
```

Then this service:

```sh
cd services/agent-runtime
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'

# macOS only: pip on darwin marks the editable `.pth` file UF_HIDDEN,
# which makes Python's site.py skip it on import (see pip#11879). Clear
# the flag after every install:
[ "$(uname)" = "Darwin" ] && chflags nohidden .venv/lib/python3.12/site-packages/__editable__.alex_agent_runtime-*.pth

uvicorn alex_agent_runtime.main:app --reload --host 0.0.0.0 --port 8000
```

The runtime needs the Claude CLI on `PATH` for the Agent SDK's subprocess transport. For local dev, install it via `npm i -g @anthropic-ai/claude-code` or set `ANTHROPIC_API_KEY=` empty to fall back to the stub backend (still routes events, just doesn't call the model).

## Configuration

Settings are loaded by `pydantic-settings` from environment / `.env`. See the repo-root `.env.example` for the shared block; the runtime adds:

| Variable | Default | Notes |
| --- | --- | --- |
| `AGENT_RUNTIME_HOST` | `0.0.0.0` | Bind host for uvicorn. |
| `AGENT_RUNTIME_PORT` | `8000` | Bind port. |
| `ANTHROPIC_API_KEY` | unset | When unset the runtime uses `StubAgentBackend` so local dev still boots. |
| `ANTHROPIC_BASE_URL` | unset | EU residency override. Forwarded to the Claude SDK subprocess via `env=`. |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6` | Default model id. |
| `TENANT_HEADER` | `X-Tenant-Id` | HTTP header carrying the tenant uuid for inbound events/callbacks. |
| `SCHEDULER_HEARTBEAT_SECONDS` | `60` | Interval for the no-op heartbeat job. |

## Architecture

- `main.py` — FastAPI app, lifespan that wires DB engine, scheduler, agent backend.
- `config.py` — `pydantic-settings` typed Settings.
- `tenant_context.py` — `ContextVar[UUID]` so each request gets an async-safe tenant id.
- `db.py` — async SQLAlchemy engine, `AsyncSession` factory, `transactional_session()` context manager that runs `SET LOCAL app.tenant_id` on entry.
- `middleware.py` — extracts `X-Tenant-Id` from inbound requests and binds it to the contextvar.
- `services/event_processor.py` — `/events` core: dedupe via `processed_events`, route via FeatureRouter, audit-log.
- `services/approval_handler.py` — `/callbacks` core: load `task_state`, transition status, audit-log.
- `services/agent_backend.py` — `AgentBackend` Protocol, `ClaudeAgentBackend` (real SDK), `StubAgentBackend` (used when no API key configured).
- `services/scheduler.py` — APScheduler `AsyncIOScheduler` lifecycle + job registry.
- `services/audit_log.py` — `record_action()` helper that inserts `audit_log` rows.

## Tests

```sh
pytest
```

Tests use a Postgres reachable via `DATABASE_URL` (the same as the dev DB). They create an isolated tenant per test via fixtures.

## Production

`Dockerfile` produces an image that bundles Python 3.12, Node 20 LTS, the Claude CLI, and this package. Configure deployment with the env vars above plus `ANTHROPIC_API_KEY` and `DATABASE_URL`. Migrations are applied separately by the data-layer release process before the runtime starts.
