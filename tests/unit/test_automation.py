"""The automation sandbox: what it may reach, what it may hold, and what it may not decide.

Every test here is about a flow written by somebody outside this repository's review.

Task ids: M32.6.1.2, M32.6.1.4, M32.6.2.1, M32.6.2.2
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.scope import Scope
from brain.ops.automation import (
    DETERMINISTIC_ONLY,
    EGRESS_ALLOWLIST,
    AutomationError,
    StepKind,
    assert_deterministic,
    credential_leaks,
    egress_refusals,
    enabled_for,
    flow_reach,
)


def _entitlement(
    *pairs: tuple[str, Scope],
    principal_id: str = "u_weiling",
    not_after: datetime | None = None,
) -> EntitlementSet:
    return EntitlementSet(
        principal_id=principal_id,
        grants=tuple(Grant(capability=Capability(value=v), scope=s) for v, s in pairs),
        not_after=not_after,
    )


# --------------------------------------------------- deterministic only (M32.6.2.1)
def test_there_is_no_step_kind_that_asks_a_model_what_to_do_next() -> None:
    """The boundary is a vocabulary, not a rule flows are asked to follow: agent control
    flow cannot be described here, so it cannot run here. Agent runs in this system are
    leashed, entitled, approved and audited, and a canvas that could branch on a model's
    answer would be a second agent runtime with none of that. Delete this and a
    `MODEL_CALL` member is added in a diff that reads as a feature."""
    values = {k.value for k in StepKind}
    for forbidden in ("model", "llm", "agent", "prompt", "decide", "reason"):
        assert not any(forbidden in v for v in values), values
    assert values == {"http_request", "tool_call", "transform", "branch", "delay"}


def test_a_flow_containing_an_unknown_step_is_refused_rather_than_skipped() -> None:
    """Flow descriptors arrive as JSON from a canvas on its own release schedule, so an
    unrecognised step is the normal way a new step type appears. Skipping it means it runs
    on the canvas and is ignored here, which is the boundary not existing. Delete this and
    the closed set is closed only against steps somebody remembered to name."""
    with pytest.raises(AutomationError, match="not one of"):
        assert_deterministic([{"kind": "tool_call"}, {"kind": "ask_the_model"}])
    with pytest.raises(AutomationError, match="not one of"):
        assert_deterministic([{"name": "step one"}])
    assert_deterministic([{"kind": "http_request"}, {"kind": "branch"}])


def test_the_rule_is_stated_where_a_refusal_quotes_it() -> None:
    """A refusal that says only "invalid step" makes the author guess, and guessing wrong
    twice is how a rule gets worked around instead of followed. Delete this and the message
    stops explaining itself."""
    assert "agent run" in DETERMINISTIC_ONLY
    with pytest.raises(AutomationError, match="agent runtime"):
        assert_deterministic([{"kind": "llm_step"}])


# --------------------------------------------------- egress (M32.6.1.2)
def test_a_host_that_merely_ends_with_an_allowlisted_name_is_refused() -> None:
    """The bug this allowlist is written to avoid: `"notapi.xero.com".endswith("api.xero.com")`
    is true, so a suffix check admits any host somebody can register whose name ends in the
    right characters, and the failure looks exactly like the allowlist working. Delete this
    and `endswith` looks like the obvious implementation, because it is."""
    assert egress_refusals(["open.larksuite.com"]) == ()
    assert len(egress_refusals(["notapi.xero.com"])) == 1
    assert len(egress_refusals(["api.xero.com.attacker.net"])) == 1


def test_a_trailing_dot_or_a_capital_does_not_get_a_host_past_the_allowlist() -> None:
    """`API.XERO.COM.` is the same host to DNS and a different string to a comparison.
    Delete this and the allowlist is bypassed by typing."""
    assert egress_refusals(["API.XERO.COM."]) == ()
    assert egress_refusals([" api.xero.com "]) == ()


def test_the_allowlist_holds_only_hosts_somebody_decided_on() -> None:
    """It is read as permission by whoever adds the next entry, so a host that is merely
    convenient must not already be on it. Delete this and it grows."""
    assert "localhost" not in EGRESS_ALLOWLIST
    assert not any(entry.startswith("*") for entry in EGRESS_ALLOWLIST)


# --------------------------------------------------- credentials (M32.6.1.2)
def test_a_connection_string_under_an_innocent_variable_name_is_found() -> None:
    """A name check alone misses `BRAIN_UPSTREAM=postgresql://...`, which is a full
    credential filed under a harmless name. Delete this and the only protection is that
    nobody names a variable badly."""
    leaks = credential_leaks({"BRAIN_UPSTREAM": "postgresql://brain:pw@db:5432/brain"})
    assert len(leaks) == 1
    assert "connection string" in leaks[0]


def test_a_credential_shaped_name_is_found_whatever_its_value_looks_like() -> None:
    """A value check alone misses `VAULT_TOKEN=hvs.CAESIH...`, which is the most dangerous
    variable in the set and looks like any other opaque string. Delete this and the vault
    token is the one thing that gets through."""
    leaks = credential_leaks({"VAULT_TOKEN": "hvs.CAESIH", "POSTGRES_PASSWORD": "x"})
    assert len(leaks) == 2


def test_an_ordinary_variable_that_merely_contains_the_word_key_is_not_a_leak() -> None:
    """A checker that refuses `MONKEY` and `TURKEY` is a checker somebody switches off.
    Delete this and the substring match comes back."""
    assert credential_leaks({"MONKEY": "banana", "KEYBOARD_LAYOUT": "us"}) == ()


def test_the_leak_report_names_the_variable_and_never_quotes_its_value() -> None:
    """A checker that quoted what it found would put the credential into whatever log the
    check writes to, which is the failure it is looking for. Delete this and the report
    becomes the leak."""
    secret = "postgresql://brain:hunter2@db:5432/brain"
    leaks = credential_leaks({"BRAIN_UPSTREAM": secret})
    assert all("hunter2" not in leak for leak in leaks)


# --------------------------------------------------- enablement (M32.6.1.4)
def test_the_canvas_is_off_for_a_client_who_was_never_asked() -> None:
    """Absent means off, because a client who has not been asked has not agreed. Delete
    this and a missing key becomes whatever the reading code's default is."""
    assert enabled_for({}) is False
    assert enabled_for({"activepieces": False}) is False
    assert enabled_for({"activepieces": True}) is True


def test_a_string_enablement_value_is_refused_rather_than_coerced() -> None:
    """`"false"` is truthy in Python, in JavaScript, and in the YAML a compose file's
    environment section produces. A feature that is meant to be off and is on is the exact
    shape of failure this module is a boundary against. Delete this and the sandbox is
    enabled by a quoted string in a config file."""
    with pytest.raises(AutomationError, match="must be a"):
        enabled_for({"activepieces": "false"})
    with pytest.raises(AutomationError, match="must be a"):
        enabled_for({"activepieces": 1})


# --------------------------------------------------- reach (M32.6.2.2)
def test_a_flow_cannot_reach_a_capability_its_caller_does_not_hold() -> None:
    """The property the leaf asks to be proved, against the real `EntitlementSet` rather
    than a stub of it. The flow's declaration is a ceiling and never a grant. Delete this
    and a flow authored on the canvas can name any capability it likes."""
    caller = _entitlement(("read:client.name", Scope.unrestricted()))
    flow = _entitlement(
        ("read:client.name", Scope.unrestricted()),
        ("read:client.contract_value", Scope.unrestricted()),
    )
    reach = flow_reach(caller, flow)
    assert reach.holds(Capability(value="read:client.name"))
    assert not reach.holds(Capability(value="read:client.contract_value"))


def test_a_flow_cannot_widen_the_scope_its_caller_holds_a_capability_in() -> None:
    """Wider is the direction that matters. A flow declaring an unrestricted scope on a
    capability its caller holds in one department must come out holding it in that
    department. Delete this and department isolation ends at the canvas."""
    caller = _entitlement(("read:client.name", Scope.department("web")))
    flow = _entitlement(("read:client.name", Scope.unrestricted()))
    scope = flow_reach(caller, flow).scope_for(Capability(value="read:client.name"))
    assert scope is not None
    assert not scope.is_unrestricted()
    assert scope.matches({"department": "web"})
    assert not scope.matches({"department": "sales"})


def test_a_flow_narrows_and_the_narrowing_survives() -> None:
    """The other direction: a flow may hold less than its caller, and a flow scoped to one
    department must not be widened by a caller who holds everything. Delete this and a
    time-boxed or department-boxed flow silently inherits the caller's whole reach."""
    caller = _entitlement(("read:client.name", Scope.unrestricted()))
    flow = _entitlement(("read:client.name", Scope.department("sales")))
    scope = flow_reach(caller, flow).scope_for(Capability(value="read:client.name"))
    assert scope is not None
    assert scope.matches({"department": "sales"})
    assert not scope.matches({"department": "web"})


def test_a_flows_own_expiry_binds_as_well_as_the_callers() -> None:
    """A time-boxed automation must not outlive its box because the caller has no expiry.
    `intersect` takes the tighter of the two bounds, and this is the case where the tighter
    one belongs to the flow. Delete this and the expiry on a flow is decorative."""
    soon = datetime.now(UTC) + timedelta(minutes=5)
    caller = _entitlement(("read:client.name", Scope.unrestricted()))
    flow = _entitlement(("read:client.name", Scope.unrestricted()), not_after=soon)
    assert flow_reach(caller, flow).not_after == soon


def test_the_reach_belongs_to_the_caller_and_not_to_the_flow() -> None:
    """Whose principal id the resulting set carries decides whose name is on every audit row
    the run writes. A flow acting under its own identity is a flow whose actions cannot be
    traced to the person who triggered it. Delete this and the canvas becomes an
    unattributable actor."""
    caller = _entitlement(("read:client.name", Scope.unrestricted()), principal_id="u_weiling")
    flow = _entitlement(("read:client.name", Scope.unrestricted()), principal_id="flow_nightly")
    assert flow_reach(caller, flow).principal_id == "u_weiling"
