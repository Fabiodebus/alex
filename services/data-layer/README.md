# Alex Data Layer

Owns the PostgreSQL + pgvector schema and Alembic migrations for Alex. This package contains **no** application-layer ORM models or query helpers — those live in `services/agent-runtime/` per the Data Layer blueprint, which mandates that the Agent Runtime is the sole direct client of the database.

## What this schema provides

- **Memory tier tables** — `rep_memories`, `deal_memories`, `account_memories`, `org_memories` (structured) plus matching `*_embeddings` tables (pgvector).
- **Identity tables** — `tenants`, `reps`, `accounts`, `deals` for the foreign-key targets the memory tables and embeddings reference.
- **Operational tables** — `task_state`, `audit_log`, `tenant_config`.
- **Tenant isolation** — Row-level security policies keyed off the `app.tenant_id` GUC. Every tenant-scoped table enforces a `USING (tenant_id = current_setting('app.tenant_id')::uuid)` policy.
- **Right-to-deletion cascades** — Foreign keys on every `*_embeddings` table use `ON DELETE CASCADE` so a deletion of a rep, deal, account, or org row removes the associated embeddings in the same transaction.
- **Append-only audit log** — `audit_log` is protected by triggers that raise `EXCEPTION` on `UPDATE` and `DELETE` regardless of role.
- **Vector indexes** — HNSW with `vector_cosine_ops` on each `*_embeddings.content_vector` column.

## Local setup

From the repo root:

```sh
cp .env.example .env
docker compose up -d postgres
```

Install this package (editable) and run migrations:

```sh
cd services/data-layer
python -m venv .venv && source .venv/bin/activate
pip install -e .[dev]
alembic upgrade head
```

To roll back:

```sh
alembic downgrade base
```

## Notes

- `DATABASE_URL` must be set (the `.env` file at the repo root is loaded automatically).
- Vector dimension defaults to 1536 (the OpenAI `text-embedding-3-small` size). Phase 2's `EmbeddingIndexer` should keep its embedding model output in sync; if a different dimension is required, a follow-up migration must alter the affected `content_vector` columns and rebuild the HNSW indexes.
- Autogenerate is disabled — schema changes are written by hand to keep DDL (RLS, triggers, partial indexes) reviewable in plain SQL.
