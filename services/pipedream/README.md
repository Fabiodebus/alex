# Alex Pipedream Integration Layer

This package contains the **source code** for the Pipedream workflows that mediate every external system integration. Each workflow lives on Pipedream's managed iPaaS in an EU region and shares the same flow:

```
external webhook → normalize → HMAC-sign → POST /events on Agent Runtime
```

## Repository layout

```
src/
├── lib/
│   ├── integration_event.mjs    # IntegrationEvent schema + validator
│   ├── normalizer.mjs           # per-source mappers to IntegrationEvent
│   ├── forwarder.mjs            # HMAC-SHA256 signing + POST to Agent Runtime
│   ├── activity_log.mjs         # structured logging
│   └── errors.mjs               # IntegrationError + serialization
└── workflows/
    ├── hubspot_record_update/     # CRM reference (Tier 1)
    ├── google_calendar_event/     # Calendar reference (Tier 1)
    ├── gmail_message_received/    # Email reference (Tier 1)
    └── gong_recording_completed/  # Recording reference (Tier 2)
tests/
└── *.test.mjs                    # Node built-in test runner
```

Each workflow's `index.mjs` is a Pipedream-flavoured `defineComponent` module: paste it into a Pipedream code step, or use `pd push` against a Pipedream project to deploy.

## Adding a new integration source

1. Add a normalizer function for the source in `src/lib/normalizer.mjs` (one pure function: `(rawPayload) → IntegrationEvent`).
2. Add a fixture + test in `tests/normalizer.test.mjs` to lock the mapping.
3. Copy one of the reference workflow folders under `src/workflows/`, swap the trigger app, and update the `normalize` call.
4. Deploy on Pipedream pointing the trigger at the right OAuth connection.

## Authentication

Workflows sign every outgoing payload to Alex with HMAC-SHA256 using `ALEX_WEBHOOK_SECRET` (Pipedream env var). The Agent Runtime's `WebhookSignatureMiddleware` verifies the `X-Alex-Signature` header before any handler runs. The shared secret is set in **both** Pipedream's environment and Alex's `.env`.

Headers sent on every forwarded request:

| Header | Value |
| --- | --- |
| `X-Tenant-Id` | UUID of the tenant the event belongs to. |
| `X-Alex-Signature` | `sha256=<hex digest>` of the request body. |
| `X-Alex-Timestamp` | ISO-8601 timestamp; included in the signed payload to prevent replays. |
| `Content-Type` | `application/json`. |

## Local development

```sh
cd services/pipedream
npm test       # node --test against tests/
npm run lint   # syntax check on lib/*.mjs
```

There are no runtime dependencies — the package uses Node's built-in `crypto`, `fetch`, and `test` modules so it can run unchanged on Pipedream's hosted environment.

## Deployment

Pipedream EU region must be selected at the workspace level (Settings → Privacy & Security → Data Region → EU). Each workflow is deployed with:

```sh
pd push src/workflows/hubspot_record_update
```

The deployer should set these environment variables on the Pipedream project:

| Var | Purpose |
| --- | --- |
| `ALEX_AGENT_RUNTIME_URL` | Base URL of the Agent Runtime (e.g., `https://agent.alex.predict-ability.com`). |
| `ALEX_WEBHOOK_SECRET` | Shared secret for HMAC signing (same value as the Agent Runtime). |
| `ALEX_TENANT_ID_RESOLVER` | Optional fallback tenant id when the source payload doesn't carry one. |

## What this WO does *not* cover

- Outbound `ActionRequest` execution (CRM writes, email sends) — owned by WO #4.
- OAuth relay — owned by WO #4.
- DryRunRequest/DryRunResponse — owned by WO #4.
- Feature-specific business logic — owned by feature WOs (not in Pipedream).
