"""Database metadata and connection-string handling.

Task ids: M0.3.2, M0.3.7, M31.2.1.1
"""

from __future__ import annotations

import pytest

from brain.db import (
    EXTENSIONS,
    NAMING_CONVENTION,
    SCHEMAS,
    Base,
    libpq_url,
    metadata,
    normalise_database_url,
)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("postgresql://u:p@h:5432/d", "postgresql+psycopg://u:p@h:5432/d"),
        ("postgres://u:p@h:5432/d", "postgresql+psycopg://u:p@h:5432/d"),
        # already explicit: left alone
        ("postgresql+psycopg://u:p@h/d", "postgresql+psycopg://u:p@h/d"),
        # a different driver is a deliberate choice, not a mistake to correct
        ("postgresql+asyncpg://u:p@h/d", "postgresql+asyncpg://u:p@h/d"),
    ],
)
def test_urls_are_pointed_at_psycopg_three(given: str, expected: str) -> None:
    assert normalise_database_url(given) == expected


def test_a_password_containing_the_scheme_is_not_mangled() -> None:
    """Only the leading scheme is replaced, never a match inside the credentials."""
    url = "postgresql://u:postgresql://@h/d"
    assert normalise_database_url(url) == "postgresql+psycopg://u:postgresql://@h/d"


def test_every_schema_is_named_and_described() -> None:
    """Each namespace is a classification boundary. A table in `public` is a table nobody
    decided the classification of, and a schema with no description is a boundary nobody
    stated the meaning of - which is how the tenth one gets added for convenience.

    Deliberately not asserting a count. The number changed from nine to ten when `chat`
    arrived, and a hard-coded count is a test that fails on a correct change and gets
    updated without thought - which is exactly what it did in CI, where the same number was
    written out twice more. What matters is that every schema is named and described, and
    that `public` is not one of them."""
    assert SCHEMAS, "there are no schemas at all"
    assert "public" not in SCHEMAS
    assert all(desc for desc in SCHEMAS.values())
    # The one property a count was standing in for: nothing may be added without a note
    # saying what belongs in it.
    assert all(len(desc) > 10 for desc in SCHEMAS.values()), "a schema has a token description"


def test_the_extensions_entity_resolution_depends_on_are_declared() -> None:
    assert set(EXTENSIONS) == {"vector", "pg_trgm", "fuzzystrmatch", "unaccent"}


def test_metadata_carries_the_naming_convention() -> None:
    """Without it Alembic cannot generate a downgrade for a constraint it did not name,
    so migrations become one-way and a failed deploy has no way back."""
    assert metadata.naming_convention == NAMING_CONVENTION
    assert set(NAMING_CONVENTION) == {"ix", "uq", "ck", "fk", "pk"}


# ------------------------------------------ the base every model inherits (M31.2.1.1)
def test_the_declarative_base_is_the_metadata_that_carries_the_convention() -> None:
    """The convention is only worth having if the base the models actually inherit from is
    the one holding it. `DeclarativeBase` builds its own `MetaData` when a subclass assigns
    none, and that default carries a naming convention for indexes alone.

    So deleting the one assignment in `brain.db.Base` is not a visible regression: the models
    still map, a test reading `metadata` directly still passes, and every table quietly moves
    onto a second `MetaData` that names almost nothing. This asserts the two are one object,
    which is the thing that would stop being true.
    """
    assert Base.metadata is metadata
    assert Base.metadata.naming_convention == NAMING_CONVENTION


def test_every_constraint_on_every_table_has_a_generated_name() -> None:
    """The behaviour the convention exists for, asserted on the real tables rather than on
    the dictionary of format strings that is supposed to produce them.

    An unnamed check or foreign key is one PostgreSQL names for us, and PostgreSQL's choice is
    not knowable from the models, so Alembic cannot write the `DROP CONSTRAINT` that reverses
    it. The migration is then one-way, which is discovered at the moment somebody needs the
    way back.

    Deleting this leaves the convention asserted only as a dictionary, and a format string
    that never reaches a constraint proves nothing. It also catches the other way this breaks:
    a table declared on some other base, which inherits none of this and arrives here with
    `None` where its names should be.
    """
    import brain.tables as tables

    # Importing the package is what puts every model on `Base.metadata`. Asserted rather than
    # assumed, because an empty table list would make everything below pass while checking
    # nothing, which is how the traceability sweep spent its whole life green.
    assert tables.AuditEntryRow.__table__ in Base.metadata.sorted_tables
    assert len(Base.metadata.sorted_tables) > 10

    prefixes = {
        "PrimaryKeyConstraint": "pk_",
        "ForeignKeyConstraint": "fk_",
        "UniqueConstraint": "uq_",
        "CheckConstraint": "ck_",
    }
    wrong: list[str] = []
    for table in Base.metadata.sorted_tables:
        for constraint in table.constraints:
            expected = prefixes.get(type(constraint).__name__)
            if expected is None:
                continue
            name = str(constraint.name or "")
            if not name.startswith(expected):
                wrong.append(f"{table.fullname}: {type(constraint).__name__} named {name!r}")
        for index in table.indexes:
            # `uq_` as well as `ix_`, and the exception is real rather than a loosening. A
            # partial unique index carries a WHERE clause, a naming convention cannot express
            # one, so those are named by hand and named as what they are: a uniqueness rule.
            name = str(index.name or "")
            if not name.startswith(("ix_", "uq_")):
                wrong.append(f"{table.fullname}: index named {name!r}")
    assert not wrong, wrong


# ------------------------------------------------- the URL psycopg itself can read
@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (
            "postgresql+psycopg://brain:pw@pgbouncer:5432/brain",
            "postgresql://brain:pw@pgbouncer:5432/brain",
        ),
        ("postgresql+asyncpg://brain@host/brain", "postgresql://brain@host/brain"),
        ("postgresql://brain:pw@host:5432/brain", "postgresql://brain:pw@host:5432/brain"),
        ("postgres://brain@host/brain", "postgres://brain@host/brain"),
        ("host=db dbname=brain user=brain", "host=db dbname=brain user=brain"),
    ],
)
def test_a_sqlalchemy_url_is_turned_into_one_psycopg_can_read(given: str, expected: str) -> None:
    """The bug this exists for reached CI and blocked a deploy.

    Every deployment writes `DATABASE_URL` in SQLAlchemy's form, because that is what the
    engine wants. Anything that opens a connection *without* SQLAlchemy gets handed the same
    string, and `psycopg.connect("postgresql+psycopg://...")` fails with `missing "=" after
    ...` - an error about keyword/value syntax, which sends the reader to look at the
    password rather than at the scheme.

    The keyword/value form is passed through untouched, because libpq accepts that too and
    it is already what psycopg wants.

    Delete this and `normalise_database_url` still has a test, and the direction that
    actually broke has none."""
    assert libpq_url(given) == expected


def test_the_two_normalisers_are_inverses_on_the_form_operators_write() -> None:
    """`normalise_database_url` exists because people write `postgresql://` and SQLAlchemy
    maps that to a driver this project does not install. `libpq_url` exists because psycopg
    cannot read what that produces. A round trip has to land back where it started, or one
    of the two is rewriting something it should not."""
    written = "postgresql://brain:pw@pgbouncer:5432/brain"

    assert libpq_url(normalise_database_url(written)) == written


def test_psycopg_actually_accepts_what_libpq_url_produces() -> None:
    """Asserted against psycopg's own parser rather than against a string I expect.

    A test comparing to a hand-written expected value proves the function does what I wrote
    down; this proves it does what psycopg needs, which is the thing that failed. `psycopg`
    is a dependency of this project, so there is no reason to assert on a proxy."""
    import psycopg.conninfo

    parsed = psycopg.conninfo.conninfo_to_dict(
        libpq_url("postgresql+psycopg://brain:pw@pgbouncer:5432/brain")
    )

    assert parsed["host"] == "pgbouncer"
    assert parsed["dbname"] == "brain"


def test_every_direct_psycopg_connection_goes_through_the_converter() -> None:
    """The call sites, not the function. `libpq_url` being correct helps nothing if the next
    module to open a connection passes the raw setting, which is exactly what happened: the
    function did not exist, two sweeps and the schema check each called
    `psycopg.connect(url)`, and the whole-stack CI job was the only thing that noticed.

    Asserted on the source because there is no behaviour to observe without a database, and
    the sweeps skip when `DATABASE_URL` is unset - which is every run on a laptop."""
    import inspect

    from brain.ops import schema_check, sweeps

    for module in (schema_check, sweeps):
        source = inspect.getsource(module)
        for line in source.splitlines():
            if "psycopg.connect(" in line and "libpq_url" not in line:
                assert "_needs_db()" in source, (
                    f"{module.__name__} calls psycopg.connect on a URL that never passed "
                    f"through libpq_url: {line.strip()}"
                )
