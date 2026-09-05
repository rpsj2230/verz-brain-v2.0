"""A refusal must not confirm the thing exists. A failure here blocks deploy.

The single most important property in the taxonomy: DENIED and ABSENT must be
indistinguishable to a person. "You are not allowed to see the contract value for SNM"
confirms that SNM has a contract value, and the permission model then leaks through its own
error messages, which is a leak nobody reviews because it does not look like data.

Written because the traceability sweep found M0.2.7 claimed in the source with no test
naming it anywhere. The claim had been on the tracker since the first day.

Task ids: M0.2.7
"""

from __future__ import annotations

import pytest

from brain.core.errors import (
    Absent,
    BrainError,
    Degraded,
    Denied,
    Failed,
    Outcome,
    Unresolved,
    to_public,
)

pytestmark = pytest.mark.invariant


# ------------------------------------------- denied and absent are one thing outside
def test_a_denial_and_an_absence_read_identically() -> None:
    """The property the whole taxonomy exists to produce. If these ever differ, asking for
    a record you cannot see tells you it is there."""
    assert to_public(Denied()) == to_public(Absent())


def test_that_holds_even_when_the_denial_carries_detail() -> None:
    """Detail is for the audit log. A detail that reaches the person is the leak arriving
    by a different route."""
    denied = Denied("caller lacks read:client.contract_value on client 447")
    assert to_public(denied) == to_public(Absent())
    assert "contract_value" not in to_public(denied)
    assert "447" not in to_public(denied)


def test_the_public_message_never_says_permission() -> None:
    """ "Not authorised" is itself the confirmation. The words have to be about finding,
    not about permission."""
    text = to_public(Denied()).lower()
    for word in ("permission", "denied", "not allowed", "unauthorised", "forbidden", "access"):
        assert word not in text


def test_the_outcomes_stay_distinguishable_internally() -> None:
    """DENIED exists so the audit log can record what actually happened. Collapsing the two
    on the inside as well would leave nothing able to answer "was she refused, or was it
    genuinely not there"."""
    assert Denied().outcome is Outcome.DENIED
    assert Absent().outcome is Outcome.ABSENT
    assert Denied().outcome is not Absent().outcome


# --------------------------------------------------------------- the other three
def test_an_unresolved_name_says_so_without_listing_the_candidates() -> None:
    """Listing them is the disclosure: "did you mean Acme Holdings or Acme Marine" names
    two clients to someone who may be entitled to neither."""
    text = to_public(Unresolved())
    assert "more than one" in text.lower()


def test_a_degraded_answer_admits_the_gap_rather_than_filling_it() -> None:
    """The alternative is answering from what could be reached and not saying so, which is
    a wrong answer delivered confidently."""
    assert "could not reach" in to_public(Degraded()).lower()


def test_every_outcome_has_a_public_message() -> None:
    """A missing one falls back to the base class, and the base class message is generic
    enough to be useless at exactly the moment someone needs to know what happened."""
    for error in (Denied(), Absent(), Unresolved(), Degraded(), Failed()):
        assert error.public_message.strip()


def test_every_member_of_the_taxonomy_is_represented_by_a_class() -> None:
    """The five are named in the architecture. A sixth outcome with no class, or a class
    with no outcome, means one of the two is out of date."""
    classes = (Denied, Absent, Unresolved, Degraded, Failed)
    assert {c.outcome for c in classes} == set(Outcome)
    assert len(classes) == len(Outcome)


# ------------------------------------------------------------------- the base
def test_an_error_carries_an_outcome_and_never_a_bare_string() -> None:
    """A bare string is a thing someone formats into a reply. An outcome is a thing the
    dispatcher has to branch on."""
    assert isinstance(Failed().outcome, Outcome)
    assert issubclass(Denied, BrainError)


def test_detail_is_kept_for_the_log_and_separate_from_the_public_message() -> None:
    error = Denied("row 447 outside scope department=maintenance")
    assert "447" in error.detail
    assert "447" not in error.public_message


def test_a_caller_can_override_the_public_message_deliberately() -> None:
    """Some paths genuinely have something safe and specific to say. It has to be an
    explicit act, not the default."""
    error = Degraded("xero timeout", public_message="I could not reach Xero just now.")
    assert to_public(error) == "I could not reach Xero just now."
