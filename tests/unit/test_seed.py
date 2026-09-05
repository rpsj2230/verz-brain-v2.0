"""The seed command, and the guard that stops it running somewhere real.

Task ids: M0.4.4, M0.4.5, M38.1.4.4

M38.1.4.4 asks for seed data in staging and never production data. That is a property of
this command rather than of the staging compose file, because the compose file describes a
stack and this is the only thing that writes rows into one. The guard is therefore tested
through `looks_like_production` itself, against a connection that answers the way a real
database would, rather than through `seed` with the guard replaced.
"""

from __future__ import annotations

from collections.abc import Collection
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest

from brain import seed as seed_mod


def test_seed_rows_come_from_the_same_fixture_the_canaries_use() -> None:
    """One definition, two consumers. If the seed and the fixture drifted, the thing you
    develop against and the thing the canaries protect would stop being the same system,
    and a permission bug could pass CI and appear only in a demo."""
    principals, grants = seed_mod._seed_rows()
    assert len(principals) == 12
    assert len(grants) > 20

    ids = {p["id"] for p in principals}
    assert "u_weiling" in ids  # sees-record-not-money
    assert "u_expired" in ids  # the lapsed contractor
    assert "svc_sentinel" in ids  # scheduled work is a principal too

    # the expiry travels into the seeded row, not just the fixture object
    expired = next(p for p in principals if p["id"] == "u_expired")
    assert expired["not_after"] is not None


def test_grants_carry_their_scope_as_json() -> None:
    _, grants = seed_mod._seed_rows()
    weiling = [g for g in grants if g["principal_id"] == "u_weiling"]
    assert weiling
    assert all("clauses" in g["scope"] for g in weiling)
    # and the money field is absent, which is the whole point of that persona
    assert not any("contract_value" in g["capability"] for g in weiling)


def test_the_owned_table_list_is_explicit() -> None:
    """Seeding truncates what it owns. What it owns is written down rather than inferred,
    so widening it is a visible edit."""
    # Corrected 5 September: the table M1.4.1 actually creates is `capability_grant`.
    # `gate.grant` named nothing, and `looks_like_production` counts rows in tables it
    # does not own, so the first real grant row would have made the seeder refuse to run.
    assert seed_mod.OWNED == ("auth.principal", "gate.capability_grant")


def test_seed_refuses_when_the_database_holds_rows_it_does_not_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'I ran the seed against production' should be impossible, not discouraged."""
    monkeypatch.setattr(
        seed_mod, "looks_like_production", lambda _url: (True, "know.item (4,102 rows)")
    )
    assert seed_mod.seed("postgresql://x", force=False) == 1


def test_force_overrides_the_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seed_mod, "looks_like_production", lambda _url: (True, "whatever"))
    assert seed_mod.seed("postgresql://x", force=True) == 0


def test_an_empty_database_seeds_without_force(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seed_mod, "looks_like_production", lambda _url: (False, ""))
    assert seed_mod.seed("postgresql://x") == 0


def test_main_needs_a_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("BRAIN_DATABASE_URL", raising=False)
    assert seed_mod.main([]) == 2


# --------------------------------------------- the guard reads more than an estimate
def test_the_guard_does_not_rely_on_statistics_alone() -> None:
    """`n_live_tup` is an estimate maintained by the statistics collector, not a count. It
    is zero for a freshly restored database until autovacuum or ANALYZE has run, so a
    production dump restored ten minutes ago read as empty and this guard said it was safe
    to truncate.

    Asserted by reading the source, because the failure needs a real Postgres with cold
    statistics and a table full of rows, which no unit test can stand up. What a test can do
    is fail the moment somebody removes the exact probe and leaves the estimate.
    """
    source = (Path(__file__).resolve().parents[2] / "src" / "brain" / "seed.py").read_text(
        encoding="utf-8"
    )
    assert "LIMIT 1" in source, "the exact probe is gone; only the estimate remains"
    assert "n_live_tup" in source, "the estimate is gone; it covers a blind spot in the probe"


# ------------------------------- the guard, driven rather than described (M38.1.4.4)
class _Result:
    """What `Connection.execute` hands back, in the two shapes this module reads."""

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[Any, ...]]:
        return self._rows

    def first(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class _Connection:
    """A database that answers the three questions the guard asks.

    A fake rather than a real Postgres, because the case that matters most cannot be built
    against a real one in a unit test: a freshly restored snapshot whose statistics are still
    cold. Here that is two lines of setup and it is the exact shape the bug had.
    """

    def __init__(self, tables: list[tuple[str, str]], populated: set[str], stats: set[str]) -> None:
        self.tables = tables
        self.populated = populated
        self.stats = stats

    def execute(self, statement: object) -> _Result:
        sql = str(statement)
        if "information_schema.tables" in sql:
            return _Result(list(self.tables))
        if "pg_stat_user_tables" in sql:
            return _Result([(*name.split("."), 4102) for name in sorted(self.stats)])
        # A row probe. The guard builds it as `SELECT 1 FROM "schema"."table" LIMIT 1`.
        quoted = sql.split("FROM ")[1].split(" LIMIT")[0]
        name = quoted.replace('"', "")
        return _Result([(1,)] if name in self.populated else [])


def _database(
    monkeypatch: pytest.MonkeyPatch,
    tables: list[tuple[str, str]],
    *,
    populated: Collection[str] = (),
    stats: Collection[str] = (),
) -> None:
    connection = _Connection(tables, set(populated), set(stats))

    class _Engine:
        def connect(self) -> Any:
            return nullcontext(connection)

        def dispose(self) -> None:
            return None

    monkeypatch.setattr(seed_mod, "create_engine", lambda *_a, **_k: _Engine())


def test_a_database_holding_rows_the_seed_does_not_own_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The leaf itself: seeding is destructive, so "I ran the seed against production" has to
    be impossible rather than discouraged. Staging gets seed data because this refuses to run
    anywhere that already holds something.

    Driven through the real guard rather than through `seed` with the guard replaced. The
    replaced version tests that `seed` honours an answer; this tests that the answer is right,
    and the answer is the whole leaf. Deleting it leaves the refusal asserted only against a
    stub, which passes with the guard's body deleted.
    """
    _database(
        monkeypatch,
        [("auth", "principal"), ("know", "item")],
        populated={"know.item"},
    )
    risky, why = seed_mod.looks_like_production("postgresql://x")
    assert risky
    assert "know.item" in why


def test_a_database_holding_only_what_the_seed_owns_is_seedable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The positive half, and it is not decoration. A guard tested only by its refusals is
    satisfied by one that refuses everything, and a seeder that refuses every database is a
    seeder somebody runs with `--force` by habit - which disables the refusal that matters
    along with the one that does not.

    `auth.principal` is full here and the answer is still yes, because re-seeding a stack this
    command already owns is the normal case.
    """
    _database(
        monkeypatch,
        [("auth", "principal"), ("gate", "capability_grant")],
        populated={"auth.principal", "gate.capability_grant"},
    )
    assert seed_mod.looks_like_production("postgresql://x") == (False, "")


def test_a_snapshot_restored_minutes_ago_is_refused_even_though_its_statistics_are_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug that actually happened, as a behaviour rather than as a source-code check.

    `n_live_tup` is an estimate maintained by the statistics collector, so it is zero for a
    freshly restored database until autovacuum or ANALYZE has run. A production dump restored
    ten minutes ago therefore read as empty, and the guard said it was safe to truncate.

    Deleting this leaves that path covered only by a test asserting the string `LIMIT 1`
    appears in the file, which passes for a probe that is built and never executed.
    """
    _database(
        monkeypatch,
        [("obs", "audit_entry")],
        populated={"obs.audit_entry"},
        stats=set(),
    )
    risky, why = seed_mod.looks_like_production("postgresql://x")
    assert risky, "a restored snapshot with cold statistics read as an empty database"
    assert "holds rows" in why


def test_a_table_whose_name_is_not_an_identifier_stops_the_seed_rather_than_being_queried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard interpolates a schema and table name into a probe, so a name that is not an
    ordinary identifier is reported instead of being run. Refusing to seed is the safe
    outcome, and a name that odd is worth a person looking at.

    Deleting this makes the validation removable without a test noticing, and what it guards
    is a string from the database reaching a query as though it were a literal.
    """
    _database(monkeypatch, [("know", 'item"; drop table x --')], populated=set())
    risky, why = seed_mod.looks_like_production("postgresql://x")
    assert risky
    assert "not an ordinary identifier" in why


def test_nothing_in_this_repository_copies_a_database_from_one_stack_to_another() -> None:
    """The other half of "never production data". The guard stops the seeder; this stops the
    shortcut that goes around it, which is a restore into staging to reproduce a bug.

    The moment that is possible somebody does it, and staging runs with weaker limits on the
    same network as a test harness. Deleting this lets a convenience script land in `ops/`
    with nothing objecting.
    """
    repo = Path(__file__).resolve().parents[2]
    offenders = [
        f"{path.relative_to(repo)}:{number}"
        for path in sorted((repo / "ops").rglob("*"))
        if path.is_file() and path.suffix != ".md"
        for number, line in enumerate(
            path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1
        )
        if "pg_dump" in line or "pg_restore" in line or "pg_basebackup" in line
    ]
    assert not offenders, f"something copies a database wholesale: {offenders}"


def test_the_guard_looks_in_every_schema_that_exists() -> None:
    """It used to check seven while nine existed. `obs` was one of the two missing, and
    `obs` holds the audit ledger, which is the one thing here that re-running the seeder
    cannot undo."""
    from brain.db import SCHEMAS

    source = (Path(__file__).resolve().parents[2] / "src" / "brain" / "seed.py").read_text(
        encoding="utf-8"
    )
    assert "sorted(SCHEMAS)" in source, (
        "the schema list is hard-coded again; it drifts from brain.db.SCHEMAS the next time "
        "a schema is added, and the one that gets forgotten is the new one"
    )
    assert "obs" in SCHEMAS
