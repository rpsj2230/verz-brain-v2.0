"""The typed result envelope and the tool contract.

Task ids: M0.2.5, M0.2.6
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from brain.core.envelope import (
    Entity,
    IdentityMode,
    Redaction,
    SideEffect,
    ToolDefinition,
    TypedResult,
)


class Client(Entity):
    name: str
    hours_remaining: int | None = None


def a_client(**kw: object) -> Client:
    return Client(entity="client", id="c_0447", name="SNM Construction Pte Ltd", **kw)  # type: ignore[arg-type]


# ---------------------------------------------------------------- Entity
def test_entity_tag_must_be_a_lowercase_identifier() -> None:
    """The tag is what the redactor looks up capabilities by, so it has to be a stable
    identifier and not free text."""
    for bad in ("Client", "client-record", "1client", ""):
        with pytest.raises(ValidationError):
            Client(entity=bad, id="c_1", name="x")


def test_entity_rejects_unknown_fields() -> None:
    """extra='forbid' means a connector cannot smuggle an unmodelled field past the
    redactor by attaching it to a record."""
    with pytest.raises(ValidationError):
        Client(entity="client", id="c_1", name="x", contract_value=48000)  # type: ignore[call-arg]


# ------------------------------------------------------------ TypedResult
def test_empty_result_is_valid_and_reports_no_records() -> None:
    r: TypedResult[Client] = TypedResult()
    assert r.record_count() == 0
    assert not r.was_redacted()


def test_result_carries_records_and_counts_them() -> None:
    r: TypedResult[Client] = TypedResult(records=(a_client(hours_remaining=12), a_client()))
    assert r.record_count() == 2
    assert r.records[0].hours_remaining == 12


def test_redactions_are_recorded_not_silently_dropped() -> None:
    """A removed field leaves a trace. Without this, an answer that was quietly narrowed
    is indistinguishable from one where the data never existed — and the console could
    not report a redaction rate at all."""
    r: TypedResult[Client] = TypedResult(
        records=(a_client(),),
        redactions=(Redaction(entity="client", record_id="c_0447", field="contract_value"),),
    )
    assert r.was_redacted()
    assert r.redactions[0].field == "contract_value"
    assert r.redactions[0].reason == "no grant"


def test_truncation_is_explicit() -> None:
    """Freshdesk search returns at most 300 records ever. A truncated result must say so,
    or 'there are no more' and 'we stopped looking' become the same answer."""
    r: TypedResult[Client] = TypedResult(records=(a_client(),), truncated=True)
    assert r.truncated


def test_result_records_its_source_and_fetch_time() -> None:
    r: TypedResult[Client] = TypedResult(source="lark_base", fetched_at="2026-09-04T14:31:00Z")
    assert r.source == "lark_base"
    assert r.fetched_at.endswith("Z")


# --------------------------------------------------------- ToolDefinition
def a_tool(**kw: object) -> ToolDefinition:
    base: dict[str, object] = {
        "name": "laravel.get_client",
        "description": "Use this to look up a client's hosting and domain expiry.",
        "entity": "client",
        "required_capability": "read:client.name",
    }
    return ToolDefinition(**(base | kw))  # type: ignore[arg-type]


def test_tool_name_must_be_source_dot_action() -> None:
    """The catalogue is projected per request and the model picks by name, so the grammar
    is what keeps 'which system does this touch' answerable from the name alone."""
    for bad in ("get_client", "Laravel.get_client", "laravel.get-client", "laravel."):
        with pytest.raises(ValidationError):
            a_tool(name=bad)


def test_a_read_tool_is_read_only() -> None:
    assert a_tool().is_read_only()
    assert a_tool(side_effect=SideEffect.NONE).is_read_only()


def test_anything_with_a_side_effect_is_not_read_only() -> None:
    for effect in (SideEffect.DRAFT, SideEffect.WRITE, SideEffect.SEND, SideEffect.MONEY):
        assert not a_tool(side_effect=effect).is_read_only()


def test_money_boundary_is_detectable_from_the_definition() -> None:
    """The leash pins money tools shut regardless of rung, so the gate has to be able to
    ask this question without consulting anything else."""
    assert a_tool(side_effect=SideEffect.MONEY).crosses_money_boundary()
    assert not a_tool(side_effect=SideEffect.SEND).crosses_money_boundary()


def test_identity_mode_defaults_to_delegated() -> None:
    """Delegated means the source enforces its own permissions too. Defaulting to SERVICE
    would mean a forgotten setting silently runs on a shared credential."""
    assert a_tool().identity_mode is IdentityMode.DELEGATED


def test_tool_definition_is_frozen() -> None:
    """A tool definition that could be mutated after catalogue projection would let a
    later step widen what an earlier check approved."""
    t = a_tool()
    with pytest.raises(ValidationError):
        t.required_capability = "read:client.contract_value"


def test_tool_requires_a_capability() -> None:
    with pytest.raises(ValidationError):
        ToolDefinition(  # type: ignore[call-arg]
            name="laravel.get_client",
            description="No capability declared.",
            entity="client",
        )
