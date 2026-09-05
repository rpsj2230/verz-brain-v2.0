"""Database metadata and connection-string handling.

Task ids: M0.3.2, M0.3.7, M31.2.1.1
"""

from __future__ import annotations

import pytest

from brain.db import EXTENSIONS, NAMING_CONVENTION, SCHEMAS, Base, metadata, normalise_database_url


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
