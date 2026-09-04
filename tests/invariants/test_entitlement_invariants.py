"""The rules that must never break. A failure here blocks deploy.

Each test carries the invariant id from the delivery document so a failure names the rule
it broke rather than the assertion that noticed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.errors import Absent, Denied, to_public
from brain.core.principal import Employment, Principal, PrincipalKind
from brain.core.scope import Clause, Op, Scope

pytestmark = pytest.mark.invariant


def cap(v: str) -> Capability:
    return Capability(value=v)


def ent(pid: str, *pairs: tuple[str, Scope]) -> EntitlementSet:
    return EntitlementSet(
        principal_id=pid,
        grants=tuple(Grant(capability=cap(c), scope=s) for c, s in pairs),
    )


# ------------------------------------------------------------------ INV-1
def test_inv1_agent_can_only_narrow() -> None:
    """INV-1: E_run(caller, agent) = E(caller) ∩ ceiling. An agent never widens."""
    caller = ent("u1", ("read:client.name", Scope.department("maintenance")))
    ceiling = ent(
        "agent",
        ("read:client.name", Scope.unrestricted()),
        ("read:client.contract_value", Scope.unrestricted()),
    )
    run = caller.intersect(ceiling)

    assert run.holds(cap("read:client.name"))
    # the ceiling offers it; the caller does not hold it, so the run must not have it
    assert not run.holds(cap("read:client.contract_value"))


def test_inv1_ceiling_narrows_a_wide_caller() -> None:
    caller = ent("u1", ("read:client.name", Scope.unrestricted()))
    ceiling = ent("agent", ("read:client.name", Scope.department("maintenance")))
    run = caller.intersect(ceiling)

    scope = run.scope_for(cap("read:client.name"))
    assert scope is not None
    assert scope.matches({"department": "maintenance"})
    assert not scope.matches({"department": "sales"})


def test_inv1_intersection_is_idempotent() -> None:
    caller = ent("u1", ("read:client.name", Scope.department("maintenance")))
    once = caller.intersect(caller)
    twice = once.intersect(caller)
    assert once.ent_hash() == twice.ent_hash()


# ------------------------------------------------------------------ INV-2
def test_inv2_scopes_compose_by_conjunction_only() -> None:
    """INV-2: composing scopes can only narrow. Two grants never combine into a wider one."""
    a = Scope.department("maintenance")
    b = Scope(clauses=(Clause(field="client_tier", op=Op.EQ, value="managed"),))
    both = a.intersect(b)

    assert both.matches({"department": "maintenance", "client_tier": "managed"})
    assert not both.matches({"department": "maintenance", "client_tier": "adhoc"})
    assert not both.matches({"department": "sales", "client_tier": "managed"})


def test_inv2_absent_field_never_satisfies_a_predicate() -> None:
    """A partially projected row must not widen access by omission."""
    s = Scope.department("maintenance")
    assert not s.matches({})
    assert not s.matches({"department": None})


def test_inv2_duplicate_grants_do_not_widen() -> None:
    e = ent(
        "u1",
        ("read:client.name", Scope.department("maintenance")),
        ("read:client.name", Scope(clauses=(Clause(field="tier", op=Op.EQ, value="a"),))),
    )
    scope = e.scope_for(cap("read:client.name"))
    assert scope is not None
    # holding it twice is the conjunction, not the union
    assert scope.matches({"department": "maintenance", "tier": "a"})
    assert not scope.matches({"department": "maintenance", "tier": "b"})


# ------------------------------------------------------------------ INV-3
def test_inv3_entitlements_are_additive_only() -> None:
    """INV-3: a field is hidden because no grant covers it, never because a rule removed it."""
    e = ent("u1", ("read:client.hours_remaining", Scope.department("maintenance")))
    assert e.holds(cap("read:client.hours_remaining"))
    assert not e.holds(cap("read:client.contract_value"))
    # there is no API to subtract; the type exposes no deny list at all
    assert not hasattr(e, "denials")
    assert not hasattr(e, "deny")


def test_inv3_entity_grant_does_not_confer_every_field() -> None:
    """`read:client` must not silently grant `read:client.contract_value`."""
    e = ent("u1", ("read:client", Scope.unrestricted()))
    assert not e.holds(cap("read:client.contract_value"))


def test_inv3_explicit_wildcard_does_confer_fields() -> None:
    e = ent("u1", ("read:client.*", Scope.unrestricted()))
    assert e.holds(cap("read:client.contract_value"))


# ------------------------------------------------------------------ INV-4
def test_inv4_ent_hash_is_order_independent() -> None:
    """INV-4: two identical entitlements built in different orders share a cache key."""
    a = ent(
        "u1",
        ("read:client.name", Scope.department("maintenance")),
        ("read:ticket.status", Scope.department("maintenance")),
    )
    b = ent(
        "u1",
        ("read:ticket.status", Scope.department("maintenance")),
        ("read:client.name", Scope.department("maintenance")),
    )
    assert a.ent_hash() == b.ent_hash()


def test_inv4_ent_hash_differs_on_any_difference() -> None:
    a = ent("u1", ("read:client.name", Scope.department("maintenance")))
    b = ent("u1", ("read:client.name", Scope.department("sales")))
    c = ent("u1", ("read:client.name", Scope.unrestricted()))
    assert len({a.ent_hash(), b.ent_hash(), c.ent_hash()}) == 3


def test_inv4_narrower_caller_never_shares_a_key_with_a_wider_one() -> None:
    wide = ent("u1", ("read:client.*", Scope.unrestricted()))
    narrow = ent("u2", ("read:client.name", Scope.department("maintenance")))
    assert wide.ent_hash() != narrow.ent_hash()


# ------------------------------------------------------------------ INV-5
def test_inv5_denied_is_indistinguishable_from_absent_in_public() -> None:
    """INV-5: an error message must never confirm that a hidden record exists."""
    assert to_public(Denied("client 4471 contract_value")) == to_public(Absent("no such client"))


def test_inv5_detail_is_retained_for_audit() -> None:
    d = Denied("client 4471 contract_value")
    assert "4471" in d.detail
    assert "4471" not in to_public(d)


# ------------------------------------------------------------------ INV-6
def test_inv6_expiry_is_enforced_at_entitlement_time() -> None:
    """INV-6: a session opened before expiry must not survive it."""
    now = datetime.now(UTC)
    p = Principal(
        id="c1",
        kind=PrincipalKind.HUMAN,
        employment=Employment.CONTRACTOR,
        display_name="Contractor",
        not_after=now - timedelta(seconds=1),
    )
    assert not p.is_active(now)


def test_inv6_bounded_engagements_must_carry_an_expiry() -> None:
    for employment in (Employment.CONTRACTOR, Employment.PARTNER):
        with pytest.raises(ValueError, match="not_after"):
            Principal(
                id="x",
                kind=PrincipalKind.HUMAN,
                employment=employment,
                display_name="No expiry",
            )


def test_inv6_naive_expiry_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        Principal(
            id="x",
            kind=PrincipalKind.HUMAN,
            employment=Employment.CONTRACTOR,
            display_name="Naive",
            not_after=datetime(2027, 1, 1),
        )


# ------------------------------------------------------------------ INV-7
def test_inv7_scope_sql_never_interpolates_a_value() -> None:
    """INV-7: predicate rendering is parameterised. A client name cannot become SQL."""
    s = Scope(clauses=(Clause(field="department", op=Op.EQ, value="'; DROP TABLE grants--"),))
    sql, params = s.to_sql()
    assert "DROP TABLE" not in sql
    assert "DROP TABLE" in next(iter(params.values()))


def test_inv7_unrestricted_scope_renders_true() -> None:
    sql, params = Scope.unrestricted().to_sql()
    assert sql == "TRUE"
    assert params == {}


# ------------------------------------------------------------------ regression
def test_scope_normalises_duplicate_clauses() -> None:
    """Regression, found by test_inv1_intersection_is_idempotent on 2026-09-04.

    `intersect` concatenates clause tuples, so intersecting a scope with itself produced
    `(department=maintenance, department=maintenance)`. Same meaning, different
    serialisation, therefore a different ent_hash — which is the cache key. The same
    caller would have missed their own cache entry and shown up in traces as a different
    principal. Scope now deduplicates and sorts on construction.
    """
    s = Scope.department("maintenance")
    assert len(s.intersect(s).clauses) == 1
    assert s.intersect(s) == s


def test_scope_clause_order_does_not_affect_identity() -> None:
    a = Clause(field="department", op=Op.EQ, value="maintenance")
    b = Clause(field="tier", op=Op.EQ, value="managed")
    assert Scope(clauses=(a, b)) == Scope(clauses=(b, a))
    assert Scope(clauses=(a, b)).to_sql() == Scope(clauses=(b, a)).to_sql()
