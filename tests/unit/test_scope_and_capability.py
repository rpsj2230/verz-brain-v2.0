"""Predicate evaluation, SQL rendering, and the capability grammar.

Task ids: M0.2.2, M0.2.3
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from brain.core.entitlement import Capability
from brain.core.scope import Clause, Op, Scope


# ------------------------------------------------------------- evaluation
def test_any_matches_anything_including_an_empty_row() -> None:
    c = Clause(field="department", op=Op.ANY)
    assert c.matches({})
    assert c.matches({"department": "maintenance"})


def test_in_matches_membership_only() -> None:
    c = Clause(field="department", op=Op.IN, value=("maintenance", "web"))
    assert c.matches({"department": "web"})
    assert not c.matches({"department": "sales"})


def test_prefix_matches_the_start_of_a_value() -> None:
    """Used for hierarchical scopes like `web.projects` under `web`."""
    c = Clause(field="scope_path", op=Op.PREFIX, value="web.")
    assert c.matches({"scope_path": "web.projects"})
    assert not c.matches({"scope_path": "sales.pipeline"})


def test_values_are_compared_as_strings() -> None:
    """Rows arrive from jsonb, where a number may surface as either. Comparing as strings
    keeps a client id matching whether it came back as 4471 or "4471"."""
    c = Clause(field="client_id", op=Op.EQ, value="4471")
    assert c.matches({"client_id": 4471})


def test_field_names_must_be_lowercase_identifiers() -> None:
    """The field name reaches SQL as a jsonb key, so it is constrained rather than quoted."""
    for bad in ("Department", "department name", "department;drop", ""):
        with pytest.raises(ValidationError):
            Clause(field=bad, op=Op.EQ, value="x")


# ---------------------------------------------------------------- to_sql
def test_any_renders_as_true() -> None:
    sql, params = Clause(field="department", op=Op.ANY).to_sql("p")
    assert sql == "TRUE"
    assert params == {}


def test_in_renders_as_a_parameterised_array() -> None:
    sql, params = Clause(field="department", op=Op.IN, value=("a", "b")).to_sql("p")
    assert "= ANY(:p)" in sql
    assert params == {"p": ["a", "b"]}


def test_prefix_renders_as_like_with_the_wildcard_in_the_parameter() -> None:
    """The `%` goes in the value, never in the SQL string — otherwise a value containing
    `%` would change the shape of the query."""
    sql, params = Clause(field="scope_path", op=Op.PREFIX, value="web.").to_sql("p")
    assert "LIKE :p" in sql
    assert params == {"p": "web.%"}


def test_multiple_clauses_render_as_a_conjunction_with_distinct_parameters() -> None:
    s = Scope(
        clauses=(
            Clause(field="department", op=Op.EQ, value="maintenance"),
            Clause(field="tier", op=Op.EQ, value="managed"),
        )
    )
    sql, params = s.to_sql()
    assert sql.count("AND") == 1
    assert len(params) == 2


def test_a_custom_parameter_prefix_avoids_collisions() -> None:
    _, params = Scope.department("maintenance").to_sql("caller")
    assert list(params) == ["caller0"]


# -------------------------------------------------------- is_unrestricted
def test_empty_scope_is_unrestricted() -> None:
    assert Scope.unrestricted().is_unrestricted()


def test_a_scope_of_only_any_clauses_is_unrestricted() -> None:
    assert Scope(clauses=(Clause(field="department", op=Op.ANY),)).is_unrestricted()


def test_a_scope_with_a_real_clause_is_restricted() -> None:
    assert not Scope.department("maintenance").is_unrestricted()


# ------------------------------------------------------------- capability
def test_capability_grammar_accepts_the_real_shapes() -> None:
    for good in (
        "read:client",
        "read:client.name",
        "read:client.hours_remaining",
        "read:client.*",
        "write:ticket.status",
        "invoke:agent",
        "approve:envelope",
        "admin:grant",
    ):
        assert Capability(value=good).value == good


def test_capability_grammar_rejects_malformed_strings() -> None:
    for bad in ("read", "read:", ":client", "Read:client", "read:Client", "read client"):
        with pytest.raises(ValidationError):
            Capability(value=bad)


def test_unknown_verbs_are_rejected() -> None:
    """A typo like `reed:client.name` would otherwise create a capability nobody holds and
    nothing grants — a permanently silent refusal that looks like missing data."""
    with pytest.raises(ValidationError, match="unknown verb"):
        Capability(value="reed:client.name")


def test_verb_and_noun_are_readable_off_the_capability() -> None:
    c = Capability(value="read:client.hours_remaining")
    assert c.verb == "read"
    assert c.noun == "client"


def test_verb_and_noun_work_without_a_field() -> None:
    c = Capability(value="invoke:agent")
    assert c.verb == "invoke"
    assert c.noun == "agent"


def test_a_wildcard_does_not_cross_the_entity_boundary() -> None:
    """`read:client.*` must not reach `read:client_secret.value`."""
    assert not Capability(value="read:client.*").covers(Capability(value="read:client_secret.x"))


def test_a_narrower_capability_does_not_cover_a_wider_one() -> None:
    assert not Capability(value="read:client.name").covers(Capability(value="read:client.*"))
