"""Alembic environment.

The connection string comes from DATABASE_URL and is never written into alembic.ini, so a
credential cannot reach the repository by someone running `alembic init` habits.

`include_schemas=True` matters: every table lives in a named schema, so without it
autogenerate would see an empty `public` and cheerfully propose dropping the entire
database.

Task ids: M0.3.2
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from brain.db import SCHEMAS, metadata, normalise_database_url

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

url = os.environ.get("DATABASE_URL")
if not url:
    msg = "DATABASE_URL is not set; migrations have nothing to connect to"
    raise RuntimeError(msg)

config.set_main_option("sqlalchemy.url", normalise_database_url(url).replace("%", "%%"))

target_metadata = metadata


def include_name(name: str | None, type_: str, _parent: object) -> bool:
    """Keep autogenerate inside our own schemas.

    Without this, a shared database would have Alembic proposing to drop tables belonging
    to something else entirely.
    """
    if type_ == "schema":
        return name in SCHEMAS
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        include_schemas=True,
        include_name=include_name,
        compare_type=True,
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
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            include_name=include_name,
            compare_type=True,
            # One transaction for the whole run: a migration that fails halfway leaves
            # nothing behind, so a failed deploy is a no-op rather than a half-migrated
            # schema that the previous image can no longer read.
            transaction_per_migration=False,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
