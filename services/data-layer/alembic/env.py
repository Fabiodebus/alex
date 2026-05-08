"""Alembic environment for the Alex data layer.

Reads the database URL from the ``DATABASE_URL`` environment variable. A
``.env`` file at the repository root is loaded automatically when present.
"""
from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv is a dev convenience
    load_dotenv = None

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


# ---------------------------------------------------------------------------
# Resolve DATABASE_URL
# ---------------------------------------------------------------------------
def _load_dotenv_files() -> None:
    if load_dotenv is None:
        return
    here = Path(__file__).resolve()
    candidates = [
        here.parents[1] / ".env",  # services/data-layer/.env
        here.parents[2] / ".env",  # services/.env
        here.parents[3] / ".env",  # repo root .env
    ]
    for candidate in candidates:
        if candidate.is_file():
            load_dotenv(candidate, override=False)


_load_dotenv_files()

database_url = os.environ.get("DATABASE_URL")
if not database_url:
    raise RuntimeError(
        "DATABASE_URL is not set. Copy .env.example to .env at the repo root "
        "or export DATABASE_URL before running Alembic."
    )

config.set_main_option("sqlalchemy.url", database_url)

# Migrations are written in raw DDL via op.execute / op.create_table; we do
# not maintain a Declarative Base in this package (ORM models are out of
# scope per WO #1). target_metadata is therefore None — autogenerate is
# intentionally disabled.
target_metadata = None


def run_migrations_offline() -> None:
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
