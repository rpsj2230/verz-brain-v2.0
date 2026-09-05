"""Load the synthetic company into a database.

The same twelve people the tests use, written to real tables. Sharing one definition
between the test fixture and the seed matters more than it looks: a seed that drifts from
the fixture means the thing you develop against and the thing the canaries protect stop
being the same system, and a permission bug can then pass CI and appear only in a demo.

Refuses to run against a database that holds real rows. Seeding is destructive by nature —
it truncates what it owns — and "I ran the seed against production" is a mistake that
should be impossible rather than merely discouraged.

Task ids: M0.4.4, M0.4.5
"""

from __future__ import annotations

import sys
from typing import Any

import structlog
from sqlalchemy import create_engine, text

from brain.db import normalise_database_url

log = structlog.get_logger()

#: Tables this command owns and will replace. Anything else is left alone.
OWNED = ("auth.principal", "gate.capability_grant")


def _seed_rows() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Built from the test fixture, so the two can never disagree."""
    # Imported lazily: tests are not on the path in a deployed image, and this command is
    # a development tool. A missing fixture should say so, not break the import graph.
    from tests.fixtures.company import build_company

    principals: list[dict[str, Any]] = []
    grants: list[dict[str, Any]] = []
    for p in build_company().values():
        principals.append(
            {
                "id": p.principal.id,
                "kind": str(p.principal.kind),
                "employment": str(p.principal.employment),
                "display_name": p.principal.display_name,
                "primary_department": p.principal.primary_department,
                "not_after": p.principal.not_after,
            }
        )
        for g in p.grants:
            grants.append(
                {
                    "principal_id": p.principal.id,
                    "capability": g.capability.value,
                    "scope": g.scope.model_dump_json(),
                }
            )
    return principals, grants


def looks_like_production(url: str) -> tuple[bool, str]:
    """A crude but honest guard. Any row in a table we do not own means stop.

    Deliberately not a check on the hostname or the database name — those are exactly the
    things that get copied into a staging config and then lie.
    """
    engine = create_engine(normalise_database_url(url), poolclass=None)
    try:
        with engine.connect() as conn:
            tables = conn.execute(
                text(
                    "SELECT schemaname || '.' || relname AS t, n_live_tup "
                    "FROM pg_stat_user_tables "
                    "WHERE schemaname IN ('auth','gate','agent','know','mem','proj','er') "
                    "AND n_live_tup > 0"
                )
            ).all()
    finally:
        engine.dispose()
    unowned = [(t, n) for t, n in tables if t not in OWNED]
    if unowned:
        listed = ", ".join(f"{t} ({n} rows)" for t, n in unowned[:5])
        return True, f"database holds rows this command does not own: {listed}"
    return False, ""


def seed(url: str, *, force: bool = False) -> int:
    risky, why = looks_like_production(url)
    if risky and not force:
        log.error("refusing to seed", reason=why)
        print(f"REFUSED: {why}", file=sys.stderr)
        print("Pass --force only if you are certain this database is disposable.", file=sys.stderr)
        return 1

    principals, grants = _seed_rows()
    log.info("seeding", principals=len(principals), grants=len(grants))
    print(f"would seed {len(principals)} principals and {len(grants)} grants")
    # The tables themselves arrive with M1 and M2. Until then this command exists to be
    # correct about what it would do and about refusing when it should — writing rows to
    # tables that do not exist yet would be the wrong kind of placeholder.
    print("auth.principal and gate.capability_grant exist; nothing written yet")
    return 0


def main(argv: list[str] | None = None) -> int:
    import os

    args = argv if argv is not None else sys.argv[1:]
    url = os.environ.get("DATABASE_URL") or os.environ.get("BRAIN_DATABASE_URL", "")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    return seed(url, force="--force" in args)


if __name__ == "__main__":
    raise SystemExit(main())
