"""Permission canaries: tests whose correct answer is a refusal.

The point of these is inverted from an ordinary test. They do not check that the right
data comes back; they check that the wrong data does not. A permission bug that widens
access passes every test written the normal way, because more data is still valid data —
it is only wrong relative to who asked.

So each of these asks for something the persona must not reach and fails if it arrives.
A failure here blocks deploy.

Task ids: M0.6.2, M0.6.3, M0.6.4
"""

from __future__ import annotations

import pytest

from brain.core.entitlement import Capability
from brain.core.principal import Employment, PrincipalKind
from tests.fixtures.company import NOW, canary_tokens, everyone, person

pytestmark = pytest.mark.invariant


def cap(v: str) -> Capability:
    return Capability(value=v)


# --------------------------------------------------------------- the fixture
def test_the_company_covers_the_shapes_that_break_things() -> None:
    """A tidy org chart tests nothing. These are the cases a real company produces."""
    people = everyone()
    assert len(people) == 12

    employments = {p.principal.employment for p in people.values()}
    assert Employment.CONTRACTOR in employments
    assert Employment.PARTNER in employments
    assert Employment.SERVICE in employments

    # expiry in both directions
    assert person("u_contractor").principal.is_active(NOW)
    assert not person("u_expired").principal.is_active(NOW)

    # someone in two departments, and someone holding a grant where they no longer sit
    assert len(person("u_dual").grants[0].scope.clauses) == 1
    assert any("web" in str(g.scope.model_dump()) for g in person("u_transfer").grants)


def test_scheduled_work_is_a_principal_like_any_other() -> None:
    """There is no principal that bypasses the gate, which is why this one is called a
    service and not the system."""
    svc = person("svc_sentinel")
    assert svc.principal.kind is PrincipalKind.SERVICE
    assert svc.grants  # it holds grants; it is not exempt from needing them
    assert not svc.entitlement().holds(cap("read:client.name"))


# ------------------------------------------------------------- the canaries
@pytest.mark.parametrize("pid", sorted(everyone()))
def test_nobody_reaches_what_their_persona_forbids(pid: str) -> None:
    """The core canary. Each persona declares what it must never reach; this asserts it."""
    p = person(pid)
    ent = p.entitlement()
    for forbidden in p.forbidden:
        assert not ent.holds(cap(f"read:{forbidden}"), NOW), (
            f"{pid} ({p.note}) reached {forbidden}, which its persona forbids"
        )


def test_the_sees_record_not_money_persona_is_exactly_that() -> None:
    """Screen 3's locked field, as an assertion. Wei Ling sees the client and the hours,
    and must never see what it is worth."""
    ent = person("u_weiling").entitlement()
    assert ent.holds(cap("read:client.name"))
    assert ent.holds(cap("read:client.hours_remaining"))
    assert not ent.holds(cap("read:client.contract_value"))
    assert not ent.holds(cap("read:client.margin"))


def test_one_person_can_see_a_field_in_one_department_and_not_another() -> None:
    """Daniel reads contract value in sales and not in web. Field-level and scope-level
    at once, inside a single person — the case a per-user permission cache gets wrong."""
    scope = person("u_dual").entitlement().scope_for(cap("read:client.contract_value"))
    assert scope is not None
    assert scope.matches({"department": "sales"})
    assert not scope.matches({"department": "web"})


def test_a_two_clause_partner_scope_narrows_on_both() -> None:
    """A bug that keeps only the first clause would let a partner see every sales client
    rather than only the ones marked visible to partners."""
    scope = person("u_partner").entitlement().scope_for(cap("read:client.name"))
    assert scope is not None
    assert scope.matches({"department": "sales", "partner_visible": "true"})
    assert not scope.matches({"department": "sales", "partner_visible": "false"})
    assert not scope.matches({"department": "web", "partner_visible": "true"})


def test_only_one_person_may_read_salary() -> None:
    """Everyone else lists it as forbidden. If a second person gains it, this fails and
    names them."""
    holders = [pid for pid, p in everyone().items() if p.entitlement().holds(cap("read:hr.salary"))]
    assert holders == ["u_hr"], f"unexpected salary readers: {holders}"


def test_an_admin_is_not_born_holding_everything() -> None:
    """A Super Admin's reach is a grant set like anyone's. That is what makes it
    auditable, and reducible."""
    assert not person("u_rupash").entitlement().holds(cap("read:hr.salary"))


# ------------------------------------------------------- the canary strings
def test_every_restricted_field_has_an_improbable_value() -> None:
    """A leaked 48000 looks like data. A leaked CANARY-CONTRACT-7Q4XZ is unmistakable,
    greppable, and cannot be something the model invented."""
    tokens = canary_tokens()
    assert len(tokens) == 7
    assert all(t.startswith("CANARY-") for t in tokens)
    # no token is a substring of another, so an assertion on one cannot pass by accident
    for t in tokens:
        assert sum(1 for other in tokens if t in other) == 1


def test_canaries_cover_every_entity_that_holds_something_restricted() -> None:
    from tests.fixtures.company import CANARIES

    entities = {k.split(".", 1)[0] for k in CANARIES}
    assert entities == {"client", "hr", "ticket", "invoice", "agent"}


# ------------------------------------------------------------------ expiry
def test_expiry_beats_grants_that_are_still_on_file() -> None:
    """Elena's grants were never revoked. Access has to stop anyway, and it stops when
    the entitlement is built rather than when the session opened."""
    elena = person("u_expired")
    assert elena.grants  # the grants are still there
    assert not elena.principal.is_active(NOW)
    # and, the part that was missing until these canaries ran: the entitlement refuses
    assert not elena.entitlement().holds(cap("read:client.name"), NOW)
    assert elena.entitlement().is_expired(NOW)


def test_a_live_contractor_still_works() -> None:
    """The opposite failure: expiry logic that denies everyone would also pass the test
    above, so this one has to exist beside it."""
    marcus = person("u_contractor")
    assert marcus.principal.is_active(NOW)
    assert marcus.entitlement().holds(cap("read:client.name"), NOW)
