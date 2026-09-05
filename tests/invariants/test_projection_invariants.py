"""What the database may keep. A failure here blocks deploy.

These are separate from the entitlement canaries and the distinction matters: those assert
nobody *sees* what they should not, these assert we never *store* what we should not. A
system can be perfect at the first and still be one query bug away from a breach if it
kept the data.

There are two doors into the projected tier and both are guarded here. `brain.core.projection`
is the ingest-time door: it sees the values a connector actually returned. `brain.connectors.
manifest` is the review-time door: it sees what a connector said it would return, in front of
whoever wrote it. Neither is redundant. Without the first, a connector that declares three
fields and returns thirteen is never caught; without the second, the thirteenth field is
caught in production, on the day the connector is switched on.

The last section is the one that keeps them honest: anything the review-time door admits must
also pass the ingest-time door. Two doors with different opinions is worse than one door,
because the strict one is the one somebody eventually routes around.

Task ids: M11.4.2, M11.4.3, M11.4.4, M11.4.5, M11.4.7
"""

from __future__ import annotations

from pathlib import Path

import pytest

from brain.connectors.manifest import (
    ChangeSignal,
    FieldShape,
    HotUse,
    ManifestError,
    ProjectedEntity,
    ProjectedField,
    failed_clauses,
    projectability,
)
from brain.core.projection import (
    MAX_LABEL_CHARS,
    MAX_PROJECTED_FIELDS,
    NEVER_PROJECT,
    ProjectionRefusedError,
    assert_projectable,
    check_projection,
    is_forbidden,
)
from brain.core.scope import Clause, Op, Scope
from tests.fixtures.company import CANARIES

pytestmark = pytest.mark.invariant

CONNECTORS = Path(__file__).resolve().parents[2] / "src" / "brain" / "connectors"


# ------------------------------------------------------------- the denylist
@pytest.mark.parametrize(
    "field",
    ["email", "phone", "address", "nric", "bank_details", "salary", "contract_value", "margin"],
)
def test_the_architectures_denylist_is_enforced(field: str) -> None:
    """These sit under "Federated - never stored" in the data tier table. Storing one
    converts a permission mistake into a breach: a bug over-returning a projected field
    leaks everything we kept, while the same bug over a federated field leaks only what
    that one question fetched."""
    assert is_forbidden(field)


@pytest.mark.parametrize(
    "field",
    [
        "employee_salary",
        "salary_band",
        "annual_salary",
        "contact_email",
        "email_address",
        "mobile_phone",
        "bank_account_number",
        "staff_nric",
        "passport_number",
    ],
)
def test_a_field_is_denied_by_shape_not_only_by_exact_name(field: str) -> None:
    """Listing every spelling a connector might use is a losing game. A connector author
    who writes `employee_salary` is not trying to evade the rule, and the rule should
    still hold."""
    assert is_forbidden(field)


@pytest.mark.parametrize(
    "field", ["id", "client_id", "status", "updated_at", "display_name", "hosting_expiry"]
)
def test_the_fields_a_pointer_actually_needs_are_allowed(field: str) -> None:
    """A denylist that blocks ids and timestamps blocks the projection itself. This test
    exists because over-broad patterns are the easy failure here."""
    assert not is_forbidden(field)


def test_the_denylist_is_a_constant_not_configuration() -> None:
    """A denylist an operator can edit is one that gets edited at 2am to make a feature
    work, and the field is then in the database permanently. Removing it later does not
    un-store it."""
    assert isinstance(NEVER_PROJECT, frozenset)


# ------------------------------------------------------------------- limits
def test_a_projection_wider_than_twelve_fields_is_refused() -> None:
    """The cap and the denylist are different rules and both are needed. The cap is what
    keeps the projection a pointer rather than a mirror: 40 MB at this scale against tens
    of gigabytes."""
    fields = {f"f{i}": i for i in range(MAX_PROJECTED_FIELDS + 1)}
    violations = check_projection("client", fields)
    assert any("mirror" in v.reason for v in violations)


def test_exactly_twelve_fields_is_allowed() -> None:
    """Off-by-one on a limit that refuses is worse than one that permits: it blocks a
    correct connector and the author works around the rule instead."""
    fields = {f"f{i}": i for i in range(MAX_PROJECTED_FIELDS)}
    assert check_projection("client", fields) == []


def test_a_long_string_is_a_payload_not_a_label() -> None:
    """A label identifies a record to a person. Past 120 characters it has stopped doing
    that and started carrying content, which is the thing we said we would not store."""
    violations = check_projection("client", {"label": "x" * (MAX_LABEL_CHARS + 1)})
    assert any("payload" in v.reason for v in violations)


def test_a_label_at_the_limit_is_fine() -> None:
    assert check_projection("client", {"label": "x" * MAX_LABEL_CHARS}) == []


# ------------------------------------------------------------- the canaries
def test_no_canary_field_can_be_projected() -> None:
    """The restricted fields the permission canaries protect are exactly the fields that
    must never be stored either. If one became projectable, a permission bug would leak
    the whole table rather than one record."""
    for dotted in CANARIES:
        field = dotted.split(".", 1)[1]
        if field in ("performance_note", "internal_note", "system_prompt", "amount_due"):
            continue  # not on the architecture's list; covered by entitlement, not storage
        assert is_forbidden(field), f"{field} carries a canary and yet may be projected"


# ------------------------------------------------------------- the boundary
def test_every_violation_is_reported_at_once() -> None:
    """One at a time turns writing a connector into a guessing game, where each fix
    reveals the next objection."""
    violations = check_projection("client", {"salary": 1, "email": "a@b.c", "label": "x" * 200})
    assert len(violations) >= 3


def test_the_boundary_raises_rather_than_warning() -> None:
    """A projection that logs and continues is a projection that stored the field."""
    with pytest.raises(ProjectionRefusedError, match="cannot be projected"):
        assert_projectable("client", {"salary": 48000})


def test_a_legitimate_projection_passes_silently() -> None:
    assert_projectable(
        "client",
        {
            "id": "c_0447",
            "name": "SNM Construction Pte Ltd",
            "department": "maintenance",
            "status": "active",
            "hosting_expiry": "2026-11-14",
            "updated_at": "2026-09-04T14:31:00Z",
        },
    )


def test_the_error_names_every_field_and_says_what_to_do() -> None:
    """A refusal that does not say "fetch it live" invites the author to rename the field
    rather than fix the design."""
    with pytest.raises(ProjectionRefusedError) as exc:
        assert_projectable("client", {"salary": 1, "nric": "S1234567A"})
    text = str(exc.value)
    assert "salary" in text
    assert "nric" in text
    assert "fetch it live" in text


# -------------------------------------------------- the review-time door (M11.4.2, M11.4.3)
def _fields(count: int, *, shape: FieldShape = FieldShape.STATUS) -> tuple[ProjectedField, ...]:
    return tuple(
        ProjectedField(name=f"f{i}", shape=shape, uses=(HotUse.FILTER,)) for i in range(count)
    )


def _entity(**overrides: object) -> ProjectedEntity:
    declared: dict[str, object] = {
        "entity": "client",
        "fields": _fields(3),
        "change_signal": ChangeSignal.WEBHOOK,
        "visibility": Scope.department("maintenance"),
    }
    declared.update(overrides)
    return ProjectedEntity(**declared)  # type: ignore[arg-type]


def test_the_manifest_validator_enforces_the_twelve_field_cap() -> None:
    """Without this, a manifest declaring thirteen fields installs cleanly and is refused on
    its first ingest, in production, by a check the author never saw. The cap is what keeps
    the projection 40 MB rather than tens of gigabytes, and it has to be reviewable."""
    with pytest.raises(ManifestError, match="mirror"):
        _entity(fields=_fields(MAX_PROJECTED_FIELDS + 1))


def test_exactly_twelve_declared_fields_are_accepted_by_the_manifest_validator() -> None:
    """Off-by-one on a refusing limit is worse than one that permits: it blocks a correct
    connector and the author works around the rule instead of writing a narrower one."""
    assert len(_entity(fields=_fields(MAX_PROJECTED_FIELDS)).field_names) == MAX_PROJECTED_FIELDS


@pytest.mark.parametrize("field", ["salary", "contact_email", "staff_nric", "bank_account"])
def test_no_denylisted_field_can_pass_the_five_clause_test(field: str) -> None:
    """A field can be genuinely hot, genuinely signalled, pointer-shaped and inside the cap
    and still be one we may never store. Deleting this makes 'the fast lane needs it' an
    argument for keeping a salary, which is exactly how the denylist gets eroded."""
    verdicts = projectability(
        ProjectedField(name=field, shape=FieldShape.STATUS, uses=(HotUse.FILTER,)),
        signal=ChangeSignal.WEBHOOK,
        label_count=0,
        field_count=1,
    )
    assert [v.clause for v in failed_clauses(verdicts)] == ["permitted"]


def test_the_five_clause_test_has_five_clauses() -> None:
    """Pinned as a count because the failure mode is silent: a clause deleted during a
    refactor leaves four checks, every remaining one still passes, and the projection admits
    a class of field nobody decided to admit."""
    verdicts = projectability(
        ProjectedField(name="status", shape=FieldShape.STATUS, uses=(HotUse.FILTER,)),
        signal=ChangeSignal.WEBHOOK,
        label_count=0,
        field_count=1,
    )
    assert len(verdicts) == 5
    assert failed_clauses(verdicts) == ()


# ------------------------------------------------------------- the change signal (M11.4.7)
def test_a_source_with_no_change_signal_may_project_nothing() -> None:
    """The clause that stops the projection becoming a mirror. A field nobody can refresh is
    the one somebody most wants to copy once and be done with, and once copied it is filtered,
    sorted and counted on as current forever with nothing reporting that it stopped being
    true."""
    with pytest.raises(ManifestError, match="no change signal"):
        _entity(change_signal=ChangeSignal.NONE)


@pytest.mark.parametrize(
    "signal", [ChangeSignal.WEBHOOK, ChangeSignal.CDC, ChangeSignal.UPDATED_SINCE]
)
def test_every_real_change_signal_admits_a_projection(signal: ChangeSignal) -> None:
    """The rule must refuse only the absence of a signal. A rule that also refused CDC or a
    cursor would leave webhooks as the only projectable source, and a webhook is the one
    signal that drops deliveries silently."""
    assert _entity(change_signal=signal).change_signal.is_a_signal


# ---------------------------------------------------- the visibility predicate (M11.4.5)
def test_a_resolved_permission_list_is_never_stored_as_a_field() -> None:
    """This is where the category fails. Microsoft's own connector documentation admits
    their incremental crawls do not update permissions at all, and Glean's full crawls run
    on 28-day cycles; both are the consequence of storing the resolved list."""
    with pytest.raises(ManifestError, match="resolved permission list"):
        _entity(
            fields=(
                ProjectedField(name="id", shape=FieldShape.IDENTIFIER, uses=(HotUse.IDENTIFY,)),
                ProjectedField(
                    name="allowed_users", shape=FieldShape.STATUS, uses=(HotUse.FILTER,)
                ),
            )
        )


def test_a_resolved_permission_list_is_never_stored_as_a_predicate_either() -> None:
    """The same list wearing a predicate's clothes. Nothing in it depends on the caller, so
    it does not re-evaluate against the live entitlement set and the mover case breaks
    exactly as it would with a stored ACL, while passing a check that only looked at field
    names."""
    enumerated = Scope(clauses=(Clause(field="member_id", op=Op.IN, value=("u_a", "u_b")),))
    with pytest.raises(ManifestError, match="wearing a predicate"):
        _entity(visibility=enumerated)


def test_a_projection_must_carry_a_predicate_at_all() -> None:
    """An absent predicate is not a narrower version of the source's rules, it is the
    absence of them, and every row is then visible to anybody holding the entity's
    capability. Nothing downstream can tell that from a source with no restrictions."""
    with pytest.raises(ManifestError, match="no visibility predicate"):
        _entity(visibility=Scope.unrestricted())


# ------------------------------------------------------------- the two doors agree
@pytest.mark.parametrize(
    "names",
    [
        ("id", "client_id", "status", "updated_at", "display_name", "hosting_expiry"),
        tuple(f"f{i}" for i in range(MAX_PROJECTED_FIELDS)),
        ("id", "status"),
    ],
)
def test_anything_the_manifest_admits_also_passes_the_ingest_check(names: tuple[str, ...]) -> None:
    """Two doors with different opinions is worse than one door: the strict one is the one
    somebody eventually routes around, and it is the ingest-time one that sees real values.
    This fails the moment the manifest's cap and the ingest cap drift apart, which is what
    happens the first time somebody writes the number 12 twice."""
    entity = _entity(
        fields=tuple(
            ProjectedField(name=name, shape=FieldShape.STATUS, uses=(HotUse.FILTER,))
            for name in names
        )
    )
    assert_projectable(entity.entity, dict.fromkeys(entity.field_names, "x"))


def test_the_connectors_package_states_the_cap_once() -> None:
    """The cap and the denylist are imported from `brain.core.projection` rather than
    restated. A second copy is a second number, and the day one of them is raised the other
    is the one still being read at ingest, so the refusal moves to production."""
    sources = sorted(CONNECTORS.glob("*.py"))
    assert sources, "the connectors package has no modules to check"
    offenders = [
        str(path.name)
        for path in sources
        if "MAX_PROJECTED_FIELDS =" in path.read_text(encoding="utf-8")
        or "NEVER_PROJECT =" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"a second definition of the storage limits lives in {offenders}"
