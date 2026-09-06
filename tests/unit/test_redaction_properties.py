"""Properties of the redactor that must hold for every shape, not for the ones we thought of.

`tests/unit/test_redaction.py` is a list of cases somebody imagined: a ticket nested in a
client, an untagged blob, a count beside the collection it counts. Each was written because
somebody thought of it, which is the limit of the technique. The walker recurses through
mappings, sequences and models to `MAX_DEPTH`, and the shapes a connector actually returns
are not the shapes a test author pictures.

**So these generate the shape instead of choosing it (M4.4.3).** Hypothesis builds nested
structures out of mappings, lists, tuples and models, buries a canary somewhere inside, and
the properties below have to hold wherever it landed. When one fails it shrinks to the
smallest shape that still breaks, which is the part that makes a failure a bug report rather
than a puzzle.

The four properties, in the order they matter:

**A canary never reaches a caller without the grant, at any depth.** This is the whole
system in one sentence. Depth is exactly where it fails: a walker that stops recursing
returns the right answer for every flat record and leaks every nested one, which is why the
hand-written suite has a test named for that and why this one generates the nesting.

**More entitlement never shows less.** Monotonicity is not obvious from the code and is
relied on everywhere: `E_run = E(caller) ∩ agent_ceiling` is only meaningful if adding a
grant cannot remove a field. A redactor that failed it would make an agent's ceiling
occasionally *widen* what a caller sees.

**Every shape produces a payload or a named refusal, and never a traceback.** A connector
returning something absurd must fail closed and say so. An `UntypedShapeError` is a
refusal; a `RecursionError` is an outage.

**The trace names fields and never values.** The report is written to logs, which outlive
the answer and are read by people who were never entitled to it.

Task ids: M4.4.3
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.envelope import Entity, TypedResult
from brain.core.field_policy import Classification, FieldPolicy, policy_from_rows
from brain.core.redaction import MAX_DEPTH, DropReason, UntypedShapeError, redact
from brain.core.scope import Scope
from tests.fixtures.company import CANARIES, NOW

CANARY = CANARIES["hr.salary"]

#: The canary's own capability, and one that is not it. Two names rather than one, because
#: "the caller lacks the grant" and "the caller holds nothing at all" are different tests
#: and only the first one distinguishes withholding from an empty answer.
SECRET_CAPABILITY = "read:vault.secret"
OPEN_CAPABILITY = "read:vault.note"

POLICY: FieldPolicy = policy_from_rows(
    [
        ("vault", "note", OPEN_CAPABILITY, Classification.INTERNAL),
        ("vault", "secret", SECRET_CAPABILITY, Classification.RESTRICTED),
        ("vault", "child", OPEN_CAPABILITY, Classification.INTERNAL),
    ]
)


class Vault(Entity):
    """One entity with an open field, a restricted field and somewhere to nest.

    `child` is `Any` deliberately: the point of these tests is that the walker survives a
    shape nobody declared, and a typed child would let the model validate away exactly the
    structures worth generating.
    """

    note: str = "ordinary"
    secret: str = CANARY
    child: Any = None


def _ent(*capabilities: str) -> EntitlementSet:
    return EntitlementSet(
        principal_id="u_property",
        grants=tuple(
            Grant(capability=Capability(value=c), scope=Scope.unrestricted()) for c in capabilities
        ),
    )


# --------------------------------------------------------------- generating a shape
def _nested(depth: int) -> st.SearchStrategy[Any]:
    """Arbitrary nested data, with a canary reachable somewhere inside.

    **The canary is never a generated leaf, and that correction is the point.** The first
    version put `CANARY` in the leaf alphabet, and hypothesis immediately shrank to
    `child=CANARY`: the canary sitting in `child`, which is classified under
    `OPEN_CAPABILITY` and which the caller is entitled to read. The redactor withheld
    `secret` correctly and returned `child` correctly, and the test failed anyway.

    That was the test being wrong, and wrong in a way worth writing down: **the redactor is
    a field-level control, not a content scanner.** It decides by the classification of the
    field a value sits in, never by what the value looks like. A secret placed in an
    unclassified field is a classification bug, and the tool for values that look
    dangerous wherever they appear is `brain.ops.pii`, whose own docstring says it is
    explicitly never an authorisation boundary.

    So the canary reaches a payload only ever as the `secret` field of some `Vault`, at
    whatever depth the recursion put that Vault. The property below is then exact: not
    "this string never appears", which is a claim about content, but "no restricted field
    is ever readable", which is the claim the system actually makes.

    Keys are drawn from a small alphabet rather than from arbitrary text, because the
    interesting variation here is the *structure* and unicode key names would spend the
    budget exploring string encoding instead of nesting. `MAX_DEPTH` is the ceiling the
    walker declares, and the strategy is allowed to exceed it so the failing-closed
    property has something to fail on.
    """
    leaves = st.one_of(
        st.text(alphabet="abcdef", max_size=6),
        st.integers(min_value=-1000, max_value=1000),
        st.booleans(),
        st.none(),
    )
    return st.recursive(
        leaves,
        lambda children: st.one_of(
            st.lists(children, max_size=3),
            st.dictionaries(st.text(alphabet="klmn", min_size=1, max_size=3), children, max_size=3),
            st.builds(lambda c: Vault(entity="vault", id="v_1", child=c), children),
        ),
        max_leaves=depth,
    )


def _result(child: Any) -> TypedResult[Entity]:
    return TypedResult(
        records=(Vault(entity="vault", id="v_0", child=child),),
        source="lark_base",
        fetched_at="2026-09-04T14:31:00Z",
    )


PROPERTY_SETTINGS = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


# --------------------------------------------------------------- the canary
@PROPERTY_SETTINGS
@given(child=_nested(depth=8))
def test_a_canary_never_reaches_a_caller_without_the_grant_at_any_depth(child: Any) -> None:
    """**The whole system in one property.** Whatever shape the connector returned and
    wherever the restricted value sits inside it, a caller without `read:vault.secret` never
    sees the token.

    Depth is where this fails in practice. A walker that stops recursing gives the correct
    answer for every flat record and leaks every nested one, so a suite of hand-written flat
    cases passes while the bypass is one `child=` away.

    Delete this and the guarantee is only ever checked against shapes somebody pictured."""
    answer = redact(_result(child), entitlement=_ent(OPEN_CAPABILITY), policy=POLICY, now=NOW)

    assert CANARY not in str(answer.payload.model_dump())


@PROPERTY_SETTINGS
@given(child=_nested(depth=8))
def test_a_caller_who_holds_the_grant_still_gets_the_value(child: Any) -> None:
    """The positive case, and it is not redundant. A redactor that removed the canary from
    every answer would satisfy the property above for every shape ever generated, and it
    would also be useless: the field exists to be read by the people entitled to it.

    Asserted on the top-level record rather than on the generated child, because the child
    is arbitrary and may legitimately contain nothing."""
    answer = redact(
        _result(child),
        entitlement=_ent(OPEN_CAPABILITY, SECRET_CAPABILITY),
        policy=POLICY,
        now=NOW,
    )

    assert answer.payload.records[0]["secret"] == CANARY


# --------------------------------------------------------------- monotonicity
@PROPERTY_SETTINGS
@given(child=_nested(depth=6))
def test_holding_more_entitlement_never_shows_a_caller_less(child: Any) -> None:
    """**Monotonicity, which the invariant quietly depends on.**
    `E_run = E(caller) ∩ agent_ceiling` is only meaningful if adding a grant cannot take a
    field away. A redactor that broke this would make an agent's ceiling occasionally widen
    what its caller sees, which reads as a permission bug nobody can reproduce.

    Compared as the set of field paths present, not as values, because the wider caller
    legitimately sees more.

    Delete this and a rule that reads two capabilities together can start hiding a field
    from somebody who gained one of them."""
    narrow = redact(_result(child), entitlement=_ent(OPEN_CAPABILITY), policy=POLICY, now=NOW)
    wide = redact(
        _result(child),
        entitlement=_ent(OPEN_CAPABILITY, SECRET_CAPABILITY),
        policy=POLICY,
        now=NOW,
    )

    def keys(payload: Any) -> set[str]:
        return {k for record in payload.records for k in record}

    assert keys(narrow.payload) <= keys(wide.payload)


@PROPERTY_SETTINGS
@given(child=_nested(depth=6))
def test_a_caller_holding_nothing_sees_no_more_than_one_holding_something(child: Any) -> None:
    """The other end of the same ordering. An empty entitlement is the floor, and a shape
    that somehow produced *more* for a caller with no grants would be a default applied
    where a decision failed."""
    nothing = redact(_result(child), entitlement=_ent(), policy=POLICY, now=NOW)
    something = redact(_result(child), entitlement=_ent(OPEN_CAPABILITY), policy=POLICY, now=NOW)

    assert {k for r in nothing.payload.records for k in r} <= {
        k for r in something.payload.records for k in r
    }
    assert CANARY not in str(nothing.payload.model_dump())


# --------------------------------------------------------------- failing closed
@PROPERTY_SETTINGS
@given(child=_nested(depth=10))
def test_every_shape_produces_a_payload_or_a_named_refusal_and_never_a_traceback(
    child: Any,
) -> None:
    """A connector returning something absurd must fail closed and say which rule it broke.
    `UntypedShapeError` is a refusal somebody can act on; a `RecursionError` or a
    `TypeError` is an outage, and on this path an outage is a request that returns a 500
    where a person expected an answer they were entitled to.

    Delete this and a shape that crashes the walker is discovered by a connector returning
    it in production."""
    try:
        answer = redact(_result(child), entitlement=_ent(OPEN_CAPABILITY), policy=POLICY, now=NOW)
    except UntypedShapeError:
        return  # A refusal is a correct outcome; it is named and it is closed.

    assert answer.payload is not None
    assert CANARY not in str(answer.payload.model_dump())


def test_a_shape_deeper_than_the_walker_will_go_stops_and_says_so() -> None:
    """`MAX_DEPTH` exists so an absurd shape stops the walk rather than the interpreter.

    **Asserted as a `TOO_DEEP` drop, after a mutation showed the obvious version proved
    nothing.** The first attempt buried a canary at the bottom and asserted it did not come
    out. Removing the ceiling left that test green, and correctly so: without the ceiling
    the walker simply recurses further and redacts each level properly. The canary never
    escapes either way.

    That is the useful finding. **This ceiling is about termination, not confidentiality.**
    Its job is that a cyclic or absurdly deep structure from a connector stops the walk
    instead of exhausting the stack, and a stack exhaustion here is a request that returns
    nothing where somebody expected an answer. So the property is that the walker refuses
    the depth and records why, which is what removing the ceiling actually breaks.

    Built deliberately rather than generated, because hypothesis shrinks away from deep
    cases and this is specifically about the ceiling.

    **Nested out of tagged entities rather than plain dictionaries**, which was the second
    correction. A chain of bare dicts is dropped as `UNTAGGED` at the first level and never
    travels far enough to meet the ceiling, so the test passed for a reason that had nothing
    to do with depth. Only a chain the walker is willing to keep following gets deep enough
    to be refused for being deep."""
    deep: Any = Vault(entity="vault", id="v_deep", child=None)
    for level in range(MAX_DEPTH + 5):
        deep = Vault(entity="vault", id=f"v_{level}", child=deep)

    answer = redact(_result(deep), entitlement=_ent(OPEN_CAPABILITY), policy=POLICY, now=NOW)

    reasons = [d.reason for d in answer.trace.dropped]
    assert DropReason.TOO_DEEP in reasons, (
        "a shape past MAX_DEPTH must stop the walk and record it; without the ceiling a "
        f"cyclic structure from a connector exhausts the stack instead. Got: {reasons}"
    )


# --------------------------------------------------------------- the trace
@PROPERTY_SETTINGS
@given(child=_nested(depth=6))
def test_the_trace_names_fields_and_never_carries_a_value(child: Any) -> None:
    """The report goes to logs, which outlive the answer and are read by people who were
    never entitled to it. `RedactionTrace` records what was withheld by name and by count,
    and a value reaching it would put the withheld thing in the one place nobody thinks to
    check.

    Delete this and a diagnostic improvement adds the value that was removed, which is the
    most natural debugging aid to reach for and the exact leak."""
    answer = redact(_result(child), entitlement=_ent(OPEN_CAPABILITY), policy=POLICY, now=NOW)

    assert CANARY not in str(answer.trace)
    for name in answer.trace.withheld_field_names():
        assert CANARY not in name


# --------------------------------------------------------------- determinism
@PROPERTY_SETTINGS
@given(child=_nested(depth=6))
def test_redacting_the_same_shape_twice_gives_the_same_answer(child: Any) -> None:
    """Determinism is what makes the answer cache safe: `gate.cache_key` keys on the
    entitlement hash and the policy epoch, and two runs at the same reach must agree or a
    cached answer is a different answer from the one that would be computed now.

    Delete this and an iteration-order dependency in the walker becomes a cache that
    sometimes serves a field it sometimes withholds."""
    first = redact(_result(child), entitlement=_ent(OPEN_CAPABILITY), policy=POLICY, now=NOW)
    second = redact(_result(child), entitlement=_ent(OPEN_CAPABILITY), policy=POLICY, now=NOW)

    assert first.payload.model_dump() == second.payload.model_dump()


@pytest.mark.parametrize("capability", [OPEN_CAPABILITY, SECRET_CAPABILITY])
def test_the_policy_under_test_actually_governs_both_fields(capability: str) -> None:
    """A guard against the generated tests passing because the policy was wrong rather than
    because the redactor was right. If `secret` were unclassified the canary property would
    hold trivially, by the field being dropped for everybody.

    Delete this and a typo in a capability name turns every property above green."""
    assert any(rule.required_capability.value == capability for rule in POLICY.rules)
