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

Task ids: M4.1.1, M4.1.3, M4.1.4, M4.1.5, M4.2.2, M4.2.4, M4.3.1, M4.3.2, M4.3.3, M4.4.1,
M4.4.2, M4.4.3, M4.4.4
"""

from __future__ import annotations

import inspect
import random
from typing import Any

import pytest

from brain.core import redaction as redaction_module
from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.envelope import Entity, TypedResult
from brain.core.errors import Denied
from brain.core.field_policy import Classification, FieldPolicy, FieldRule, policy_from_rows
from brain.core.redaction import (
    LOCK_TEXT,
    RESERVED_KEYS,
    ChannelPayload,
    UntypedShapeError,
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
