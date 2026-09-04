"""Database metadata, naming conventions and schema namespaces.

Two decisions here that are cheap now and expensive later.

**Constraint names are generated, not left to PostgreSQL.** Without a naming convention,
Alembic cannot autogenerate a downgrade for a constraint it did not name, because it has
no idea what PostgreSQL called it. Migrations then become one-way, and a failed deploy has
no way back.

**Every table lives in a named schema, never `public`.** The schema is what row-level
security policies and the grant sweeps are written against, and `public` is where anything
that forgets to say otherwise ends up. A table in `public` is a table nobody decided the
classification of.

Task ids: M0.3.2, M0.3.7
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

#: Deterministic constraint names, so a migration can always be reversed.
#: `ix_` index, `uq_` unique, `ck_` check, `fk_` foreign key, `pk_` primary key.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_N_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

#: The nine namespaces. Each is a classification boundary, not a folder.
SCHEMAS: dict[str, str] = {
    "auth": "identity, principals, sessions",
    "gate": "capabilities, grants, scopes, entitlement cache",
    "agent": "agents, templates, skills, leashes, artifacts",
    "know": "knowledge items, chunks, embeddings",
    "mem": "the three memory kinds",
    "obs": "the metadata ledger and traces",
    "proj": "projected connector fields, never payloads",
    "er": "entity resolution: candidates, merges, pre-images",
    "ops": "scheduled jobs, budgets, deployment records",
}

#: Extensions the schema depends on. pgvector for embeddings; pg_trgm for the trigram
#: similarity that entity resolution scores names with; fuzzystrmatch for phonetic
#: comparison; unaccent so "Rané" and "Rane" match before either reaches a human.
EXTENSIONS = ("vector", "pg_trgm", "fuzzystrmatch", "unaccent")

metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Base(DeclarativeBase):
    """Declarative base carrying the naming convention."""

    metadata = metadata


def normalise_database_url(url: str) -> str:
    """Point a plain postgres URL at psycopg 3.

    `postgresql://` is what every operator, tutorial and other tool writes, and SQLAlchemy
    maps it to psycopg2 — a driver this project does not install. The failure is
    `ModuleNotFoundError: No module named 'psycopg2'`, which reads like a missing
    dependency rather than a URL scheme, so it sends you to pyproject.toml instead of to
    the connection string.

    Rather than require every deployment to spell the driver correctly, accept the form
    people actually write.

    Task ids: M0.3.2
    """
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url
