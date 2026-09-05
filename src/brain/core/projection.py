"""What may be copied into our database, and what may never be.

Three tiers, and the boundary between the second and third is the one this file enforces.

- **Local** — ours: identity, capabilities, audit, knowledge, memory, the entity registry.
- **Projected** — a pointer, never the payload. Record ids, join keys, status enums,
  timestamps, short display labels, and the source's own visibility predicate. Capped at
  twelve fields per entity type.
- **Federated** — everything else, fetched live at question time, never stored.

The cap and the denylist are not the same rule and both are needed. The cap keeps the
projection a pointer rather than a mirror; the denylist names fields that may not be
copied at any size, however useful they would be.

**A note on what this is not.** It is a restriction on *storage*, not on *access*. HR can
read a salary; the salary is fetched from the source each time and never lands here. This
is the distinction that makes the architecture's "permanent deny list" compatible with
entitlements being additive-only: nothing subtracts from what a person holds, and the
denylist subtracts from what the database may keep.

Why it is a hard constant rather than configuration: a denylist an operator can edit is a
denylist that gets edited at 2am to make a feature work, and the field is then in the
database permanently. Removing it later does not un-store it.

Task ids: M11.4.4
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Never projected, at any size, under any configuration. From the architecture's data
#: tier table, where these sit under "Federated — never stored".
#:
#: Each is here because storing it converts a permission mistake into a breach: a bug that
#: over-returns a projected field leaks whatever we kept, while the same bug over a
#: federated field leaks only what that one question fetched.
NEVER_PROJECT: frozenset[str] = frozenset(
    {
        "email",
        "phone",
        "address",
        "nric",
        "bank_details",
        "bank_account",
        "salary",
        "contract_value",
        "margin",
        "ticket_body",
        "conversation",
        "invoice_line",
        "contract",
        "crm_note",
        "attachment",
    }
)

#: Suffixes and substrings that make a field a member of the above by shape rather than by
#: name. `employee_salary`, `salary_band` and `annual_salary` are all salary; listing every
#: spelling a connector might use is a losing game.
NEVER_PROJECT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(^|_)salary(_|$)"),
    re.compile(r"(^|_)nric(_|$)"),
    re.compile(r"(^|_)(email|phone|mobile|address)(_|$)"),
    re.compile(r"(^|_)bank(_|$)"),
    re.compile(r"(^|_)(iban|swift|account_number)(_|$)"),
    re.compile(r"(^|_)(passport|nin|ssn)(_|$)"),
)

#: Twelve, from the architecture. Not a round number chosen for looks: it is what fits the
#: purpose of a pointer (ids, join keys, a handful of enums and timestamps, one label)
#: without becoming a mirror. The projection is about 40 MB at Verz's scale; a mirror of
#: the same data is tens of gigabytes.
MAX_PROJECTED_FIELDS = 12

#: A label is for showing a person which record this is, not for holding content. Anything
#: longer has stopped being a label and started being a payload.
MAX_LABEL_CHARS = 120


class ProjectionRefusedError(Exception):
    """Raised when a projection would store something it must not.

    Deliberately not part of the user-facing error taxonomy. Nobody asking a question ever
    sees this; it is a build-time and ingest-time failure, and it should stop a connector
    from being written rather than degrade an answer.
    """


@dataclass(frozen=True)
class ProjectionViolation:
    field: str
    reason: str

    def __str__(self) -> str:
        return f"{self.field}: {self.reason}"


def is_forbidden(field: str) -> bool:
    """True when this field may never be stored locally."""
    name = field.strip().lower()
    if name in NEVER_PROJECT:
        return True
    return any(p.search(name) for p in NEVER_PROJECT_PATTERNS)


def check_projection(entity: str, fields: dict[str, object]) -> list[ProjectionViolation]:
    """Every reason this projection is not allowed, not just the first.

    Returning one at a time turns writing a connector into a guessing game, where each
    fix reveals the next objection.
    """
    violations: list[ProjectionViolation] = []

    for name, value in fields.items():
        if is_forbidden(name):
            violations.append(
                ProjectionViolation(
                    field=name,
                    reason="on the permanent denylist; fetch it live, never store it",
                )
            )
            continue
        if isinstance(value, str) and len(value) > MAX_LABEL_CHARS:
            violations.append(
                ProjectionViolation(
                    field=name,
                    reason=(
                        f"{len(value)} characters, over the {MAX_LABEL_CHARS} limit; "
                        "a label identifies a record, anything longer is a payload"
                    ),
                )
            )

    if len(fields) > MAX_PROJECTED_FIELDS:
        violations.append(
            ProjectionViolation(
                field=f"<{entity}>",
                reason=(
                    f"{len(fields)} fields, over the {MAX_PROJECTED_FIELDS} limit; "
                    "the projection is a pointer, and past this it is a mirror"
                ),
            )
        )

    return violations


def assert_projectable(entity: str, fields: dict[str, object]) -> None:
    """Raise with every violation at once. Called at the projection boundary."""
    violations = check_projection(entity, fields)
    if not violations:
        return
    listed = "\n".join(f"  - {v}" for v in violations)
    msg = f"{entity} cannot be projected:\n{listed}"
    raise ProjectionRefusedError(msg)
