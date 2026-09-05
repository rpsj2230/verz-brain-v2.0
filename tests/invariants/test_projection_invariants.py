"""What the database may keep. A failure here blocks deploy.

These are separate from the entitlement canaries and the distinction matters: those assert
nobody *sees* what they should not, these assert we never *store* what we should not. A
system can be perfect at the first and still be one query bug away from a breach if it
kept the data.

Task ids: M11.4.4
"""

from __future__ import annotations

import pytest

from brain.core.projection import (
    MAX_LABEL_CHARS,
    MAX_PROJECTED_FIELDS,
    NEVER_PROJECT,
    ProjectionRefusedError,
    assert_projectable,
    check_projection,
    is_forbidden,
)
from tests.fixtures.company import CANARIES

pytestmark = pytest.mark.invariant


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
