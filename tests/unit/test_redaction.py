"""The redaction walker, the field policy, the lock and the serialiser.

These are the mechanics. The properties that must never break, whatever anybody refactors,
live in `tests/invariants/test_redaction_invariants.py` and are marked so a failure blocks
deploy.

The policy below is rebuilt in the invariant suite rather than imported from here. That
duplication is deliberate: an invariant suite that imports its fixtures from a unit suite
stops being an independent check the moment somebody tidies the unit suite up.

Task ids: M4.1.1, M4.1.2, M4.1.3, M4.1.4, M4.1.5, M4.1.6, M4.2.1, M4.2.2, M4.2.3, M4.2.4,
M4.3.1, M4.3.2, M4.3.3, M4.3.4, M4.4.1, M4.4.2, M4.4.4
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.envelope import Entity, Redaction, TypedResult
from brain.core.errors import Denied, Outcome, to_public
from brain.core.field_policy import (
    Classification,
    FieldPolicy,
    FieldRule,
    PolicyConflictError,
    policy_from_rows,
)
from brain.core.redaction import (
    ASKER_ACKNOWLEDGEMENT,
    LOCK_TEXT,
    MAX_DEPTH,
    OPAQUE_CAPABILITY,
    OPAQUE_LABEL,
    AccessRequest,
    ChannelAdapterRegistry,
    ChannelPathError,
    ChannelPayload,
    DropReason,
    RedactedAnswer,
    RedactionReason,
    RedactionTrace,
    SimulationReport,
    UntypedShapeError,
    assert_channel_adapter,
    assert_tool_returns_typed_result,
    compute_mask,
    redact,
    render_lock,
    serialise_for_channel,
    simulate_redaction,
)
from brain.core.scope import Clause, Op, Scope
from tests.fixtures.company import CANARIES, NOW, person


# ------------------------------------------------------------------ the shapes
class Ticket(Entity):
    department: str = "maintenance"
    status: str = "open"
    subject: str = "SSL renewal"
    internal_note: str = CANARIES["ticket.internal_note"]


class Client(Entity):
    name: str = "SNM Construction Pte Ltd"
    department: str = "maintenance"
    hosting_expiry: str = "2026-11-14"
    hours_remaining: int | None = 12
    contract_value: str = CANARIES["client.contract_value"]
    margin: str = CANARIES["client.margin"]
    tickets: tuple[Ticket, ...] = ()


class Blob(Entity):
    """An entity with a deliberately untyped payload.

    Every connector eventually returns one of these, and the walker has to survive
    whatever is inside it without being told the shape in advance.
    """

    payload: Any = None


class ClientWithCount(Entity):
    """A client carrying a summary count beside the collection it counts (M4.2.5).

    The shape that makes the leak: `ticket_count` is an ordinary classified field, held
    under an ordinary capability, and printing it beside a filtered list of tickets hands
    the asker the difference.
    """

    name: str = "SNM Construction Pte Ltd"
    department: str = "maintenance"
    ticket_count: int = 2
    tickets: tuple[Ticket, ...] = ()


class SummaryClient(Entity):
    """A count with no collection beside it. The case that must keep working."""

    name: str = "SNM Construction Pte Ltd"
    department: str = "maintenance"
    ticket_count: int = 40


class ClientWithOneTicket(Entity):
    """A count declared over something that is not a sequence."""

    name: str = "SNM Construction Pte Ltd"
    department: str = "maintenance"
    ticket_count: int = 1
    tickets: Ticket | None = None


def a_client(**kw: Any) -> Client:
    return Client(entity="client", id="c_0447", **kw)


def a_ticket(**kw: Any) -> Ticket:
    return Ticket(entity="ticket", id="t_9", **kw)


def a_blob(payload: Any) -> Blob:
    return Blob(entity="blob", id="b_1", payload=payload)


def result_of(*records: Entity) -> TypedResult[Entity]:
    return TypedResult(
        records=tuple(records), source="lark_base", fetched_at="2026-09-04T14:31:00Z"
    )


# ------------------------------------------------------------------ the policy
POLICY: FieldPolicy = policy_from_rows(
    [
        ("client", "name", "read:client.name", Classification.INTERNAL),
        ("client", "department", "read:client.name", Classification.INTERNAL),
        ("client", "hosting_expiry", "read:client.hosting_expiry", Classification.INTERNAL),
        ("client", "hours_remaining", "read:client.hours_remaining", Classification.INTERNAL),
        ("client", "contract_value", "read:client.contract_value", Classification.RESTRICTED),
        ("client", "margin", "read:client.margin", Classification.RESTRICTED),
        ("client", "tickets", "read:ticket.status", Classification.INTERNAL),
        ("ticket", "department", "read:ticket.status", Classification.INTERNAL),
        ("ticket", "status", "read:ticket.status", Classification.INTERNAL),
        ("ticket", "subject", "read:ticket.subject", Classification.INTERNAL),
        ("ticket", "internal_note", "read:ticket.internal_note", Classification.CONFIDENTIAL),
        ("blob", "payload", "read:blob.payload", Classification.INTERNAL),
    ]
)


#: The same policy with the count declaration added (M4.2.5). Written as an addition rather
#: than folded into `POLICY`, so that every test above still runs against a policy with no
#: counts in it at all and the count rule cannot quietly become load-bearing for them.
COUNTING_POLICY: FieldPolicy = POLICY.with_rules(
    FieldRule.of(
        "client", "ticket_count", "read:client.name", Classification.INTERNAL, counts="tickets"
    )
)


def ent(*values: str, scope: Scope | None = None, principal: str = "u_test") -> EntitlementSet:
    """An entitlement built here rather than borrowed from a persona.

    Used only where the point of the test is a shape rather than a person. Anything about
    who sees what is asked of the synthetic company instead.
    """
    where = Scope.unrestricted() if scope is None else scope
    return EntitlementSet(
        principal_id=principal,
        grants=tuple(Grant(capability=Capability(value=v), scope=where) for v in values),
    )


def payload_for(pid: str, *records: Entity, policy: FieldPolicy = POLICY) -> ChannelPayload:
    return serialise_for_channel(
        result_of(*records), entitlement=person(pid).entitlement(), policy=policy, now=NOW
    )


# ============================================================= M4.1 the walker
def test_the_walk_reaches_every_nesting_level() -> None:
    """If it deletes only at the top level, a restricted field survives by being nested
    one deeper, and every connector that returns a record with children becomes a bypass.
    This is the test that catches a walker that stops recursing."""
    answer = redact(
        result_of(a_client(tickets=(a_ticket(),))),
        entitlement=person("u_weiling").entitlement(),
        policy=POLICY,
        now=NOW,
    )
    ticket = answer.payload.records[0]["tickets"][0]
    assert "internal_note" not in ticket
    assert "subject" not in ticket
    assert ticket["status"] == "open"


def test_a_field_outside_the_mask_is_deleted_not_blanked() -> None:
    """A key present with a placeholder is still a key. The next thing to serialise it
    carries the placeholder into a model's context as though it were data, and the model
    then has a name for something it was never supposed to know existed."""
    record = payload_for("u_weiling", a_client()).records[0]
    assert "contract_value" not in record
    assert CANARIES["client.contract_value"] not in str(record)


def test_an_untagged_object_is_dropped_rather_than_passed_through() -> None:
    """Fail closed. An object with no entity tag has nothing to look a capability up by,
    so there is no question the redactor could ask about it. Passing it through returns
    data nobody checked; returning half of it means guessing which half was safe."""
    answer = redact(
        result_of(a_blob({"note": "untagged", "salary": CANARIES["hr.salary"]})),
        entitlement=ent("read:blob.payload"),
        policy=POLICY,
    )
    assert answer.payload.records == ()
    assert CANARIES["hr.salary"] not in str(answer.payload.model_dump())


def test_an_untagged_object_is_logged_when_it_is_dropped() -> None:
    """Silent dropping means a connector returning the wrong shape looks like a connector
    returning no data, and nobody investigates an empty answer."""
    answer = redact(
        result_of(a_blob({"note": "untagged"})),
        entitlement=ent("read:blob.payload"),
        policy=POLICY,
    )
    reasons = [d.reason for d in answer.trace.dropped]
    assert DropReason.UNTAGGED in reasons
    assert [d.path for d in answer.trace.dropped if d.reason is DropReason.UNTAGGED] == [
        "records[0].payload"
    ]


def test_a_tagged_object_without_a_usable_record_id_is_dropped() -> None:
    """A redaction that cannot name the record it happened to is one nobody can audit, and
    a citation that cannot name the record is not a citation."""
    answer = redact(
        result_of(a_blob({"entity": "ticket", "status": "open"})),
        entitlement=ent("read:blob.payload", "read:ticket.status"),
        policy=POLICY,
    )
    assert DropReason.UNIDENTIFIED in [d.reason for d in answer.trace.dropped]
    assert answer.payload.records == ()


def test_a_record_left_holding_only_its_tag_is_dropped_after_the_child_walk() -> None:
    """The husk case, and the reason the substance check runs twice. A record whose one
    permitted field held nothing but an untagged object passes the mask, because the mask
    reads keys and cannot know whether the child will survive. Without the second check it
    arrives at the channel as a bare entity and id, announcing that the record exists."""
    answer = redact(
        result_of(a_blob({"note": "untagged"})),
        entitlement=ent("read:blob.payload"),
        policy=POLICY,
    )
    assert answer.payload.records == ()
    assert [d.reason for d in answer.trace.dropped] == [
        DropReason.UNTAGGED,
        DropReason.NO_VISIBLE_FIELD,
    ]


def test_an_array_of_entities_is_walked_element_by_element() -> None:
    """One bad element must not take the array with it, and one good element must not
    carry the others past the gate."""
    answer = redact(
        result_of(
            a_blob(
                [
                    {"entity": "ticket", "id": "t_1", "status": "open"},
                    {"note": "untagged"},
                    {"entity": "ticket", "id": "t_2", "status": "closed"},
                ]
            )
        ),
        entitlement=ent("read:blob.payload", "read:ticket.status"),
        policy=POLICY,
    )
    kept = answer.payload.records[0]["payload"]
    assert [t["id"] for t in kept] == ["t_1", "t_2"]


def test_a_mixed_array_of_scalars_and_entities_survives_intact() -> None:
    """Real connectors return ragged arrays. A walker that assumes a homogeneous list
    raises on the first mixed one, and an exception in the redactor is an outage in every
    answer rather than a bug in one."""
    answer = redact(
        result_of(a_blob([1, "two", None, {"entity": "ticket", "id": "t_1", "status": "open"}])),
        entitlement=ent("read:blob.payload", "read:ticket.status"),
        policy=POLICY,
    )
    kept = answer.payload.records[0]["payload"]
    assert kept[:3] == [1, "two", None]
    assert kept[3]["id"] == "t_1"


def test_a_shape_deeper_than_the_guard_is_dropped_rather_than_recursed() -> None:
    """A shape we cannot reason about is not a shape we return. Without the guard a
    self-referential mapping from a connector takes the process down, and a redactor that
    can be crashed is a redactor that can be removed from the path."""
    node: Any = {"entity": "ticket", "id": "t_deep", "status": "open"}
    for i in range(MAX_DEPTH + 4):
        node = {"entity": "ticket", "id": f"t_{i}", "status": "open", "nested": node}
    answer = redact(
        result_of(a_blob(node)),
        entitlement=ent("read:blob.payload", "read:ticket.*"),
        policy=POLICY.with_rules(
            FieldRule.of("ticket", "nested", "read:ticket.status", Classification.INTERNAL)
        ),
    )
    assert DropReason.TOO_DEEP in [d.reason for d in answer.trace.dropped]


def test_a_nested_entity_is_masked_by_its_own_type_not_its_parents() -> None:
    """A ticket inside a client is still a ticket. A walker that carried the parent's
    entity down would ask the wrong policy question at every level below the first."""
    answer = redact(
        result_of(a_client(tickets=(a_ticket(),))),
        entitlement=ent("read:client.name", "read:ticket.status"),
        policy=POLICY,
    )
    record = answer.payload.records[0]
    # client rules decide the outer record: hosting_expiry and hours_remaining need their
    # own client capabilities and are gone
    assert set(record) == {"entity", "id", "name", "department", "tickets"}
    # ticket rules decide the inner one: read:ticket.status reaches status and department,
    # and says nothing about subject or internal_note
    assert set(record["tickets"][0]) == {"entity", "id", "status", "department"}


def test_a_childs_own_scope_field_beats_the_one_it_would_inherit() -> None:
    """A ticket nested under a maintenance client is not thereby a maintenance ticket.
    Inheritance fills gaps; it must never override, or a connector could widen access by
    choosing where to nest a record."""
    answer = redact(
        result_of(a_client(tickets=(a_ticket(department="web"),))),
        entitlement=person("u_weiling").entitlement(),
        policy=POLICY,
        now=NOW,
    )
    assert answer.payload.records[0]["tickets"] == []


def test_a_child_inherits_a_scope_field_it_does_not_carry() -> None:
    """The opposite failure. A nested record rarely repeats its parent's department, and a
    walker with no inheritance would refuse every nested record for everybody, which reads
    as a permission problem and is really a plumbing one."""

    class Bare(Entity):
        status: str = "open"

    policy = POLICY.with_rules(
        FieldRule.of("client", "tickets", "read:ticket.status", Classification.INTERNAL),
        FieldRule.of("bare", "status", "read:ticket.status", Classification.INTERNAL),
    )

    class ClientWithBare(Entity):
        name: str = "SNM Construction Pte Ltd"
        department: str = "maintenance"
        tickets: tuple[Bare, ...] = ()

    record = ClientWithBare(entity="client", id="c_0447", tickets=(Bare(entity="bare", id="b_1"),))
    answer = redact(
        result_of(record),
        entitlement=person("u_weiling").entitlement(),
        policy=policy,
        now=NOW,
    )
    assert answer.payload.records[0]["tickets"][0]["status"] == "open"


def test_the_scope_is_evaluated_before_the_fields_it_reads_are_deleted() -> None:
    """Order of operations, and a real bug rather than a theoretical one. `Clause.matches`
    treats a missing field as not matching, so deleting `department` first would refuse
    every departmental grant for everybody, and the failure would look like a permission
    problem rather than an ordering one."""
    without_department = POLICY.without("client", "department")
    record = payload_for("u_weiling", a_client(), policy=without_department).records[0]
    assert "department" not in record
    assert record["name"] == "SNM Construction Pte Ltd"
    assert record["hours_remaining"] == 12


# ------------------------------------------------------- M4.1.6 the escape hatch
def test_the_opaque_escape_hatch_requires_its_own_capability() -> None:
    """An escape hatch anybody can open is not an escape hatch. Its own capability, so it
    shows up in an entitlement set, an ent_hash and a joiners-movers-leavers report like
    every other grant, rather than being an admin flag nobody can query for."""
    with pytest.raises(Denied):
        redact(
            result_of(a_client()),
            entitlement=person("u_aaron").entitlement(),
            policy=POLICY,
            now=NOW,
            opaque=True,
        )


def test_a_wildcard_grant_on_another_noun_does_not_confer_the_escape_hatch() -> None:
    """The hatch has its own noun, so no wildcard anybody already holds reaches it. A
    capability named `read:client.opaque` would have been swept up by `read:client.*`, and
    every department admin in the company holds one of those."""
    assert OPAQUE_CAPABILITY.value == "read:opaque_payload"
    assert not ent("read:client.*", "read:ticket.*", "admin:grant").holds(OPAQUE_CAPABILITY)


def test_a_refused_escape_hatch_still_says_nothing_to_a_person() -> None:
    """The refusal is loud to the caller and silent to the asker. `Denied` collapses to
    the same public message as `Absent`, so even this path cannot confirm a record."""
    with pytest.raises(Denied) as exc:
        redact(result_of(a_client()), entitlement=ent("read:client.*"), policy=POLICY, opaque=True)
    assert exc.value.outcome is Outcome.DENIED
    assert to_public(exc.value) == "I could not find that."


def test_the_escape_hatch_passes_the_payload_through_and_labels_it() -> None:
    """An unredacted payload that looks like a redacted one is how unredacted data gets
    pasted into a channel by somebody who believed the gate had already run."""
    answer = redact(
        result_of(a_client()),
        entitlement=ent("read:opaque_payload"),
        policy=POLICY,
        opaque=True,
    )
    assert answer.payload.label == OPAQUE_LABEL
    assert answer.payload.records[0]["contract_value"] == CANARIES["client.contract_value"]


def test_the_escape_hatch_flags_the_trace() -> None:
    """Without the flag, an auditor reading a trace with no redactions cannot tell a
    payload that needed none from a payload that skipped the walk entirely."""
    answer = redact(
        result_of(a_client()),
        entitlement=ent("read:opaque_payload"),
        policy=POLICY,
        opaque=True,
    )
    assert answer.trace.opaque
    assert answer.trace.redactions == ()


def test_an_ordinary_answer_is_not_labelled() -> None:
    """A label on every answer is a label nobody reads."""
    answer = redact(
        result_of(a_client()), entitlement=person("u_weiling").entitlement(), policy=POLICY, now=NOW
    )
    assert answer.payload.label == ""
    assert not answer.trace.opaque


# ======================================================== M4.1.2 / M4.2 the mask
def test_an_unclassified_field_is_withheld() -> None:
    """Default-deny. A policy that does not mention a field is not permission to show it,
    which is what makes adding a column to a connector safe by default rather than safe if
    somebody remembers."""
    mask = compute_mask(
        "client",
        ["entity", "id", "name", "invented_yesterday"],
        entitlement=ent("read:client.*"),
        policy=POLICY,
        row={"department": "maintenance"},
    )
    assert "invented_yesterday" not in mask.allowed
    assert ("invented_yesterday", RedactionReason.UNCLASSIFIED) in mask.withheld


def test_a_public_classification_is_not_permission_to_show_a_field() -> None:
    """A classification describes a field; a capability describes a person. Letting the
    first stand in for the second means one careless row in a policy table publishes a
    column to everybody, and the row looks harmless because it says "public"."""
    policy = FieldPolicy(
        rules=(FieldRule.of("client", "name", "read:client.name", Classification.PUBLIC),)
    )
    mask = compute_mask("client", ["name"], entitlement=ent("invoke:agent"), policy=policy, row={})
    assert mask.allowed == frozenset()
    assert mask.withheld == (("name", RedactionReason.NO_GRANT),)


def test_a_held_capability_is_still_refused_outside_its_scope() -> None:
    """Daniel reads contract value in sales and not in web. A mask that checked only
    whether the capability was held would hand him every department's figures."""
    entitlement = person("u_dual").entitlement()
    in_sales = compute_mask(
        "client",
        ["contract_value"],
        entitlement=entitlement,
        policy=POLICY,
        row={"department": "sales"},
        now=NOW,
    )
    in_web = compute_mask(
        "client",
        ["contract_value"],
        entitlement=entitlement,
        policy=POLICY,
        row={"department": "web"},
        now=NOW,
    )
    assert in_sales.allowed == frozenset({"contract_value"})
    assert in_web.withheld == (("contract_value", RedactionReason.OUT_OF_SCOPE),)


def test_the_entity_tag_survives_the_mask() -> None:
    """Strip the tag and the record becomes untyped, so the next walk over the same data
    drops it whole. It is safe to keep only because a record with nothing else left is
    dropped anyway."""
    mask = compute_mask(
        "client", ["entity", "id", "margin"], entitlement=ent("invoke:agent"), policy=POLICY, row={}
    )
    assert mask.allowed == frozenset({"entity", "id"})
    assert not mask.has_substance()


def test_a_mask_with_only_the_tag_has_no_substance() -> None:
    """This is the record-level collapse of denied into absent, expressed as a predicate.
    If it returned True, every refused record would come back as a husk announcing its own
    existence."""
    assert not compute_mask(
        "client", ["entity", "id"], entitlement=ent("read:client.*"), policy=POLICY, row={}
    ).has_substance()
    assert compute_mask(
        "client", ["entity", "id", "name"], entitlement=ent("read:client.*"), policy=POLICY, row={}
    ).has_substance()


# ------------------------------------------------------------ M4.2.1 field policy
def test_a_field_rule_must_require_a_read_capability() -> None:
    """Otherwise somebody granted the ability to change a number acquires the ability to
    see it as a side effect, which is not a permission anybody granted."""
    with pytest.raises(ValidationError, match="permission to act is not a permission to see"):
        FieldRule.of(
            "client", "contract_value", "write:client.contract_value", Classification.RESTRICTED
        )


def test_a_rule_can_be_written_with_a_plain_capability_string() -> None:
    """A policy table is long. A rule that must be spelled out in full in every row is a
    rule people write with a helper, and the helper acquires a default that becomes the
    thing under test."""
    rule = FieldRule.of("client", "name", "read:client.name", Classification.INTERNAL)
    assert rule.required_capability == Capability(value="read:client.name")
    assert rule.dotted == "client.name"


def test_two_rules_that_disagree_about_one_field_refuse_to_load() -> None:
    """An ambiguous policy turns "may this person see this field" from a lookup into an
    evaluation-order problem, which is the exact failure the entitlement model refuses by
    having no deny clause. Last-one-wins would make the answer depend on a table's sort."""
    with pytest.raises(PolicyConflictError, match=r"client\.margin"):
        FieldPolicy(
            rules=(
                FieldRule.of("client", "margin", "read:client.margin", Classification.RESTRICTED),
                FieldRule.of("client", "margin", "read:client.name", Classification.PUBLIC),
            )
        )


def test_the_same_rule_written_twice_is_not_a_conflict() -> None:
    """A policy assembled from two overlapping sources is normal. Refusing an exact
    duplicate would make composition impossible and teach people to deduplicate by hand,
    which is where the real conflicts get lost."""
    rule = FieldRule.of("client", "margin", "read:client.margin", Classification.RESTRICTED)
    assert len(FieldPolicy(rules=(rule, rule))) == 1


def test_an_unknown_field_has_no_rule_and_the_lookup_does_not_decide() -> None:
    """`rule_for` returning None must read as "withhold" at the call site. A lookup that
    substituted a permissive default would put the default-deny decision somewhere nobody
    reviews."""
    assert POLICY.rule_for("client", "invented_yesterday") is None
    assert not POLICY.governs("client", "invented_yesterday")


# --------------------------------------------------------- M4.2.4 the policy epoch
def test_the_epoch_changes_when_a_required_capability_changes() -> None:
    """The epoch is in the answer cache key. Without it, tightening a field policy leaves
    a window where a cached answer keeps disclosing a field that was just revoked."""
    tightened = POLICY.with_rules(
        FieldRule.of(
            "client", "hours_remaining", "read:client.contract_value", Classification.INTERNAL
        )
    )
    assert tightened.epoch() != POLICY.epoch()


def test_the_epoch_changes_when_only_the_classification_changes() -> None:
    """Classification drives per-channel sensitivity policy and artifact retention, so a
    reclassification changes what an answer may do even when it changes nobody's access.
    An epoch that ignored it would serve a cached answer through a channel that may no
    longer carry it."""
    reclassified = POLICY.with_rules(
        FieldRule.of("client", "name", "read:client.name", Classification.CONFIDENTIAL)
    )
    assert reclassified.epoch() != POLICY.epoch()


def test_removing_a_rule_changes_the_epoch() -> None:
    """Revoking a field from everybody is a policy change like any other. If the epoch
    stayed put, every cached answer would keep serving the field that was just removed."""
    assert POLICY.without("client", "margin").epoch() != POLICY.epoch()


def test_the_epoch_does_not_depend_on_the_order_the_rules_arrived_in() -> None:
    """Loading the same policy from a table with a different sort would otherwise
    invalidate every cached answer in the system and look like a policy change in the
    trace."""
    reversed_policy = FieldPolicy(rules=tuple(reversed(POLICY.rules)))
    assert reversed_policy.epoch() == POLICY.epoch()


def test_reverting_a_policy_change_restores_the_epoch() -> None:
    """A counter would keep climbing and throw away a cache that was never wrong. The
    policy after the revert is byte-for-byte the one those answers were computed under."""
    original = POLICY.epoch()
    changed = POLICY.without("client", "margin")
    restored = changed.with_rules(
        FieldRule.of("client", "margin", "read:client.margin", Classification.RESTRICTED)
    )
    assert changed.epoch() != original
    assert restored.epoch() == original


def test_the_epoch_travels_with_every_answer() -> None:
    """A trace that does not say which policy produced it cannot be replayed, so nobody
    can tell whether an old answer was correct at the time."""
    answer = redact(
        result_of(a_client()), entitlement=person("u_weiling").entitlement(), policy=POLICY, now=NOW
    )
    assert answer.trace.policy_epoch == POLICY.epoch()


# ------------------------------------------------------------ M4.2.3 simulate mode
def test_simulate_reports_the_field_names_that_would_be_withheld() -> None:
    """Screen 13's preview, as an assertion: an admin previewing a person sees which
    fields that person loses, computed by the real gate rather than by an estimator."""
    report = simulate_redaction(
        result_of(a_client(tickets=(a_ticket(),))),
        entitlement=person("u_weiling").entitlement(),
        policy=POLICY,
        now=NOW,
    )
    assert report.would_withhold == (
        "client.contract_value",
        "client.margin",
        "ticket.internal_note",
        "ticket.subject",
    )


def test_a_simulation_report_has_nowhere_to_put_a_value() -> None:
    """The structural half of the argument. A simulate mode that could return the
    unredacted payload is one boolean away from a breach, and that boolean is the kind
    a caching layer or a test helper eventually sets."""
    assert set(SimulationReport.model_fields) == {
        "policy_epoch",
        "ent_hash",
        "would_withhold",
        "would_withhold_a_record",
    }


def test_a_simulation_names_no_values_even_when_everything_is_withheld() -> None:
    """The canaries are the check. A report that leaked one would be a preview feature
    that hands an admin the data they were previewing the absence of."""
    report = simulate_redaction(
        result_of(a_client(tickets=(a_ticket(),))),
        entitlement=person("u_jason").entitlement(),
        policy=POLICY,
        now=NOW,
    )
    text = str(report.model_dump())
    assert all(token not in text for token in CANARIES.values())
    assert report.would_withhold_a_record


def test_a_simulation_reports_a_withheld_record_as_a_flag_not_a_count() -> None:
    """A count of withheld records is a hidden-item count wherever it is read. A flag
    answers "does this person lose whole records" without answering "how many"."""
    report = simulate_redaction(
        result_of(a_client(), a_client(), a_client()),
        entitlement=person("u_jason").entitlement(),
        policy=POLICY,
        now=NOW,
    )
    assert report.would_withhold_a_record is True
    # nothing in the report is a number, so there is nothing for a reader to count
    assert not any(
        isinstance(v, int) and not isinstance(v, bool) for v in report.model_dump().values()
    )


def test_two_policies_can_be_simulated_and_diffed() -> None:
    """Shadow-testing a tightening is a composition rather than a mode. Building it in
    would have meant the redactor holding two policies at once and choosing between
    them, which is one more place to choose wrongly."""
    entitlement = person("u_weiling").entitlement()
    before = simulate_redaction(
        result_of(a_client()), entitlement=entitlement, policy=POLICY, now=NOW
    )
    after = simulate_redaction(
        result_of(a_client()),
        entitlement=entitlement,
        policy=POLICY.without("client", "hours_remaining"),
        now=NOW,
    )
    assert set(after.would_withhold) - set(before.would_withhold) == {"client.hours_remaining"}


# ================================================== M4.3 user-facing presentation
def test_the_lock_rendering_takes_no_arguments() -> None:
    """This is the mechanism, not a style note. A lock that could vary by viewer, field,
    classification or reason would make its own shape a side channel that two people
    comparing screens could read. A signature with nothing in it cannot vary by
    anything."""
    assert inspect.signature(render_lock).parameters == {}


def test_the_lock_renders_the_same_string_for_every_viewer() -> None:
    """Screen 3: the lock renders identically for everyone who cannot see the field, so
    its presence discloses nothing about the value."""
    assert {render_lock() for _ in range(5)} == {LOCK_TEXT}


def test_a_locked_field_carries_no_reason() -> None:
    """The reason is the leak. "Out of scope" tells the asker the field exists on records
    elsewhere; "unclassified" tells them about the policy. Every lock is the same lock."""
    locked = payload_for("u_weiling", a_client()).locked
    assert {item.field for item in locked} == {"contract_value", "margin"}
    assert set(type(locked[0]).model_fields) == {"entity", "record_id", "field"}
    assert all(item.render() == LOCK_TEXT for item in locked)


def test_a_null_value_is_locked_like_any_other() -> None:
    """Otherwise the absence of a lock means "this one is empty", which is a value oracle
    built out of the mechanism meant to hide values."""
    locked = payload_for("u_jason", a_client(hours_remaining=None), policy=POLICY).locked
    assert locked == ()  # the whole record went, so nothing is locked

    entitlement = person("u_weiling").entitlement()
    empty = serialise_for_channel(
        result_of(a_client(hours_remaining=None)), entitlement=entitlement, policy=POLICY, now=NOW
    )
    filled = serialise_for_channel(
        result_of(a_client(hours_remaining=7)), entitlement=entitlement, policy=POLICY, now=NOW
    )
    assert [item.field for item in empty.locked] == [item.field for item in filled.locked]


def test_a_record_with_nothing_visible_is_absent_rather_than_empty() -> None:
    """Returning the tag and the id with every field locked confirms the record exists to
    somebody who was not entitled to learn that. This is the single most important
    property in the module."""
    assert payload_for("u_jason", a_client()).records == ()


def test_a_refusal_is_byte_identical_to_a_record_that_does_not_exist() -> None:
    """The assertion behind "four distinct I-don't-know states, and two of them are the
    same sentence". If these ever differ, the permission model leaks through its own
    output shape."""
    refused = payload_for("u_jason", a_client())
    nothing_found = serialise_for_channel(
        TypedResult(), entitlement=person("u_jason").entitlement(), policy=POLICY, now=NOW
    )
    assert refused == nothing_found


def test_a_payload_that_found_nothing_does_not_name_the_source() -> None:
    """ "I looked in the finance ledger and found nothing" and "I found nothing" have to be
    the same sentence, or the set of sources a person cannot reach becomes enumerable by
    asking about each in turn."""
    refused = payload_for("u_jason", a_client())
    assert refused.source == ""
    assert refused.fetched_at == ""


def test_a_payload_that_found_something_does_name_the_source() -> None:
    """The opposite failure. Suppressing provenance on every answer would make citations
    impossible, and a citation is what separates an answer from a guess."""
    assert payload_for("u_weiling", a_client()).source == "lark_base"


def test_the_payload_has_no_field_that_could_carry_a_count() -> None:
    """Never emit a count of hidden items. With `extra="forbid"` and this field set, a
    channel adapter cannot attach one later because it thought it would be helpful."""
    assert set(ChannelPayload.model_fields) == {
        "records",
        "locked",
        "label",
        "source",
        "fetched_at",
        "truncated",
    }


def test_no_count_of_withheld_records_reaches_the_payload() -> None:
    """Three refused records and one visible must look exactly like one visible record."""
    visible = a_client(department="maintenance")
    hidden = [a_client(department="web") for _ in range(3)]
    many = payload_for("u_weiling", visible, *hidden)
    one = payload_for("u_weiling", visible)
    assert many == one


# ------------------------------------------------------- M4.3.4 the request route
def test_the_owner_sees_the_question_in_the_askers_own_words() -> None:
    """A request stripped down to "grant read:client.contract_value to u_weiling" is a
    request nobody can judge. The owner is deciding whether this person should see this
    field for this reason, and the reason is the question."""
    request = AccessRequest(
        asker_id="u_weiling",
        entity="client",
        field="contract_value",
        question="What is SNM worth to us this year?",
        requested_capability=Capability(value="read:client.contract_value"),
    )
    notice = request.for_owner()
    assert notice.question == "What is SNM worth to us this year?"
    assert notice.asker_id == "u_weiling"
    assert notice.requested_capability.value == "read:client.contract_value"


def test_the_asker_learns_nothing_from_the_request_route() -> None:
    """ "Your request was sent to the Finance owner" names a department, and "there is no
    owner for that field" says the field does not exist. Either can be asked repeatedly
    with different guesses until the shape of the company falls out."""
    requests = [
        AccessRequest(
            asker_id=pid,
            entity=entity,
            field=field,
            question=f"why can I not see {field}",
            requested_capability=Capability(value=f"read:{entity}.{field}"),
        )
        for pid in ("u_weiling", "u_jason", "u_partner")
        for entity, field in (("client", "contract_value"), ("hr", "salary"), ("ticket", "status"))
    ]
    replies = {r.for_asker() for r in requests}
    assert replies == {ASKER_ACKNOWLEDGEMENT}
    for request in requests:
        reply = request.for_asker()
        assert request.field not in reply
        assert request.entity not in reply
        assert request.asker_id not in reply
        assert request.question not in reply


def test_an_unclassified_field_is_traced_but_not_locked() -> None:
    """A lock is an offer: it says the field exists, somebody owns it, and a capability
    would reach it, which is what makes the request-access route beside it lead somewhere.
    An unclassified field has neither, so a lock on it advertises a connector's column to
    everybody and routes anyone who asks into a dead end. The trace still records it,
    because "this connector returns a column nobody classified" is what an operator needs
    to be told."""

    class Surprise(Entity):
        name: str = "SNM Construction Pte Ltd"
        department: str = "maintenance"
        added_by_a_connector_last_night: str = CANARIES["client.contract_value"]

    answer = redact(
        result_of(Surprise(entity="client", id="c_0447")),
        entitlement=person("u_weiling").entitlement(),
        policy=POLICY,
        now=NOW,
    )
    assert [item.field for item in answer.payload.locked] == []
    assert "added_by_a_connector_last_night" not in str(answer.payload.model_dump())
    assert answer.trace.withheld_field_names() == ("client.added_by_a_connector_last_night",)


def test_a_governed_field_is_locked_so_the_two_cases_stay_distinguishable() -> None:
    """The counterpart to the test above. If nothing were ever locked, screen 3 would show
    a client record with no sign that a figure exists and somebody else can see it, and
    the locked field is the product."""
    assert {item.field for item in payload_for("u_weiling", a_client()).locked} == {
        "contract_value",
        "margin",
    }


def test_a_key_that_is_not_a_name_is_dropped_rather_than_raising() -> None:
    """A redactor that raises is a redactor somebody takes out of the path. A key that is
    not a name cannot be classified, granted or cited, so it is dropped and recorded by
    path. Recording it by name would keep the half that leaks: in
    `{"SNM Construction Pte Ltd": "overdue"}` the key is what names the client."""
    answer = redact(
        result_of(
            a_blob(
                {
                    "entity": "ticket",
                    "id": "t_1",
                    "status": "open",
                    "SNM Construction Pte Ltd": CANARIES["hr.salary"],
                }
            )
        ),
        entitlement=ent("read:blob.payload", "read:ticket.status"),
        policy=POLICY,
    )
    assert answer.payload.records[0]["payload"]["status"] == "open"
    assert DropReason.UNNAMED_KEY in [d.reason for d in answer.trace.dropped]
    trace_text = str(answer.trace.model_dump())
    assert "SNM" not in trace_text
    assert CANARIES["hr.salary"] not in trace_text


def test_a_nested_array_produces_a_trace_path_the_trace_accepts() -> None:
    """An array of arrays yields consecutive subscripts with no name between them. The
    path grammar has to admit that, or the redactor raises on a shape it should simply
    have recorded, and it raises inside the one component every answer passes through."""
    answer = redact(
        result_of(a_blob([[{"note": "untagged"}]])),
        entitlement=ent("read:blob.payload"),
        policy=POLICY,
    )
    assert "records[0].payload[0][0]" in [d.path for d in answer.trace.dropped]


# ============================================== M4.4 serialisation and the trace
def test_the_serialiser_hands_back_only_the_payload() -> None:
    """A channel adapter calling this cannot reach the trace, the reasons or the dropped
    records, because the value it is handed does not contain them. A rule saying "channels
    must not read the trace" would be a rule; this is a shape."""
    assert isinstance(payload_for("u_weiling", a_client()), ChannelPayload)


@pytest.mark.parametrize(
    "untyped", [{"entity": "client", "id": "c_1"}, [{"id": "c_1"}], "a client", 42, None]
)
def test_an_untyped_shape_from_a_tool_is_refused(untyped: object) -> None:
    """The redactor has no entity to ask a capability question about, so nothing can be
    returned. Walking it defensively was rejected: the tool would appear to work, return
    progressively less as the redactor got stricter, and nobody would file a bug because a
    thin answer looks like a narrow entitlement."""
    with pytest.raises(UntypedShapeError):
        serialise_for_channel(
            untyped,  # type: ignore[arg-type]
            entitlement=ent("read:client.*"),
            policy=POLICY,
        )


def test_a_result_whose_records_are_not_entities_is_refused() -> None:
    """`TypedResult` is generic over `BaseModel`, so a tool can build one out of a model
    carrying no entity tag. Two defences, and this asserts the second.

    mypy is the first: the `type: ignore` below is the build-time refusal, and removing it
    fails the type check. That is the defence a connector author meets while writing. The
    runtime check is what meets the same author after a `cast`, a `**kwargs`, or a plugin
    loaded by name, which is how this shape actually arrives in production."""

    class Untagged(BaseModel):  # a valid TypedResult member, and no entity tag
        name: str

    with pytest.raises(UntypedShapeError, match="not entities"):
        serialise_for_channel(
            TypedResult(records=(Untagged(name="SNM"),)),  # type: ignore[type-var]
            entitlement=ent("read:client.*"),
            policy=POLICY,
        )


def test_the_trace_records_field_names_and_counts() -> None:
    """The console reports a redaction rate and the telemetry list requires a redaction
    count per request. Both need the trace to hold them, and only the trace."""
    answer = redact(
        result_of(a_client(tickets=(a_ticket(),))),
        entitlement=person("u_weiling").entitlement(),
        policy=POLICY,
        now=NOW,
    )
    assert answer.trace.redaction_count == 4
    assert answer.trace.dropped_count == 0
    assert {r.field for r in answer.trace.redactions} == {
        "contract_value",
        "margin",
        "subject",
        "internal_note",
    }


def test_the_trace_never_holds_the_value_it_withheld() -> None:
    """The trace is the one artifact of an answer that outlives the answer, which makes it
    the worst possible place to keep what was just withheld."""
    answer = redact(
        result_of(a_client(tickets=(a_ticket(),))),
        entitlement=person("u_weiling").entitlement(),
        policy=POLICY,
        now=NOW,
    )
    text = str(answer.trace.model_dump())
    assert all(token not in text for token in CANARIES.values())


def test_the_trace_refuses_a_value_written_in_place_of_a_field_name() -> None:
    """The validator duplicates what the walker guarantees, on purpose. A trace also
    arrives by being loaded from a store or built by a later version of the code, and the
    rule has to have one answer rather than one answer per code path."""
    with pytest.raises(ValidationError, match="would put a value in the trace"):
        RedactionTrace(
            policy_epoch=POLICY.epoch(),
            ent_hash="0" * 32,
            redactions=(
                Redaction(
                    entity="client",
                    record_id="c_0447",
                    field="salary was 92000",
                    reason="no grant",
                ),
            ),
        )


def test_the_trace_refuses_a_reason_that_is_not_in_the_taxonomy() -> None:
    """A free-text reason is where somebody eventually writes what the value was."""
    with pytest.raises(ValidationError, match="is not one of"):
        RedactionTrace(
            policy_epoch=POLICY.epoch(),
            ent_hash="0" * 32,
            redactions=(
                Redaction(
                    entity="client",
                    record_id="c_0447",
                    field="margin",
                    reason="looked too high for this caller",
                ),
            ),
        )


def test_the_trace_carries_the_entitlement_as_a_hash_not_a_grant_list() -> None:
    """A trace of capabilities is a map of who can see what, which is a document nobody
    should have. The ledger makes the same argument about its own entries."""
    entitlement = person("u_weiling").entitlement()
    answer = redact(result_of(a_client()), entitlement=entitlement, policy=POLICY, now=NOW)
    assert answer.trace.ent_hash == entitlement.ent_hash()
    assert "read:client" not in str(answer.trace.model_dump())


def test_a_dropped_object_is_recorded_by_path_and_never_by_content() -> None:
    """The whole point of a drop is that we did not trust the object. Copying it into the
    longest-lived record of the request would be a strange way to express that."""
    answer = redact(
        result_of(a_blob({"client_name": "SNM", "salary": CANARIES["hr.salary"]})),
        entitlement=ent("read:blob.payload"),
        policy=POLICY,
    )
    assert set(type(answer.trace.dropped[0]).model_fields) == {"path", "entity", "reason"}
    assert CANARIES["hr.salary"] not in str(answer.trace.model_dump())


def test_a_trace_path_that_is_not_a_path_is_refused() -> None:
    """A mapping key can itself be the leak: in `{"SNM Construction Pte Ltd": "overdue"}`
    the key names the client, so recording it while redacting the value keeps the half
    that mattered."""
    answer = redact(
        result_of(a_blob({"SNM Construction Pte Ltd": {"note": "overdue"}})),
        entitlement=ent("read:blob.payload"),
        policy=POLICY,
    )
    assert "SNM" not in str(answer.trace.model_dump())


def test_a_scope_clause_reads_the_row_the_record_actually_carries() -> None:
    """The partner's second clause is what a single-clause bug drops. This asserts the
    walker passes the whole row to the scope, not just the department."""
    scope = Scope(
        clauses=(
            Clause(field="department", op=Op.EQ, value="sales"),
            Clause(field="partner_visible", op=Op.EQ, value="true"),
        )
    )

    class PartnerClient(Entity):
        name: str = "SNM Construction Pte Ltd"
        department: str = "sales"
        partner_visible: str = "false"

    policy = FieldPolicy(
        rules=(
            FieldRule.of("client", "name", "read:client.name", Classification.INTERNAL),
            FieldRule.of("client", "department", "read:client.name", Classification.INTERNAL),
            FieldRule.of("client", "partner_visible", "read:client.name", Classification.INTERNAL),
        )
    )
    entitlement = ent("read:client.name", scope=scope)
    hidden = serialise_for_channel(
        result_of(PartnerClient(entity="client", id="c_1")), entitlement=entitlement, policy=policy
    )
    shown = serialise_for_channel(
        result_of(PartnerClient(entity="client", id="c_2", partner_visible="true")),
        entitlement=entitlement,
        policy=policy,
    )
    assert hidden.records == ()
    assert shown.records[0]["name"] == "SNM Construction Pte Ltd"


# ================================================== M4.2.5 counts over collections
def test_a_count_of_a_filtered_collection_is_withheld() -> None:
    """The leak this leaf exists to close. Wei Ling may read a client's summary count and
    may read tickets only in maintenance, so a client carrying two tickets hands her one
    ticket and the number two. Two minus one is the hidden-item count the module promises
    never to emit, arriving through a field she legitimately holds."""
    record = ClientWithCount(
        entity="client", id="c_0447", tickets=(a_ticket(), a_ticket(department="web"))
    )
    kept = payload_for("u_weiling", record, policy=COUNTING_POLICY).records[0]
    assert len(kept["tickets"]) == 1
    assert "ticket_count" not in kept


def test_a_count_of_an_unfiltered_collection_is_still_visible() -> None:
    """The over-correction, and the failure that gets the rule switched off. If a count
    disappeared whenever anything anywhere was redacted, every count in the company would
    vanish within a week and somebody would delete the declaration rather than debug it."""
    record = ClientWithCount(entity="client", id="c_0447", tickets=(a_ticket(), a_ticket()))
    kept = payload_for("u_weiling", record, policy=COUNTING_POLICY).records[0]
    assert len(kept["tickets"]) == 2
    assert kept["ticket_count"] == 2


def test_a_count_of_a_collection_the_record_does_not_carry_stays_visible() -> None:
    """A summary record with a count and no bodies is the ordinary shape of a list view.
    There is nothing on screen to subtract the count from, so withholding it would refuse
    the feature in the exact case it was built for."""
    kept = payload_for(
        "u_weiling", SummaryClient(entity="client", id="c_0447"), policy=COUNTING_POLICY
    ).records[0]
    assert kept["ticket_count"] == 40
    assert "tickets" not in kept


def test_a_count_of_a_collection_withheld_whole_is_withheld() -> None:
    """The strongest form of the leak rather than an exemption from it. Forty beside no
    tickets at all says every one of the forty was withheld, which is a more complete
    answer than the subtraction a partly filtered list gives."""
    answer = redact(
        result_of(ClientWithCount(entity="client", id="c_0447", tickets=(a_ticket(),))),
        entitlement=ent("read:client.name"),
        policy=COUNTING_POLICY,
    )
    kept = answer.payload.records[0]
    assert "ticket_count" not in kept
    assert "tickets" not in kept


def test_a_count_over_something_that_is_not_a_sequence_is_withheld() -> None:
    """Default-deny applied to the declaration itself. A count over a mapping or a scalar
    is a claim this walker cannot check, and a check that cannot run is not a reason to
    emit a number."""
    record = ClientWithOneTicket(entity="client", id="c_0447", tickets=a_ticket())
    kept = payload_for("u_weiling", record, policy=COUNTING_POLICY).records[0]
    assert "ticket_count" not in kept
    assert kept["tickets"]["status"] == "open"


def test_a_count_over_a_nested_array_sees_a_loss_in_the_inner_array() -> None:
    """An array of arrays is one collection. Comparing only the outer length would keep a
    count of two beside one visible ticket, because the outer list still has both of its
    elements after the inner one lost a ticket."""
    entitlement = EntitlementSet(
        principal_id="u_test",
        grants=(
            Grant(capability=Capability(value="read:blob.payload"), scope=Scope.unrestricted()),
            Grant(capability=Capability(value="read:client.name"), scope=Scope.unrestricted()),
            Grant(
                capability=Capability(value="read:ticket.status"),
                scope=Scope.department("maintenance"),
            ),
        ),
    )
    nested = {
        "entity": "client",
        "id": "c_0447",
        "name": "SNM Construction Pte Ltd",
        "department": "maintenance",
        "ticket_count": 2,
        "tickets": [
            [{"entity": "ticket", "id": "t_1", "status": "open", "department": "maintenance"}],
            [{"entity": "ticket", "id": "t_2", "status": "open", "department": "web"}],
        ],
    }
    answer = redact(result_of(a_blob(nested)), entitlement=entitlement, policy=COUNTING_POLICY)
    kept = answer.payload.records[0]["payload"]
    assert [len(inner) for inner in kept["tickets"]] == [1, 0]
    assert "ticket_count" not in kept


def test_a_drop_inside_a_surviving_element_does_not_withhold_the_count() -> None:
    """A record absorbs what was pruned inside it. A ticket that survives is one ticket
    however much was removed from within it, so an untagged blob dropped inside one must
    not withhold the count of the list that ticket sits in. Delete this and the flag
    propagates upward, one odd shape from a connector empties every count in the company,
    and the rule gets switched off rather than fixed."""
    policy = COUNTING_POLICY.with_rules(
        FieldRule.of("ticket", "nested", "read:ticket.status", Classification.INTERNAL)
    )
    nested = {
        "entity": "client",
        "id": "c_0447",
        "name": "SNM Construction Pte Ltd",
        "department": "maintenance",
        "ticket_count": 1,
        "tickets": [
            {
                "entity": "ticket",
                "id": "t_1",
                "status": "open",
                "department": "maintenance",
                "nested": {"note": "untagged"},
            }
        ],
    }
    answer = redact(
        result_of(a_blob(nested)),
        entitlement=ent("read:blob.payload", "read:client.name", "read:ticket.status"),
        policy=policy,
    )
    kept = answer.payload.records[0]["payload"]
    assert DropReason.UNTAGGED in [d.reason for d in answer.trace.dropped]
    assert len(kept["tickets"]) == 1
    assert kept["ticket_count"] == 1


def test_a_withheld_count_carries_no_lock() -> None:
    """A lock here would appear exactly when the list beside it was filtered and never
    otherwise, so its presence would say "records were withheld from that list" to anybody
    who knew the rule. It would also lead nowhere: the caller already holds the count's
    capability, so granting it again changes nothing."""
    record = ClientWithCount(
        entity="client", id="c_0447", tickets=(a_ticket(), a_ticket(department="web"))
    )
    payload = payload_for("u_weiling", record, policy=COUNTING_POLICY)
    assert "ticket_count" not in {item.field for item in payload.locked}


def test_a_withheld_count_is_recorded_in_the_trace_with_its_own_reason() -> None:
    """The auditor has to be able to tell a count withheld because the caller lacked the
    grant from one withheld because the list beside it came back short. They are different
    events: the first is a permission answer, the second is a policy declaration doing its
    job, and only the second says a collection was filtered."""
    record = ClientWithCount(
        entity="client", id="c_0447", tickets=(a_ticket(), a_ticket(department="web"))
    )
    answer = redact(
        result_of(record),
        entitlement=person("u_weiling").entitlement(),
        policy=COUNTING_POLICY,
        now=NOW,
    )
    reasons = {r.field: r.reason for r in answer.trace.redactions}
    assert reasons["ticket_count"] == RedactionReason.FILTERED_COLLECTION.value
    assert "client.ticket_count" in answer.trace.withheld_field_names()


def test_a_record_whose_only_visible_field_was_a_withheld_count_is_absent() -> None:
    """The count check has to run before the substance check, and this is what proves it.
    A record whose one remaining field is a count of a collection the caller may not see
    is a record they can learn nothing about, so it collapses into nothing-found like any
    other record with no visible field."""
    answer = redact(
        result_of(
            a_blob(
                {
                    "entity": "client",
                    "id": "c_0447",
                    "ticket_count": 2,
                    "tickets": [{"entity": "ticket", "id": "t_1", "status": "open"}],
                }
            )
        ),
        entitlement=ent("read:blob.payload", "read:client.name"),
        policy=COUNTING_POLICY,
    )
    assert answer.payload.records == ()
    assert DropReason.NO_VISIBLE_FIELD in [d.reason for d in answer.trace.dropped]


def test_a_rule_may_declare_the_collection_its_field_counts() -> None:
    """The declaration lives on the policy rather than in the walker, because the person
    adding a summary column to a connector is not the person who reads the walker, and a
    rule that has to be written in the walker is a rule that does not get written."""
    rule = COUNTING_POLICY.rule_for("client", "ticket_count")
    assert rule is not None
    assert rule.counts == "tickets"
    assert rule.is_a_count
    plain = COUNTING_POLICY.rule_for("client", "name")
    assert plain is not None
    assert not plain.is_a_count


def test_a_field_cannot_declare_that_it_counts_itself() -> None:
    """A count of itself makes the walker's question circular. Refused at authoring time,
    where a typo is cheap, rather than handled at request time, where failing closed would
    hide it."""
    with pytest.raises(ValidationError, match="counts itself"):
        FieldRule.of(
            "client",
            "ticket_count",
            "read:client.name",
            Classification.INTERNAL,
            counts="ticket_count",
        )


def test_a_count_declaration_must_name_a_field() -> None:
    """The walker looks the declaration up as a key on the record, and a key that is not a
    name never survives the walk, so a declaration that is not a name could never match
    anything and would quietly mean "never filtered"."""
    with pytest.raises(ValidationError, match="not a field name"):
        FieldRule.of(
            "client",
            "ticket_count",
            "read:client.name",
            Classification.INTERNAL,
            counts="SNM Construction",
        )


def test_declaring_a_count_changes_the_policy_epoch() -> None:
    """Declaring a count tightens what an answer may contain. An epoch that ignored it
    would leave every cached answer still emitting the number it was just told to withhold,
    which is the same window a capability change without an epoch bump leaves."""
    assert COUNTING_POLICY.epoch() != POLICY.epoch()
    undeclared = COUNTING_POLICY.with_rules(
        FieldRule.of("client", "ticket_count", "read:client.name", Classification.INTERNAL)
    )
    assert undeclared.epoch() != COUNTING_POLICY.epoch()


# ============================================== M4.4.1 the only path to a channel
def a_slack_adapter(payload: ChannelPayload, room: str) -> None:
    """An adapter shaped the way every adapter should be: a payload and some scalars."""


def an_adapter_handed_the_whole_answer(answer: RedactedAnswer, room: str) -> None:
    """The mistake this refuses: reaching for the answer to get the source name too."""


def an_adapter_handed_the_trace(payload: ChannelPayload, trace: RedactionTrace) -> None:
    """The same mistake dressed as telemetry."""


def an_adapter_with_an_unannotated_parameter(payload: ChannelPayload, extra) -> None:  # type: ignore[no-untyped-def]
    """An unannotated parameter can hold the unredacted answer.

    The missing annotation is the test input, so mypy is told to allow it here rather than
    the fixture being annotated into something the check would pass."""


def an_adapter_taking_anything(payload: ChannelPayload, **kw: object) -> None:
    """A signature that accepts anything has declared nothing."""


def an_adapter_that_fetches_its_own_data(room: str) -> None:
    """No payload at all, which means it is getting its data from somewhere else."""


def test_a_channel_adapter_taking_only_a_payload_and_scalars_is_accepted() -> None:
    """The check has to admit the shape every real adapter has, or nobody registers one
    and the guarantee goes back to being a comment."""
    assert_channel_adapter(a_slack_adapter)


def test_a_channel_adapter_handed_the_whole_answer_is_refused() -> None:
    """The rule is enforced on what an adapter can be given rather than on what it does.
    An adapter handed a RedactedAnswer reaches the trace, the reasons and the dropped
    records, and every one of those is a hidden-item count or a value."""
    with pytest.raises(ChannelPathError, match="RedactedAnswer"):
        assert_channel_adapter(an_adapter_handed_the_whole_answer)


def test_a_channel_adapter_handed_the_trace_is_refused() -> None:
    """The trace is the longest-lived artifact of an answer and it holds counts by design,
    so an adapter that can read it can serialise a count of hidden items to a person."""
    with pytest.raises(ChannelPathError, match="RedactionTrace"):
        assert_channel_adapter(an_adapter_handed_the_trace)


def test_a_channel_adapter_with_an_unannotated_parameter_is_refused() -> None:
    """Default-deny, in the same shape as an unclassified field. An unannotated parameter
    can hold anything at all, including the whole answer, so it cannot be shown safe."""
    with pytest.raises(ChannelPathError, match="unannotated"):
        assert_channel_adapter(an_adapter_with_an_unannotated_parameter)


def test_a_channel_adapter_taking_kwargs_is_refused() -> None:
    """A signature that accepts anything has declared nothing, so there is nothing here to
    read and no basis on which to accept it."""
    with pytest.raises(ChannelPathError, match=r"\*args or \*\*kwargs"):
        assert_channel_adapter(an_adapter_taking_anything)


def test_a_channel_adapter_that_takes_no_payload_is_refused() -> None:
    """An adapter with no payload is fetching its own data, which is precisely the path
    around the serialiser this exists to close."""
    with pytest.raises(ChannelPathError, match="takes no ChannelPayload"):
        assert_channel_adapter(an_adapter_that_fetches_its_own_data)


def test_the_registry_returns_the_adapter_unchanged_and_records_that_it_passed() -> None:
    """Returning a wrapper would put the redaction module in the call path of every message
    the company sends, and a redaction module that can break message delivery is one
    somebody eventually routes around."""
    registry = ChannelAdapterRegistry()
    assert registry.register(a_slack_adapter) is a_slack_adapter
    assert registry.names() == ("a_slack_adapter",)
    assert len(registry) == 1


def test_the_registry_refuses_an_adapter_that_would_not_pass_the_check() -> None:
    """The registry is where the check is unavoidably applied. If it registered first and
    checked afterwards, the guarantee would depend on somebody reading a log."""
    registry = ChannelAdapterRegistry()
    with pytest.raises(ChannelPathError):
        registry.register(an_adapter_handed_the_whole_answer)
    assert registry.names() == ()


def test_two_different_adapters_cannot_share_one_name() -> None:
    """One of them would be unreachable and which one is decided by import order, so the
    channel a message went out on would depend on the order two modules were loaded."""

    def build() -> Callable[[ChannelPayload], None]:
        def an_adapter(payload: ChannelPayload) -> None:
            """A fresh function object with the same qualified name every time."""

        return an_adapter

    registry = ChannelAdapterRegistry()
    registry.register(build())
    with pytest.raises(ChannelPathError, match="two different channel adapters"):
        registry.register(build())


def test_registering_the_same_adapter_twice_is_not_a_clash() -> None:
    """A module imported twice, or a registry rebuilt in a test, is normal. Refusing it
    would teach people to guard registration with a flag, and that flag is where a real
    clash would then hide."""
    registry = ChannelAdapterRegistry()
    registry.register(a_slack_adapter)
    registry.register(a_slack_adapter)
    assert len(registry) == 1


# ============================================ M4.4.2 the tool side of the boundary
class NotAnEntity(BaseModel):
    """A valid TypedResult member carrying no entity tag."""

    name: str


def a_well_typed_tool(department: str) -> TypedResult[Ticket]:
    """What every tool must look like."""
    raise NotImplementedError


def a_tool_returning_a_dict(department: str) -> dict[str, Any]:
    """The shape the redactor has no entity to ask a capability question about."""
    raise NotImplementedError


def a_tool_with_no_return_annotation(department: str):  # type: ignore[no-untyped-def]
    """A shape nobody has stated. Unannotated on purpose; see the adapter fixture above."""
    raise NotImplementedError


def a_tool_returning_a_bare_typed_result(department: str) -> TypedResult:  # type: ignore[type-arg]
    """Type-checks, and promises only that something came back in a box."""
    raise NotImplementedError


def a_tool_returning_untagged_records(department: str) -> TypedResult[NotAnEntity]:
    """A TypedResult of a model carrying no entity tag."""
    raise NotImplementedError


def test_a_tool_declaring_a_typed_result_passes_registration() -> None:
    """The check has to admit the shape every real tool has, or a registry applying it can
    register nothing and gets bypassed."""
    assert_tool_returns_typed_result(a_well_typed_tool)


def test_a_tool_returning_an_untyped_shape_is_refused_at_registration() -> None:
    """`require_typed_result` is this same rule one request too late: by then somebody has
    asked a question, a connector has been called, and the answer is an exception.
    Refusing at registration means the tool never becomes callable at all."""
    with pytest.raises(UntypedShapeError, match="only TypedResult"):
        assert_tool_returns_typed_result(a_tool_returning_a_dict)


def test_a_tool_with_no_declared_return_is_refused_at_registration() -> None:
    """Default-deny for shapes. An unannotated return is not "probably fine", it is a
    shape nobody has stated, and the redactor cannot check one of those."""
    with pytest.raises(UntypedShapeError, match="declares no return type"):
        assert_tool_returns_typed_result(a_tool_with_no_return_annotation)


def test_a_bare_typed_result_is_refused_at_registration() -> None:
    """It type-checks and it promises nothing. TypedResult is generic over BaseModel, so a
    bare one says only that something came back in a box, and the entity parameter is the
    whole promise the redactor depends on."""
    with pytest.raises(UntypedShapeError, match="only TypedResult"):
        assert_tool_returns_typed_result(a_tool_returning_a_bare_typed_result)


def test_a_typed_result_of_something_that_is_not_an_entity_is_refused() -> None:
    """The second half of the hole `require_typed_result` closes at request time. A model
    with no entity tag satisfies the generic bound and still gives the redactor nothing to
    look a capability up by."""
    with pytest.raises(UntypedShapeError, match="not an Entity"):
        assert_tool_returns_typed_result(a_tool_returning_untagged_records)
