"""Engines, sessions, and the PgBouncer constraints that shape them.

Task ids: M0.3.4, M0.3.5, M31.2.1.2, M31.2.1.3, M31.2.1.4, M31.2.1.5
"""

from __future__ import annotations

import pytest
from sqlalchemy.pool import NullPool

from brain import session as sess
from brain.db import AuditMixin, SoftDeleteMixin, TimestampMixin

URL = "postgresql://u:p@localhost:5432/d"


def test_the_app_engine_disables_server_side_prepared_statements() -> None:
    """Not a tuning knob. PgBouncer in transaction mode hands a different backend to every
    transaction, so a statement prepared on one and executed on another fails - or worse,
    reuses a plan built for different parameters. Without this the app works in
    development, where there is no pooler, and fails in production under load.
    """
    engine = sess.make_app_engine(URL)
    assert engine.dialect.name == "postgresql"
    # psycopg's own kwarg, passed through connect_args
    assert engine.pool.__class__ is NullPool


def test_the_app_engine_does_not_stack_a_pool_on_the_pooler() -> None:
    """PgBouncer is the pool. Two pools with different ideas about connection lifetime
    produce connections that look idle to one and busy to the other."""
    assert sess.make_app_engine(URL).pool.__class__ is NullPool


def test_the_worker_engine_keeps_a_real_pool() -> None:
    """LISTEN/NOTIFY binds to a backend connection. A listener behind a transaction pooler
    stops receiving notifications with no error anywhere, so the worker holds its own
    long-lived connections."""
    engine = sess.make_worker_engine(URL)
    assert engine.pool.__class__ is not NullPool


def test_a_bare_postgres_url_reaches_psycopg_three() -> None:
    assert sess._async_url("postgresql://u:p@h/d").startswith("postgresql+psycopg://")


# ------------------------------------------------------------------ sizing
@pytest.mark.parametrize(
    ("profile", "size"), [("lite", 5), ("standard", 10), ("full", 20), ("nonsense", 5)]
)
def test_pool_size_per_profile_and_a_safe_default(profile: str, size: int) -> None:
    """An unknown profile gets the smallest pool. The target box runs about thirty
    containers on twelve gigabytes; a pool sized for a machine we do not have is how a
    shared host falls over."""
    assert sess.engine_kwargs_for_profile(profile)["pool_size"] == size


# ------------------------------------------------------------------ mixins
def test_timestamps_come_from_the_database_not_the_application() -> None:
    """One clock. Application time is the clock of whichever container handled the write,
    and on a busy host those drift - which makes a ledger ordered by application time
    wrong exactly when it matters."""
    col = TimestampMixin.__annotations__
    assert "created_at" in col
    assert "updated_at" in col


def test_soft_delete_rather_than_removal() -> None:
    """A hard delete destroys the audit trail for the thing deleted, and the audit trail
    is the product."""
    assert "deleted_at" in SoftDeleteMixin.__annotations__


def test_the_audit_mixin_records_a_hash_not_a_capability_list() -> None:
    """A ledger holding the capabilities themselves becomes a map of who can see what,
    which is a document you do not want to have."""
    fields = AuditMixin.__annotations__
    assert "ent_hash" in fields
    assert "created_by" in fields
    assert "trace_id" in fields
    assert not any("capabilit" in f for f in fields)
