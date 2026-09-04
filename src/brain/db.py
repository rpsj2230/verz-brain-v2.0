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

from datetime import datetime

from sqlalchemy import DateTime, MetaData, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

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


class TimestampMixin:
    """created_at and updated_at, set by the database rather than the application.

    `server_default=func.now()` and `onupdate` mean the times come from one clock. An
    application-set timestamp is the clock of whichever container handled the write, and
    on a box running thirty containers those drift — which makes an audit ledger ordered
    by application time subtly wrong exactly when it matters.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class SoftDeleteMixin:
    """Rows are retired, not removed.

    A hard delete destroys the audit trail for the thing deleted, and the audit trail is
    the whole product. `deleted_at` is null for live rows; every query that forgets to
    filter it is a bug that row-level security also catches, which is the point of having
    both.
    """

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    @property
    def is_live(self) -> bool:
        return self.deleted_at is None


class AuditMixin:
    """Who did this, and under which entitlement.

    `ent_hash` rather than a list of capabilities: the hash is stable, fixed-width, and
    identifies the reach without recording what the reach was. A ledger holding the
    capabilities themselves becomes a map of who can see what, which is a document you do
    not want to have.
    """

    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    ent_hash: Mapped[str | None] = mapped_column(String(32), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
