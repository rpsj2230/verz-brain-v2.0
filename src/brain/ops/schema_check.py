"""Does the database have the schemas the code expects?

A one-line shell check lived in two CI jobs and hard-coded both the list and the count:

    n=$(psql ... "nspname in ('auth','gate',...)"); [ "$n" = "9" ] || fail

Three things were wrong with it, and only the third is about shell.

**The list was written out three times** - twice here and once inside `sweep_rls` - and the
three had already drifted: the sweep's copy was missing `ops`, so a table with no row-level
security in that schema passed the check whose whole purpose is to find one.

**A hard-coded count fails on a correct change.** Adding `chat` made it say "expected 9,
found 10", which is a green-to-red transition caused by the change being right. A test that
fails when you do the right thing is a test somebody updates without reading, and the next
time it fails for a real reason they update it again.

**And a check written in YAML cannot be tested.** This one can be, and is.

Task ids: M0.3.1
"""

from __future__ import annotations

import sys

from brain.db import SCHEMAS


def missing_schemas(present: set[str]) -> tuple[str, ...]:
    """Schemas the code expects that the database does not have.

    One direction only, deliberately. An *extra* schema in the database is not a failure:
    `public` is always there, a client may have their own, and PostgreSQL creates
    `information_schema`. Refusing those would make this a check about the database being
    exactly what we imagined rather than about the migration having run.
    """
    return tuple(sorted(set(SCHEMAS) - present))


def check(url: str) -> int:
    """0 when every expected schema exists, 1 otherwise. Prints what is missing."""
    import psycopg

    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT nspname FROM pg_namespace WHERE nspname = ANY(%(names)s)",
            {"names": sorted(SCHEMAS)},
        )
        present = {row[0] for row in cur.fetchall()}

    missing = missing_schemas(present)
    if missing:
        # `::error::` so GitHub annotates the line rather than burying it in a log.
        print(f"::error::missing schema(s): {', '.join(missing)}", file=sys.stderr)
        print(f"expected {len(SCHEMAS)}: {', '.join(sorted(SCHEMAS))}", file=sys.stderr)
        return 1
    print(f"ok: all {len(SCHEMAS)} schemas present ({', '.join(sorted(SCHEMAS))})")
    return 0


def main() -> int:
    """`python -m brain.ops.schema_check [database-url]`.

    Falls back to the environment when no argument is given, and that is what makes it
    runnable inside the container. `docker compose exec app ... "$DATABASE_URL"` expands the
    variable on the *host*, which does not have it, so the container would be handed an
    empty string and would report a usage error that reads like a broken command. Reading
    the environment inside the process asks the right machine.

    Both names, because `brain.app.Settings` accepts both and a check that disagreed with
    the application about where the database is would be a check about a different database.
    """
    import os

    url = (
        (sys.argv[1] if len(sys.argv) > 1 else "")
        or os.environ.get("DATABASE_URL", "")
        or os.environ.get("BRAIN_DATABASE_URL", "")
    )
    if not url:
        print(
            "no database url: pass one, or set DATABASE_URL or BRAIN_DATABASE_URL",
            file=sys.stderr,
        )
        return 2
    return check(url)


if __name__ == "__main__":
    raise SystemExit(main())
