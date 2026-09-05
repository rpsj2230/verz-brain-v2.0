"""The route from a lock to an owner, and the silence that goes back to the asker.

These are the mechanics of `brain.core.access_route`. The properties that must never break
live beside the redaction invariants in `tests/invariants/test_redaction_invariants.py`,
because the rule they protect is the redaction module's rule rather than this module's: a
refusal and an absence are the same event, and a request-access route is the feature most
likely to break that by being helpful.

The asymmetry is what almost every test here is about. The owner is meant to learn
everything, so a test that only checked the asker's half would pass against a route that
sent an empty notice nobody could act on.

Task ids: M4.3.4
"""

from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from brain.core.access_route import (
    AskerAcknowledgement,
    CapabilityOwner,
    OwnerDirectory,
    RoutedRequest,
    route_access_request,
)
from brain.core.entitlement import Capability
from brain.core.field_policy import Classification, FieldPolicy, FieldRule
from brain.core.redaction import ASKER_ACKNOWLEDGEMENT, LockedField, OwnerNotice

POLICY: FieldPolicy = FieldPolicy(
    rules=(
        FieldRule.of("client", "name", "read:client.name", Classification.INTERNAL),
        FieldRule.of(
            "client", "contract_value", "read:client.contract_value", Classification.RESTRICTED
        ),
        FieldRule.of("client", "margin", "read:client.margin", Classification.RESTRICTED),
        FieldRule.of("hr", "salary", "read:hr.salary", Classification.RESTRICTED),
    )
)


def owner(capability: str, principal_id: str) -> CapabilityOwner:
    return CapabilityOwner(capability=Capability(value=capability), principal_id=principal_id)


#: Aaron owns client data in general, Daniel owns the one field that is more sensitive than
#: the rest, and nobody at all owns HR salary. The third case is the one that matters: a
#: classified field with no owner is normal in a company that has just started classifying.
OWNERS: OwnerDirectory = OwnerDirectory(
    owners=(
        owner("read:client.*", "u_aaron"),
        owner("read:client.contract_value", "u_dual"),
    )
)


def a_lock(entity: str = "client", field: str = "contract_value") -> LockedField:
    return LockedField(entity=entity, record_id="c_0447", field=field)


def route(
    locked: LockedField,
    *,
    asker_id: str = "u_weiling",
    question: str = "What is SNM worth to us this year?",
    policy: FieldPolicy = POLICY,
    owners: OwnerDirectory = OWNERS,
) -> RoutedRequest:
    return route_access_request(
        locked, asker_id=asker_id, question=question, policy=policy, owners=owners
    )


# ================================================== what the owner is told
def test_the_owner_learns_who_asked_what_they_asked_and_what_would_answer_it() -> None:
    """A request stripped down to "grant read:client.contract_value to u_weiling" is a
    request nobody can judge. Delete this and the route still returns something, and every
    owner approves or refuses without knowing what they are deciding about."""
    routed = route(a_lock())
    assert routed.notice == OwnerNotice(
        asker_id="u_weiling",
        entity="client",
        field="contract_value",
        question="What is SNM worth to us this year?",
        requested_capability=Capability(value="read:client.contract_value"),
    )
    assert routed.owner_id == "u_dual"


def test_the_capability_the_owner_is_asked_for_comes_from_the_policy() -> None:
    """The route must not invent `read:<entity>.<field>` from the lock. A field on one
    entity may legitimately answer to another entity's capability, and a request naming a
    capability nobody can grant is a request that goes nowhere and looks handled."""
    policy = POLICY.with_rules(
        FieldRule.of("client", "margin", "read:finance.margin", Classification.RESTRICTED)
    )
    directory = OwnerDirectory(owners=(owner("read:finance.*", "u_transfer"),))
    routed = route(a_lock(field="margin"), policy=policy, owners=directory)
    assert routed.notice is not None
    assert routed.notice.requested_capability == Capability(value="read:finance.margin")
    assert routed.owner_id == "u_transfer"


def test_a_wildcard_owner_covers_a_field_nobody_named() -> None:
    """A directory that had to name every field would be a directory nobody finishes, and
    an unfinished directory routes most requests nowhere. The wildcard is read through the
    same `Capability.covers` the entitlement model uses, so it cannot mean something else
    here than it means there."""
    routed = route(a_lock(field="name"))
    assert routed.owner_id == "u_aaron"


def test_the_most_specific_owner_wins_over_a_wildcard() -> None:
    """Otherwise one sensitive field cannot be given a different owner without rewriting
    the directory, and in practice it is left with the general owner, who then approves
    requests about a figure they were never meant to decide on."""
    assert OWNERS.owner_for(Capability(value="read:client.contract_value")) == "u_dual"
    assert OWNERS.owner_for(Capability(value="read:client.margin")) == "u_aaron"


def test_a_directory_naming_two_owners_for_one_capability_refuses_to_load() -> None:
    """Two owners makes "who approves this" depend on the order rows came out of a table,
    and the loser never learns a request existed. This is the same argument the field
    policy makes about conflicting rules, and it fails the same way: at load."""
    with pytest.raises(ValidationError, match="two owners for one capability"):
        OwnerDirectory(
            owners=(
                owner("read:client.margin", "u_aaron"),
                owner("read:client.margin", "u_hr"),
            )
        )


def test_the_same_owner_written_twice_is_not_a_conflict() -> None:
    """A directory assembled from two overlapping sources is normal. Refusing an exact
    duplicate would teach people to deduplicate by hand, which is where the real conflicts
    would then get lost."""
    directory = OwnerDirectory(
        owners=(owner("read:client.margin", "u_aaron"), owner("read:client.margin", "u_aaron"))
    )
    assert len(directory) == 1


# ============================================= what the asker is told, which is nothing
def test_the_asker_is_told_the_same_sentence_whether_or_not_an_owner_exists() -> None:
    """The oracle this closes. "There is no owner for that field" says the field does not
    exist, and it can be asked repeatedly with different guesses until the shape of the
    company falls out. Delete this and the difference between an owned field and an
    unowned one becomes readable from outside."""
    owned = route(a_lock())
    unowned = route(a_lock(entity="hr", field="salary"))
    assert unowned.notice is None
    assert owned.for_asker() == unowned.for_asker() == ASKER_ACKNOWLEDGEMENT


def test_an_unclassified_field_produces_no_notice_and_the_same_reply() -> None:
    """The policy can change between the answer that rendered the lock and the request
    made from it. Failing closed here keeps the asker's reply identical, so a policy
    change is not observable by asking about a field twice."""
    routed = route(a_lock(field="contract_value"), policy=FieldPolicy())
    assert routed.notice is None
    assert routed.owner_id == ""
    assert routed.for_asker() == ASKER_ACKNOWLEDGEMENT


def test_the_reply_to_the_asker_names_nothing_about_the_request() -> None:
    """Asserted over many different requests rather than one, because a reply that
    happened not to contain one field name would pass a single-case test while still being
    built from the request."""
    requests = [
        (pid, entity, field)
        for pid in ("u_weiling", "u_jason", "u_partner")
        for entity, field in (("client", "contract_value"), ("client", "margin"), ("hr", "salary"))
    ]
    for pid, entity, field in requests:
        reply = route(
            a_lock(entity=entity, field=field),
            asker_id=pid,
            question=f"why can I not see {field} for SNM",
        ).for_asker()
        assert reply == ASKER_ACKNOWLEDGEMENT
        assert entity not in reply
        assert field not in reply
        assert pid not in reply
        assert "SNM" not in reply


def test_the_acknowledgement_has_nowhere_to_put_anything() -> None:
    """The structural half of the argument, and the one that survives a refactor. A model
    with no fields cannot vary by entity, field, owner or outcome, in the same way and for
    the same reason `render_lock` takes no arguments."""
    assert set(AskerAcknowledgement.model_fields) == set()
    assert AskerAcknowledgement() == AskerAcknowledgement()
    assert AskerAcknowledgement().render() == ASKER_ACKNOWLEDGEMENT


def test_the_owners_half_and_the_askers_half_are_separate_values() -> None:
    """Two objects rather than one, so a caller holding a single variable cannot send the
    owner's half to the asker by reaching for the wrong attribute."""
    routed = route(a_lock())
    assert isinstance(routed.notice, OwnerNotice)
    assert isinstance(routed.acknowledgement, AskerAcknowledgement)
    assert routed.for_asker() == ASKER_ACKNOWLEDGEMENT


# ==================================================== the shape of the transition
def test_the_route_takes_a_lock_so_it_cannot_be_pointed_at_a_withheld_record() -> None:
    """A lock is only ever offered on a record the caller was already entitled to see, so
    asking about it discloses nothing new. A record withheld whole produces no lock, so
    taking a `LockedField` is what stops "request access to the record you were not shown"
    from ever being written. Delete this and the parameter widens to a string pair, and the
    rule that a refusal and an absence are the same event goes with it."""
    parameter = inspect.signature(route_access_request).parameters["locked"]
    assert parameter.annotation == "LockedField"


def test_a_routed_request_carries_a_notice_and_an_owner_together_or_neither() -> None:
    """The half-built shape fails silently: a notice with no owner id is dropped by
    whatever tries to deliver it, the asker has already been told their request was passed
    on, and nobody finds out for a quarter."""
    with pytest.raises(ValidationError, match="together or neither"):
        RoutedRequest(owner_id="u_aaron")
    with pytest.raises(ValidationError, match="together or neither"):
        RoutedRequest(
            notice=OwnerNotice(
                asker_id="u_weiling",
                entity="client",
                field="contract_value",
                question="why",
                requested_capability=Capability(value="read:client.contract_value"),
            )
        )


def test_the_question_reaches_the_owner_unchanged() -> None:
    """The owner is deciding whether this person should see this field for this reason,
    and the reason is the question. A route that summarised it would be deciding on the
    owner's behalf which parts of the reason mattered."""
    asked = "Priya needs SNM's renewal figure before Thursday's call. Can I see it?"
    routed = route(a_lock(), question=asked)
    assert routed.notice is not None
    assert routed.notice.question == asked


def test_a_question_that_is_empty_is_refused_rather_than_routed() -> None:
    """An empty question is a request the owner cannot judge, so it is refused where it is
    made rather than delivered as a decision nobody can take. The bound comes from
    `AccessRequest`, and routing through that domain object rather than building an
    `OwnerNotice` directly is what makes it apply to every routed request."""
    with pytest.raises(ValidationError):
        route(a_lock(), question="")
