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

import re
import sys
from typing import Any

import structlog
from sqlalchemy import create_engine, text

from brain.db import SCHEMAS, normalise_database_url

log = structlog.get_logger()

#: Tables this command owns and will replace. Anything else is left alone.
OWNED = ("auth.principal", "gate.capability_grant")

#: What an ordinary Postgres identifier looks like. A name that does not match is reported
#: rather than interpolated into a query.
_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


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

    Deliberately not a check on the hostname or the database name. Those are exactly the
    things that get copied into a staging config and then lie.

    **Two signals, and either one refuses.** This used to read `n_live_tup` alone, which is
    a statistics estimate rather than a count: it is zero for a freshly restored database
    until autovacuum or ANALYZE has run, so a production dump restored ten minutes ago read
    as empty and this function said the database was safe to truncate. The exact probe fixes
    that. The estimate is kept beside it rather than replaced, because the exact probe has
    its own blind spot: it runs as whoever is connected, and row-level security can hide
    rows from that role while the statistics still count them. Neither signal covers the
    other, so both run and either refuses.

    It also used to check seven schemas while nine exist. `ops` was missing, and so was
    `obs`, which holds the audit ledger. Truncating that is the one thing here that cannot
    be undone by re-running the seeder.
    """
    engine = create_engine(normalise_database_url(url), poolclass=None)
    # Interpolated below, and safe because it is built from a constant in this repository.
    schema_list = ", ".join(f"'{name}'" for name in sorted(SCHEMAS))
    found: dict[str, str] = {}
    list_tables = (
        "SELECT table_schema, table_name FROM information_schema.tables "  # noqa: S608
        f"WHERE table_schema IN ({schema_list}) AND table_type = 'BASE TABLE'"
    )
    count_estimates = (
        "SELECT schemaname, relname, n_live_tup FROM pg_stat_user_tables "  # noqa: S608
        f"WHERE schemaname IN ({schema_list}) AND n_live_tup > 0"
    )
    try:
        with engine.connect() as conn:
            for schema, table in conn.execute(text(list_tables)).all():
                qualified = f"{schema}.{table}"
                if qualified in OWNED:
                    continue
                if not _IDENTIFIER_RE.match(schema) or not _IDENTIFIER_RE.match(table):
                    # A table name is not a literal just because the database supplied it.
                    # Anything that cannot be an ordinary lowercase identifier is reported
                    # rather than interpolated. Refusing to seed is the safe outcome, and a
                    # name that odd is worth a person looking at anyway.
                    found[qualified] = "name is not an ordinary identifier"
                    continue
                # Exact, and stops at the first row. `count(*)` would read the whole table
                # for an answer that only needs to be "any". The identifiers are validated
                # immediately above, which is what makes this interpolation safe.
                probe = f'SELECT 1 FROM "{schema}"."{table}" LIMIT 1'  # noqa: S608
                if conn.execute(text(probe)).first():
                    found[qualified] = "holds rows"

            for schema, table, estimate in conn.execute(text(count_estimates)).all():
                qualified = f"{schema}.{table}"
                if qualified not in OWNED:
                    found.setdefault(qualified, f"~{estimate} rows by statistics")
    finally:
        engine.dispose()

    if found:
        listed = ", ".join(f"{t} ({why})" for t, why in sorted(found.items())[:5])
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
