"""Running migrations at startup, safely, with more than one replica.

Migrations used to run as a separate one-shot container. That worked, but it exits when
it succeeds, and Coolify has no way to be told a container is *meant* to stop — so a
successful migration displayed as a red "Exited" next to three healthy services, forever.
A status anyone has to remember is fine is a status that will eventually be believed.

So migrations now run during application startup, before readiness passes. The obvious
objection is the race: two replicas starting together would both try to migrate. That is
handled by a PostgreSQL advisory lock rather than by hoping. The first replica takes the
lock and migrates; the others block until it finishes, then find nothing to do. The lock
is held on a session and released automatically if that process dies, so a replica killed
mid-migration does not wedge the deployment.

The trade-off, stated because it is real: a failed migration now fails startup, so the
application refuses to serve rather than serving against a schema it does not match. That
is the behaviour we want — the alternative is answering questions from a half-migrated
database — but it does mean a bad migration takes the app down rather than just failing a
job. The invariant suite and the CI stack test exist to catch that before it deploys.

Task ids: M0.3.2, M31.1.1
"""

from __future__ import annotations

from pathlib import Path

import structlog
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

from brain.db import normalise_database_url

log = structlog.get_logger()

REPO = Path(__file__).resolve().parents[2]

#: Any constant works; it only has to be the same in every replica. Chosen once and never
#: changed, because changing it would let an old and a new replica migrate concurrently.
MIGRATION_LOCK_ID = 8_274_419_003


def _alembic_config(url: str) -> Config:
    cfg = Config(str(REPO / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def pending_revisions(url: str) -> list[str]:
    """Revisions the database has not applied yet. Empty means up to date."""
    cfg = _alembic_config(url)
    script = ScriptDirectory.from_config(cfg)
    engine = create_engine(url, poolclass=None)
    try:
        with engine.connect() as conn:
            current = MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()
    head = script.get_current_head()
    if current == head:
        return []
    return [rev.revision for rev in script.iterate_revisions(head, current) if rev.revision]


def run_migrations(database_url: str) -> list[str]:
    """Bring the database to head. Returns the revisions applied, newest first.

    Safe to call from every replica simultaneously.
    """
    url = normalise_database_url(database_url)
    pending = pending_revisions(url)
    if not pending:
        log.info("migrations up to date")
        return []

    log.info("migrations pending", count=len(pending), revisions=pending)
    engine = create_engine(url, poolclass=None)
    try:
        with engine.connect() as conn:
            # Blocking, not try-lock: a replica that loses the race must wait for the
            # winner rather than start serving against an unmigrated schema.
            conn.execute(text("SELECT pg_advisory_lock(:id)"), {"id": MIGRATION_LOCK_ID})
            conn.commit()
            try:
                # Re-check inside the lock. The replica that waited will usually find the
                # work already done, and running `upgrade` regardless would be harmless
                # but would log a migration that did not happen.
                still_pending = pending_revisions(url)
                if not still_pending:
                    log.info("migrations applied by another replica")
                    return []
                command.upgrade(_alembic_config(url), "head")
                log.info("migrations applied", revisions=still_pending)
                return still_pending
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": MIGRATION_LOCK_ID})
                conn.commit()
    finally:
        engine.dispose()
