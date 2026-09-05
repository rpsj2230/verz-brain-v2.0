"""Database metadata and connection-string handling.

Task ids: M0.3.2, M0.3.7
"""

from __future__ import annotations

import pytest

from brain.db import EXTENSIONS, NAMING_CONVENTION, SCHEMAS, metadata, normalise_database_url


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
