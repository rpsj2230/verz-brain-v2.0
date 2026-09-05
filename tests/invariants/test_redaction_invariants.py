"""Redaction rules that must never break. A failure here blocks deploy.

These are inverted tests, in the sense the canary suite describes: they do not check that
the right data comes back, they check that the wrong data does not. A permission bug that
widens access passes every test written the normal way, because more data is still valid
data and is only wrong relative to who asked.

Three groups, and they fail for different reasons.

**The canaries** ask the synthetic company. Every restricted field holds an improbable
string, so a leak is unmistakable and greppable rather than a plausible number somebody
argues about.

**The structural properties** assert shapes rather than values: that the payload has
nowhere to put a count, that the lock rendering cannot vary, that only one function in the
module returns something a channel can accept.

**The generative properties** (M4.4.3) run the walker over shapes nobody wrote by hand.
Hypothesis is not a dependency of this project and adding one to the core test suite was
not worth it, so the generation is exhaustive over a bounded shape space and then
pseudo-random with fixed seeds beyond it. Fixed seeds rather than a clock, so a failure
here is reproducible by re-running rather than by luck.

The policy is rebuilt here rather than imported from the unit suite. An invariant suite
that borrows its fixtures from a unit suite stops being an independent check the first
time somebody tidies the unit suite up.

Task ids: M4.1.1, M4.1.3, M4.1.4, M4.1.5, M4.2.2, M4.2.4, M4.2.5, M4.3.1, M4.3.2, M4.3.3,
M4.3.4, M4.4.1, M4.4.2, M4.4.3, M4.4.4
"""

from __future__ import annotations

import inspect
import random
from typing import Any

import pytest
from pydantic import ValidationError

from brain.core import access_route as access_route_module
from brain.core import redaction as redaction_module
from brain.core.access_route import CapabilityOwner, OwnerDirectory, route_access_request
from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.envelope import Entity, Redaction, TypedResult
from brain.core.errors import Denied
from brain.core.field_policy import Classification, FieldPolicy, FieldRule, policy_from_rows
from brain.core.redaction import (
    ASKER_ACKNOWLEDGEMENT,
    LOCK_TEXT,
    RESERVED_KEYS,
    UNREDACTED_TYPE_NAMES,
    ChannelPathError,
    ChannelPayload,
    DroppedObject,
    LockedField,
    Mask,
    RedactedAnswer,
    RedactionTrace,
    UntypedShapeError,
    assert_channel_adapter,
    assert_tool_returns_typed_result,
    compute_mask,
    redact,
    render_lock,
    serialise_for_channel,
)
from brain.core.scope import Scope
from tests.fixtures.company import CANARIES, NOW, canary_tokens, everyone, person

pytestmark = pytest.mark.invariant


# ---------------------------------------------------------------- the shapes
class Ticket(Entity):
    department: str = "maintenance"
    status: str = "open"
    subject: str = "SSL renewal"
    internal_note: str = CANARIES["ticket.internal_note"]


class Client(Entity):
    name: str = "SNM Construction Pte Ltd"
    department: str = "maintenance"
    hosting_expiry: str = "2026-11-14"
    hours_remaining: int = 12
    contract_value: str = CANARIES["client.contract_value"]
    margin: str = CANARIES["client.margin"]
    tickets: tuple[Ticket, ...] = ()


class HrRecord(Entity):
    department: str = "maintenance"
    salary: str = CANARIES["hr.salary"]
    performance_note: str = CANARIES["hr.performance_note"]


class Invoice(Entity):
    department: str = "finance"
    status: str = "overdue"
    amount_due: str = CANARIES["invoice.amount_due"]


class AgentRecord(Entity):
    department: str = "maintenance"
    name: str = "Site Health Sentinel"
    system_prompt: str = CANARIES["agent.system_prompt"]


class Blob(Entity):
    """An entity whose payload is whatever a connector felt like returning."""

    payload: Any = None


#: One record per canary, so a single answer exercises every restricted field at once.
def every_canary_record() -> tuple[Entity, ...]:
    return (
        Client(entity="client", id="c_0447", tickets=(Ticket(entity="ticket", id="t_9"),)),
        HrRecord(entity="hr", id="h_1"),
        Invoice(entity="invoice", id="i_1"),
        AgentRecord(entity="agent", id="a_1"),
    )


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
        ("ticket", "nested", "read:ticket.status", Classification.INTERNAL),
        ("hr", "department", "read:hr.department", Classification.INTERNAL),
        ("hr", "salary", "read:hr.salary", Classification.RESTRICTED),
        ("hr", "performance_note", "read:hr.performance_note", Classification.RESTRICTED),
        ("invoice", "department", "read:invoice.status", Classification.INTERNAL),
        ("invoice", "status", "read:invoice.status", Classification.INTERNAL),
        ("invoice", "amount_due", "read:invoice.amount_due", Classification.RESTRICTED),
        ("agent", "department", "read:agent.name", Classification.INTERNAL),
        ("agent", "name", "read:agent.name", Classification.INTERNAL),
        ("agent", "system_prompt", "read:agent.system_prompt", Classification.RESTRICTED),
        ("blob", "payload", "read:blob.payload", Classification.INTERNAL),
    ]
)


def unrestricted(*values: str) -> EntitlementSet:
    """An entitlement with no scope clauses.

    Used only by the generative properties. Scope is deliberately out of the picture
    there so the audit below can recompute a mask over the *output* tree and compare it
    exactly. A scoped grant would make the audit's row differ from the walker's, because
    the walker reads the row before deleting anything and the audit can only read what
    survived. Scope behaviour is asserted against the real company personas instead, which
    is where the awkward shapes live anyway.
    """
    return EntitlementSet(
        principal_id="u_generative",
        grants=tuple(
            Grant(capability=Capability(value=v), scope=Scope.unrestricted()) for v in values
        ),
    )


GENERATIVE_ASKERS: dict[str, EntitlementSet] = {
    "wide": unrestricted("read:blob.payload", "read:ticket.*", "read:client.*"),
    "middling": unrestricted("read:blob.payload", "read:ticket.status", "read:client.name"),
    "bare": unrestricted("read:blob.payload"),
    "nothing": unrestricted("invoke:agent"),
}


# ================================================================== the canaries
@pytest.mark.parametrize("pid", sorted(everyone()))
def test_no_canary_reaches_a_persona_that_does_not_hold_the_field(pid: str) -> None:
    """The core canary, applied to the redactor rather than to the entitlement set. A
    permission bug in the walker would pass every entitlement test and still put a salary
    on screen, because the entitlement was right and the thing that reads it was not."""
    entitlement = person(pid).entitlement()
    payload = serialise_for_channel(
        TypedResult(records=every_canary_record()),
        entitlement=entitlement,
        policy=POLICY,
        now=NOW,
    )
    text = str(payload.model_dump())
    for dotted, token in CANARIES.items():
        if not entitlement.holds(Capability(value=f"read:{dotted}"), NOW):
            assert token not in text, f"{pid} received {dotted} and holds no grant for it"


@pytest.mark.parametrize("pid", sorted(everyone()))
def test_a_personas_own_forbidden_list_is_enforced_by_the_redactor(pid: str) -> None:
    """Each persona declares what it must never reach. The entitlement suite asserts the
    grants; this asserts the data. Both are needed, because the interesting bugs live in
    the gap between what a person holds and what they are handed."""
    subject = person(pid)
    payload = serialise_for_channel(
        TypedResult(records=every_canary_record()),
        entitlement=subject.entitlement(),
        policy=POLICY,
        now=NOW,
    )
    text = str(payload.model_dump())
    for dotted in subject.forbidden:
        token = CANARIES.get(dotted)
        if token is not None:
            assert token not in text, f"{pid} ({subject.note}) received {dotted}"


@pytest.mark.parametrize("pid", sorted(everyone()))
def test_no_canary_ever_reaches_the_trace(pid: str) -> None:
    """The trace is the one artifact of an answer that outlives the answer, so a value in
    it is a permanent leak rather than a momentary one. It records field names and counts
    and nothing else."""
    answer = redact(
        TypedResult(records=every_canary_record()),
        entitlement=person(pid).entitlement(),
        policy=POLICY,
        now=NOW,
    )
    text = str(answer.trace.model_dump())
    for token in canary_tokens():
        assert token not in text, f"{pid}: {token} reached the trace"


def test_a_holder_of_the_grant_still_receives_the_field() -> None:
    """The opposite failure, and the one every canary suite needs beside it. A redactor
    that returned nothing at all would pass every test above and be useless. Meera is the
    only person who may read a salary and she must actually get it."""
    payload = serialise_for_channel(
        TypedResult(records=every_canary_record()),
        entitlement=person("u_hr").entitlement(),
        policy=POLICY,
        now=NOW,
    )
    text = str(payload.model_dump())
    assert CANARIES["hr.salary"] in text
    assert CANARIES["client.contract_value"] not in text


def test_a_department_admin_reaches_their_own_departments_money() -> None:
    """Aaron holds `read:client.*` inside maintenance, so the wildcard has to reach a
    field nobody named explicitly. A walker that only honoured exact capabilities would
    quietly narrow every wildcard grant in the company."""
    payload = serialise_for_channel(
        TypedResult(records=every_canary_record()),
        entitlement=person("u_aaron").entitlement(),
        policy=POLICY,
        now=NOW,
    )
    text = str(payload.model_dump())
    assert CANARIES["client.contract_value"] in text
    assert CANARIES["hr.salary"] not in text


def test_expiry_beats_a_grant_that_is_still_on_file() -> None:
    """Elena's grants were never revoked and she must still receive nothing. The check
    lives in the entitlement set, and this asserts the redactor actually asks it rather
    than caching a decision from when the session opened."""
    payload = serialise_for_channel(
        TypedResult(records=every_canary_record()),
        entitlement=person("u_expired").entitlement(),
        policy=POLICY,
        now=NOW,
    )
    assert payload.records == ()


# ============================================ denied and absent are the same event
def test_a_refusal_is_byte_identical_to_a_record_that_does_not_exist() -> None:
    """The single most important property in the module. A refusal that explains itself
    confirms the record exists, and every question after that one is a probe."""
    entitlement = person("u_jason").entitlement()
    refused = serialise_for_channel(
        TypedResult(records=every_canary_record(), source="lark_base", fetched_at="14:31"),
        entitlement=entitlement,
        policy=POLICY,
        now=NOW,
    )
    nothing_found = serialise_for_channel(
        TypedResult(), entitlement=entitlement, policy=POLICY, now=NOW
    )
    assert refused == nothing_found
    assert refused == ChannelPayload()


@pytest.mark.parametrize("pid", sorted(everyone()))
def test_a_persona_who_can_see_nothing_gets_the_same_payload_as_every_other(pid: str) -> None:
    """Two people comparing empty answers must not be able to tell which of them was
    refused and which asked about nothing. If the payloads differ by so much as a source
    name, the pair of them can map the company by comparing screens."""
    payload = serialise_for_channel(
        TypedResult(records=every_canary_record(), source="lark_base", fetched_at="14:31"),
        entitlement=person(pid).entitlement(),
        policy=POLICY,
        now=NOW,
    )
    if payload.records == ():
        assert payload == ChannelPayload()


def test_a_record_refused_among_visible_ones_leaves_no_gap() -> None:
    """Three refused records beside one visible record must look exactly like one visible
    record. A placeholder, a null, or a shortened list with a stated length would each be
    a hidden-item count written another way."""
    visible = Client(entity="client", id="c_1", department="maintenance")
    hidden = [Client(entity="client", id=f"c_{i}", department="web") for i in range(2, 5)]
    entitlement = person("u_weiling").entitlement()
    many = serialise_for_channel(
        TypedResult(records=(visible, *hidden)), entitlement=entitlement, policy=POLICY, now=NOW
    )
    one = serialise_for_channel(
        TypedResult(records=(visible,)), entitlement=entitlement, policy=POLICY, now=NOW
    )
    assert many == one


# =================================================================== default-deny
@pytest.mark.parametrize("pid", sorted(everyone()))
def test_an_unclassified_field_is_withheld_from_everybody(pid: str) -> None:
    """A field policy that does not mention a field is not permission to show it. This
    includes the Super Admin: an admin's reach is a grant set like anybody's, and a grant
    set says nothing about a field no policy classifies."""

    class Surprise(Entity):
        department: str = "maintenance"
        name: str = "SNM Construction Pte Ltd"
        added_by_a_connector_last_night: str = CANARIES["client.contract_value"]

    payload = serialise_for_channel(
        TypedResult(records=(Surprise(entity="client", id="c_0447"),)),
        entitlement=person(pid).entitlement(),
        policy=POLICY,
        now=NOW,
    )
    text = str(payload.model_dump())
    assert "added_by_a_connector_last_night" not in text
    assert CANARIES["client.contract_value"] not in text


@pytest.mark.parametrize("pid", sorted(everyone()))
def test_an_empty_policy_returns_nothing_to_anybody(pid: str) -> None:
    """The limit case of default-deny, and the state a fresh install is in. A policy with
    no rules must withhold everything rather than fall back to returning everything, which
    is the failure mode of every allow-unless-configured design."""
    payload = serialise_for_channel(
        TypedResult(records=every_canary_record()),
        entitlement=person(pid).entitlement(),
        policy=FieldPolicy(),
        now=NOW,
    )
    assert payload == ChannelPayload()


# ============================================================ the policy epoch
@pytest.mark.parametrize("index", range(len(POLICY.rules)))
def test_removing_any_single_rule_changes_the_epoch(index: int) -> None:
    """Exhaustive over the policy rather than a spot check. The epoch is in the answer
    cache key, and a rule whose removal did not move it would leave a window where a
    cached answer keeps disclosing a field that was just revoked."""
    rule = POLICY.rules[index]
    assert POLICY.without(rule.entity, rule.field).epoch() != POLICY.epoch()


@pytest.mark.parametrize("index", range(len(POLICY.rules)))
def test_reclassifying_any_single_rule_changes_the_epoch(index: int) -> None:
    """Classification changes what an answer may do even when it changes nobody's access,
    because it drives per-channel sensitivity policy and artifact retention. An epoch that
    ignored it would serve a cached answer through a channel that may no longer carry
    it."""
    rule = POLICY.rules[index]
    other = (
        Classification.PUBLIC
        if rule.classification is not Classification.PUBLIC
        else Classification.RESTRICTED
    )
    changed = POLICY.with_rules(
        FieldRule(
            entity=rule.entity,
            field=rule.field,
            required_capability=rule.required_capability,
            classification=other,
        )
    )
    assert changed.epoch() != POLICY.epoch()


# ============================================================= structural rules
def test_the_lock_rendering_cannot_vary_by_viewer() -> None:
    """Checked by reading the signature rather than by trusting the body. A lock that
    varied by viewer, field or reason would make its own shape a side channel two people
    could read by comparing screens."""
    assert inspect.signature(render_lock).parameters == {}
    assert render_lock() == LOCK_TEXT


@pytest.mark.parametrize("pid", sorted(everyone()))
def test_every_lock_in_every_answer_renders_the_same_string(pid: str) -> None:
    """The property behind the signature check, asserted over real answers so that a
    future lock carrying a per-field rendering is caught even if the signature stays."""
    payload = serialise_for_channel(
        TypedResult(records=every_canary_record()),
        entitlement=person(pid).entitlement(),
        policy=POLICY,
        now=NOW,
    )
    assert {item.render() for item in payload.locked} <= {LOCK_TEXT}


def test_the_payload_has_no_field_that_could_carry_a_count() -> None:
    """Never emit a count of hidden items. `extra="forbid"` plus this field set means a
    channel adapter cannot attach one later because it seemed helpful, and the check fails
    the moment somebody adds one here."""
    assert set(ChannelPayload.model_fields) == {
        "records",
        "locked",
        "label",
        "source",
        "fetched_at",
        "truncated",
    }


def test_only_one_function_in_the_module_returns_a_channel_payload() -> None:
    """ "The serializer is the only path to a channel" has to be a shape rather than a
    rule, or it is a rule somebody routes around when they need the trace as well."""
    returns_a_payload = sorted(
        name
        for name, obj in vars(redaction_module).items()
        if inspect.isfunction(obj)
        and not name.startswith("_")
        and inspect.signature(obj).return_annotation == "ChannelPayload"
    )
    assert returns_a_payload == ["serialise_for_channel"]


@pytest.mark.parametrize(
    "untyped",
    [
        {"entity": "client", "id": "c_1", "contract_value": CANARIES["client.contract_value"]},
        [{"entity": "client", "id": "c_1"}],
        CANARIES["client.contract_value"],
        None,
        42,
    ],
)
def test_an_untyped_shape_can_never_reach_a_channel(untyped: object) -> None:
    """Structural prevention rather than a convention. A tool that returned a bare dict
    would give the redactor no entity to ask a capability question about, so the honest
    answer is a refusal at the boundary rather than a best effort."""
    with pytest.raises(UntypedShapeError):
        serialise_for_channel(
            untyped,  # type: ignore[arg-type]
            entitlement=person("u_rupash").entitlement(),
            policy=POLICY,
            now=NOW,
        )


@pytest.mark.parametrize("pid", sorted(everyone()))
def test_nobody_in_the_company_can_open_the_opaque_escape_hatch(pid: str) -> None:
    """The hatch has its own capability and nobody in the fixture holds it, including the
    Super Admin. If a wildcard grant ever reached it, every department admin in the
    company would be able to bypass field-level redaction entirely."""
    with pytest.raises(Denied):
        redact(
            TypedResult(records=every_canary_record()),
            entitlement=person(pid).entitlement(),
            policy=POLICY,
            now=NOW,
            opaque=True,
        )


# ==================================================== generative shapes (M4.4.3)
def shape_atoms() -> list[Any]:
    """The leaves the generator composes.

    Each is a shape a real connector has produced somewhere: a scalar, an empty
    collection, an untagged blob, a tagged record missing its id, a tagged record of an
    entity nobody wrote a policy for, and a mapping with a key that is not a name.
    """
    return [
        None,
        0,
        "plain",
        [],
        {},
        {"note": "untagged", "salary": CANARIES["hr.salary"]},
        {"entity": "ticket", "status": "open", "internal_note": CANARIES["ticket.internal_note"]},
        {
            "entity": "ticket",
            "id": "t_1",
            "status": "open",
            "subject": "SSL renewal",
            "internal_note": CANARIES["ticket.internal_note"],
        },
        {
            "entity": "client",
            "id": "c_1",
            "name": "SNM Construction Pte Ltd",
            "contract_value": CANARIES["client.contract_value"],
        },
        {"entity": "unknown_entity", "id": "x_1", "anything": CANARIES["agent.system_prompt"]},
        {"Weird Key": CANARIES["hr.salary"], "entity": "ticket", "id": "t_2", "status": "open"},
    ]


def wrappers(inner: Any) -> list[Any]:
    """The three ways one shape sits inside another: an array, a nested entity, a mix."""
    return [
        [inner],
        {"entity": "ticket", "id": "t_w", "status": "open", "nested": inner},
        [inner, "scalar", {"note": "untagged"}, inner],
    ]


def exhaustive_shapes() -> list[Any]:
    """Every atom, and every atom under every wrapper, to three levels."""
    level0 = shape_atoms()
    level1 = [w for atom in level0 for w in wrappers(atom)]
    level2 = [w for shape in level1 for w in wrappers(shape)]
    return level0 + level1 + level2


def random_shape(rng: random.Random, depth: int = 0) -> Any:
    """A pseudo-random tree, for the widths and depths the exhaustive set does not reach."""
    if depth >= 4 or rng.random() < 0.3:
        return rng.choice(shape_atoms())
    kind = rng.randrange(3)
    if kind == 0:
        return [random_shape(rng, depth + 1) for _ in range(rng.randrange(4))]
    if kind == 1:
        return {
            "entity": "ticket",
            "id": f"t_{rng.randrange(1000)}",
            "status": "open",
            "nested": random_shape(rng, depth + 1),
        }
    return {"note": "untagged", "nested": random_shape(rng, depth + 1)}


def assert_tree_is_clean(node: Any, *, entitlement: EntitlementSet, where: str) -> None:
    """Every mapping is tagged, identified, non-empty and inside its own mask.

    This is the audit that makes the generative tests worth running. It re-derives the
    answer from the policy rather than comparing against a recorded output, so it catches
    a walker that is wrong in a way nobody thought to write down.
    """
    if isinstance(node, dict):
        tag = node.get("@entity", node.get("entity"))
        assert isinstance(tag, str) and tag, f"{where}: an untagged mapping survived: {node}"
        record_id = node.get("@id", node.get("id"))
        assert isinstance(record_id, str) and record_id, f"{where}: no record id: {node}"
        assert any(key not in RESERVED_KEYS for key in node), f"{where}: a husk survived: {node}"

        mask = compute_mask(
            tag,
            list(node),
            entitlement=entitlement,
            policy=POLICY,
            row={k: str(v) for k, v in node.items() if isinstance(v, str | int | float | bool)},
            now=NOW,
        )
        outside = set(node) - set(mask.allowed)
        assert not outside, f"{where}: {sorted(outside)} survived outside the mask on {tag}"
        for key, value in node.items():
            assert_tree_is_clean(value, entitlement=entitlement, where=f"{where}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            assert_tree_is_clean(item, entitlement=entitlement, where=f"{where}[{index}]")


def redacted_payload(shape: Any, entitlement: EntitlementSet) -> Any:
    answer = redact(
        TypedResult(records=(Blob(entity="blob", id="b_1", payload=shape),)),
        entitlement=entitlement,
        policy=POLICY,
        now=NOW,
    )
    if not answer.payload.records:
        return None
    return answer.payload.records[0].get("payload")


@pytest.mark.parametrize("asker", sorted(GENERATIVE_ASKERS))
def test_no_generated_shape_leaks_a_key_outside_the_mask(asker: str) -> None:
    """The general property the canaries are a special case of. If a walker mishandles a
    shape nobody wrote a test for, this finds it, because the audit re-derives the answer
    from the policy instead of comparing against a recorded output."""
    entitlement = GENERATIVE_ASKERS[asker]
    for index, shape in enumerate(exhaustive_shapes()):
        out = redacted_payload(shape, entitlement)
        assert_tree_is_clean(out, entitlement=entitlement, where=f"{asker}/exhaustive[{index}]")


@pytest.mark.parametrize("asker", sorted(GENERATIVE_ASKERS))
def test_no_random_shape_leaks_a_key_outside_the_mask(asker: str) -> None:
    """The same property at widths and depths the exhaustive set does not reach. Seeds are
    fixed, so a failure is reproducible by re-running rather than by luck."""
    entitlement = GENERATIVE_ASKERS[asker]
    for seed in range(200):
        shape = random_shape(random.Random(seed))  # noqa: S311 - shapes, not secrets
        out = redacted_payload(shape, entitlement)
        assert_tree_is_clean(out, entitlement=entitlement, where=f"{asker}/seed{seed}")


@pytest.mark.parametrize("asker", sorted(GENERATIVE_ASKERS))
def test_no_generated_shape_leaks_a_canary(asker: str) -> None:
    """Stated separately from the mask audit, because the two fail for different reasons.
    A mask bug is a logic error; a canary in the output is the breach itself."""
    entitlement = GENERATIVE_ASKERS[asker]
    reachable = {
        token
        for dotted, token in CANARIES.items()
        if entitlement.holds(Capability(value=f"read:{dotted}"), NOW)
    }
    for index, shape in enumerate(exhaustive_shapes()):
        text = str(redacted_payload(shape, entitlement))
        for token in canary_tokens() - reachable:
            assert token not in text, f"{asker}/exhaustive[{index}] leaked {token}"


@pytest.mark.parametrize("asker", sorted(GENERATIVE_ASKERS))
def test_redacting_a_redacted_shape_changes_nothing(asker: str) -> None:
    """Idempotence. If a second pass removes more, the first pass was wrong; if it removes
    less, the walker is not deterministic. Either way the answer a person saw is not the
    answer the system would defend."""
    entitlement = GENERATIVE_ASKERS[asker]
    for index, shape in enumerate(exhaustive_shapes()):
        once = redacted_payload(shape, entitlement)
        twice = redacted_payload(once, entitlement)
        assert once == twice, f"{asker}/exhaustive[{index}] is not stable under a second pass"


def test_a_generated_shape_that_should_survive_does_survive() -> None:
    """The generative suite would pass entirely if the walker returned nothing, so one
    case has to assert the opposite. A wide asker gets the nested ticket back intact."""
    shape = {"entity": "ticket", "id": "t_1", "status": "open", "subject": "SSL renewal"}
    out = redacted_payload([shape], GENERATIVE_ASKERS["wide"])
    assert out == [shape]


# ============================================ counts over collections (M4.2.5)
class CountingClient(Entity):
    """A client whose summary count sits beside the collection it counts."""

    name: str = "SNM Construction Pte Ltd"
    department: str = "maintenance"
    ticket_count: int = 2
    tickets: tuple[Ticket, ...] = ()


#: The same policy with the count declared. Added rather than folded in, so every property
#: above still runs against a policy with no counts in it and the count rule cannot become
#: load-bearing for a test that is about something else.
COUNTING_POLICY: FieldPolicy = POLICY.with_rules(
    FieldRule.of(
        "client", "ticket_count", "read:client.name", Classification.INTERNAL, counts="tickets"
    )
)

#: The same rule with the `counts` link removed, so a comparison isolates the declaration.
#:
#: Comparing against `POLICY` instead would compare two different things at once: `POLICY`
#: classifies no `ticket_count` at all, so default-deny withholds it there, and the count
#: rule would look like a widening when what actually widened was classifying the field.
COUNTED_BUT_UNDECLARED: FieldPolicy = POLICY.with_rules(
    FieldRule.of("client", "ticket_count", "read:client.name", Classification.INTERNAL)
)


def a_counted_client() -> CountingClient:
    """Two tickets in two departments, and a count that is honestly two."""
    return CountingClient(
        entity="client",
        id="c_0447",
        tickets=(
            Ticket(entity="ticket", id="t_1", department="maintenance"),
            Ticket(entity="ticket", id="t_2", department="web"),
        ),
    )


def test_a_count_that_survives_always_matches_the_list_beside_it() -> None:
    """The arithmetic form of "never emit a count of hidden items", and the only form that
    catches this leak. There is no canary token to grep for here: the leak is a subtraction
    the asker performs, so the property has to be that no subtraction is ever available.
    Either the count and the list agree, or the count is not there at all.

    The last assertion is the half that stops the whole thing passing vacuously. A walker
    that withheld every count would satisfy the loop above and be useless."""
    seen_a_count = False
    for pid in sorted(everyone()):
        payload = serialise_for_channel(
            TypedResult(records=(a_counted_client(),)),
            entitlement=person(pid).entitlement(),
            policy=COUNTING_POLICY,
            now=NOW,
        )
        for record in payload.records:
            if "ticket_count" not in record:
                continue
            seen_a_count = True
            assert "tickets" in record, f"{pid} received a count with no list beside it"
            assert record["ticket_count"] == len(record["tickets"]), (
                f"{pid} can subtract {record['ticket_count']} from {len(record['tickets'])}"
            )
    assert seen_a_count, "no persona received a count, so the property above proved nothing"


@pytest.mark.parametrize("pid", sorted(everyone()))
def test_a_count_is_never_locked_however_it_was_withheld(pid: str) -> None:
    """A lock on a count would appear exactly when the list beside it was filtered and
    never otherwise, so its presence would tell anybody who knew the rule that records had
    been withheld from that list. Withholding the count silently makes the answer identical
    to a record whose source never carried a count."""
    payload = serialise_for_channel(
        TypedResult(records=(a_counted_client(),)),
        entitlement=person(pid).entitlement(),
        policy=COUNTING_POLICY,
        now=NOW,
    )
    assert "ticket_count" not in {item.field for item in payload.locked}


@pytest.mark.parametrize("pid", sorted(everyone()))
def test_declaring_a_count_never_widens_what_a_persona_receives(pid: str) -> None:
    """A rule that only ever takes away. If declaring a count could add a field to
    somebody's answer, the declaration would be a permission written in the wrong table,
    and nobody reviewing grants would ever see it."""
    records = (a_counted_client(),)
    entitlement = person(pid).entitlement()
    without = serialise_for_channel(
        TypedResult(records=records),
        entitlement=entitlement,
        policy=COUNTED_BUT_UNDECLARED,
        now=NOW,
    )
    with_counts = serialise_for_channel(
        TypedResult(records=records), entitlement=entitlement, policy=COUNTING_POLICY, now=NOW
    )
    for before, after in zip(without.records, with_counts.records, strict=False):
        assert set(after) <= set(before)
    assert len(with_counts.records) <= len(without.records)


# =========================================== the request-access route (M4.3.4)
#: Somebody owns client data; nobody owns HR salary. The pair is the point: the asker must
#: not be able to tell the two apart, and an unowned classified field is the normal state
#: of a company that has just started classifying.
ROUTE_OWNERS: OwnerDirectory = OwnerDirectory(
    owners=(CapabilityOwner(capability=Capability(value="read:client.*"), principal_id="u_aaron"),)
)


def test_the_asker_learns_the_same_sentence_from_every_refusal() -> None:
    """The single most important property of the route, and the reason it exists as one
    function. Owned or unowned, classified or not, whoever asked and whatever they asked
    about, the reply is one constant. Any variation at all is an oracle that can be asked
    repeatedly with different guesses until the shape of the company falls out."""
    replies = set()
    for pid in sorted(everyone()):
        for entity, field in (
            ("client", "contract_value"),
            ("client", "margin"),
            ("hr", "salary"),
            ("invoice", "amount_due"),
            ("agent", "system_prompt"),
        ):
            routed = route_access_request(
                LockedField(entity=entity, record_id="c_0447", field=field),
                asker_id=pid,
                question=f"why can I not see {field} for SNM Construction",
                policy=COUNTING_POLICY,
                owners=ROUTE_OWNERS,
            )
            replies.add(routed.for_asker())
    assert replies == {ASKER_ACKNOWLEDGEMENT}


def test_nothing_about_a_request_survives_into_the_askers_reply() -> None:
    """Stated separately from the constant above because the two fail for different
    reasons. A reply could be one constant per asker and still pass the first test if the
    loop happened to build only one; this one asserts the reply carries no canary, no name
    and no field, whatever went in."""
    routed = route_access_request(
        LockedField(entity="client", record_id="c_0447", field="contract_value"),
        asker_id="u_weiling",
        question=f"SNM's figure is {CANARIES['client.contract_value']}, why is it hidden",
        policy=COUNTING_POLICY,
        owners=ROUTE_OWNERS,
    )
    reply = routed.for_asker()
    assert all(token not in reply for token in canary_tokens())
    assert "SNM" not in reply
    assert "contract_value" not in reply
    assert "u_weiling" not in reply


def test_only_one_function_turns_a_refusal_into_a_request() -> None:
    """ "The route is the single place that transition happens" has to be a shape rather
    than a rule. A lock is the one refusal it is safe to offer a route from, because the
    record was legitimately disclosed first; a second function taking one is a second
    chance to offer the route from a record that was withheld whole."""
    takes_a_lock = sorted(
        name
        for module in (redaction_module, access_route_module)
        for name, obj in vars(module).items()
        if inspect.isfunction(obj)
        and not name.startswith("_")
        and any(
            "LockedField" in str(p.annotation) for p in inspect.signature(obj).parameters.values()
        )
    )
    assert takes_a_lock == ["route_access_request"]


# ================================ the only path to a channel, checkable (M4.4.1)
def wants_a_typed_result(payload: ChannelPayload, result: TypedResult[Client]) -> None:
    """Unredacted rows, straight from the tool."""


def wants_a_redacted_answer(payload: ChannelPayload, answer: RedactedAnswer) -> None:
    """The payload and the trace together, which is the trace."""


def wants_a_trace(payload: ChannelPayload, trace: RedactionTrace) -> None:
    """Counts of hidden items, by design."""


def wants_a_redaction(payload: ChannelPayload, item: Redaction) -> None:
    """A field name and a reason, and the reason is what leaks."""


def wants_a_dropped_object(payload: ChannelPayload, item: DroppedObject) -> None:
    """One entry per record nobody may see, which is a count of them."""


def wants_a_mask(payload: ChannelPayload, mask: Mask) -> None:
    """Exactly what this caller was refused, field by field."""


def wants_an_entity(payload: ChannelPayload, record: Entity) -> None:
    """A record the walker never saw."""


#: One adapter per denied type, written out rather than generated, so that the name in the
#: denylist and the annotation a real adapter would carry are checked against each other.
UNREDACTED_ADAPTERS: tuple[tuple[str, Any], ...] = (
    ("TypedResult", wants_a_typed_result),
    ("RedactedAnswer", wants_a_redacted_answer),
    ("RedactionTrace", wants_a_trace),
    ("Redaction", wants_a_redaction),
    ("DroppedObject", wants_a_dropped_object),
    ("Mask", wants_a_mask),
    ("Entity", wants_an_entity),
)


def test_every_name_on_the_denylist_has_an_adapter_proving_it_is_refused() -> None:
    """Adding a shape to the denylist without a case here would leave the new entry
    untested, and an untested denylist entry is a typo waiting to happen: `RedactionTrace`
    misspelled once admits the trace to every channel in the company."""
    assert {name for name, _ in UNREDACTED_ADAPTERS} == set(UNREDACTED_TYPE_NAMES)


@pytest.mark.parametrize(
    ("name", "adapter"), UNREDACTED_ADAPTERS, ids=[n for n, _ in UNREDACTED_ADAPTERS]
)
def test_a_channel_adapter_holding_an_unredacted_shape_is_refused(name: str, adapter: Any) -> None:
    """Every one of these adapters also takes a ChannelPayload, so it is refused for
    holding the forbidden shape rather than for missing the payload. That distinction is
    the test: an adapter that takes the payload and reaches for the answer as well is
    exactly the shortcut that looks reasonable while writing it."""
    with pytest.raises(ChannelPathError, match=name):
        assert_channel_adapter(adapter)


def test_an_adapter_that_takes_only_a_payload_and_scalars_is_admitted() -> None:
    """The opposite failure, and the one that makes the check worth having. A check that
    refused every adapter would be removed within a day."""

    def send(payload: ChannelPayload, room: str, thread: str | None) -> None:
        """The shape every real adapter has."""

    # A bare call is the assertion: it raises or it does not. Comparing the return value
    # to None asserts that the function returns None, which every function does.
    assert_channel_adapter(send)


# ======================= no untyped shape from a tool, refused early (M4.4.2)
def returns_a_dict(department: str) -> dict[str, Any]:
    """No entity to ask a capability question about."""
    raise NotImplementedError


def returns_a_list(department: str) -> list[Client]:
    """Entities, and no envelope, so no source and no fetched_at either."""
    raise NotImplementedError


def returns_nothing_stated(department: str) -> Any:
    """A shape nobody has stated. Annotated `Any` because an unannotated function is a
    type error here, and the point is a return type that promises nothing."""
    raise NotImplementedError


@pytest.mark.parametrize(
    "tool", [returns_a_dict, returns_a_list, returns_nothing_stated], ids=["dict", "list", "bare"]
)
def test_a_tool_that_could_not_be_redacted_is_refused_before_it_is_ever_called(tool: Any) -> None:
    """The same refusal `require_typed_result` makes, moved to registration. At request
    time it is an outage in somebody's answer; at registration it is a build failure in
    front of the person who wrote the tool, which is where a contract violation belongs."""
    with pytest.raises(UntypedShapeError):
        assert_tool_returns_typed_result(tool)


def returns_a_typed_result(department: str) -> TypedResult[Client]:
    """What every tool must look like."""
    raise NotImplementedError


def test_a_tool_returning_a_typed_result_of_entities_is_admitted() -> None:
    """The opposite failure again. A check nothing passes is a check somebody deletes, and
    the deletion looks like a cleanup rather than a permission change."""
    assert_tool_returns_typed_result(returns_a_typed_result)


# ------------------------------- a visible field may not rebuild a withheld one (M7.5.2)
def test_a_withheld_field_cannot_be_reconstructed_from_visible_siblings() -> None:
    """Classifying `cost` as restricted achieves nothing while `sell_price` and `margin` are
    both visible, because cost is the subtraction. The caller withholds the output and the
    inputs quietly put it back.

    This lives on the mask rather than in a caller, and that placement is the whole point.
    It was first written one layer up, driving the mask from outside, and anything handing a
    row straight to `redact` bypassed it entirely - a guard that only applies on the path
    that remembers to call it is a guard on one path.

    Deleting this test makes a restricted field derivable by arithmetic, which is invisible
    in the answer: every field shown is one the caller was entitled to."""
    policy = FieldPolicy(
        rules=(
            FieldRule.of("item", "sell_price", "read:item.sell_price", Classification.INTERNAL),
            FieldRule.of("item", "margin", "read:item.margin", Classification.CONFIDENTIAL),
            FieldRule(
                entity="item",
                field="cost",
                required_capability=Capability(value="read:item.cost"),
                classification=Classification.RESTRICTED,
                derived_from=("sell_price", "margin"),
            ),
        )
    )
    # Holds both inputs and not the output, which is exactly the shape that leaks.
    holder = EntitlementSet(
        principal_id="u_weiling",
        grants=(
            Grant(capability=Capability(value="read:item.sell_price"), scope=Scope.unrestricted()),
            Grant(capability=Capability(value="read:item.margin"), scope=Scope.unrestricted()),
        ),
    )
    mask = compute_mask(
        "item",
        ["sell_price", "margin", "cost"],
        entitlement=holder,
        policy=policy,
        row={"sell_price": 100, "margin": 40, "cost": 60},
    )
    assert "cost" not in mask.allowed
    # The most sensitive input goes, so the field the company needs survives.
    assert "sell_price" in mask.allowed
    assert "margin" not in mask.allowed


def test_the_closure_keeps_going_until_nothing_is_derivable() -> None:
    """Withholding one field can make a second derivation resolvable that was not before. A
    single sweep leaves the second standing while every test about the first passes, which
    is the shape of a guard that looks like it works."""
    policy = FieldPolicy(
        rules=(
            FieldRule.of("item", "a", "read:item.a", Classification.PUBLIC),
            FieldRule.of("item", "b", "read:item.b", Classification.INTERNAL),
            FieldRule(
                entity="item",
                field="c",
                required_capability=Capability(value="read:item.c"),
                classification=Classification.RESTRICTED,
                derived_from=("a", "b"),
            ),
            FieldRule(
                entity="item",
                field="d",
                required_capability=Capability(value="read:item.d"),
                classification=Classification.RESTRICTED,
                derived_from=("a",),
            ),
        )
    )
    holder = EntitlementSet(
        principal_id="u_weiling",
        grants=(
            Grant(capability=Capability(value="read:item.a"), scope=Scope.unrestricted()),
            Grant(capability=Capability(value="read:item.b"), scope=Scope.unrestricted()),
        ),
    )
    mask = compute_mask(
        "item",
        ["a", "b", "c", "d"],
        entitlement=holder,
        policy=policy,
        row={"a": 1, "b": 2, "c": 3, "d": 4},
    )
    # `c` costs `b`; `d` then still resolves from `a` alone, so `a` goes too.
    assert mask.allowed == frozenset()


def test_a_derivation_of_a_field_the_caller_may_see_withholds_nothing() -> None:
    """Only a *withheld* field's derivation is worth closing over. Protecting the inputs of
    something the caller can read directly withholds data for no reason at all, and a
    redactor that removes what it did not need to is one people route around."""
    policy = FieldPolicy(
        rules=(
            FieldRule.of("item", "sell_price", "read:item.sell_price", Classification.INTERNAL),
            FieldRule.of("item", "margin", "read:item.margin", Classification.CONFIDENTIAL),
            FieldRule(
                entity="item",
                field="cost",
                required_capability=Capability(value="read:item.cost"),
                classification=Classification.RESTRICTED,
                derived_from=("sell_price", "margin"),
            ),
        )
    )
    everything = EntitlementSet(
        principal_id="u_rupash",
        grants=tuple(
            Grant(capability=Capability(value=v), scope=Scope.unrestricted())
            for v in ("read:item.sell_price", "read:item.margin", "read:item.cost")
        ),
    )
    mask = compute_mask(
        "item",
        ["sell_price", "margin", "cost"],
        entitlement=everything,
        policy=policy,
        row={"sell_price": 100, "margin": 40, "cost": 60},
    )
    assert mask.allowed == frozenset({"sell_price", "margin", "cost"})


def test_a_field_cannot_be_declared_as_derived_from_itself() -> None:
    """The closure would then withhold it in order to protect it, which is a rule that reads
    as working and removes the field from every answer."""
    with pytest.raises(ValidationError, match="derived from itself"):
        FieldRule(
            entity="item",
            field="cost",
            required_capability=Capability(value="read:item.cost"),
            classification=Classification.RESTRICTED,
            derived_from=("cost", "margin"),
        )
