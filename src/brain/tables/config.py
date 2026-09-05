"""Configuration as data: the knobs an operator turns without a deploy.

M31.3.1.4 asks for "configuration as data in Postgres rather than code, so tuning is not a
deploy". `brain.config` is the other half of that sentence and stays exactly where it is:
what must be *present* per environment - a database URL, a Valkey URL - is a startup
question asked before the server binds a port, and a row cannot answer a question asked
before the connection exists. What lives here is the rest, which is most of it: the numbers
and flags somebody changes at four in the afternoon because a provider got slower, and which
today would each be a pull request, a build and a restart.

**Nothing an operator can tune may be a permission, and this module is shaped around that
one rule.** A settings table is by construction the easiest thing in the system to change:
no review, no deploy, no diff. That is the point of it and it is also the reason a grant
must never be reachable from here. If a row in this table could widen somebody's reach, then
the whole permission model - the reasons, the expiry, the ledger entry the grant triggers
write - would have a bypass sitting next to it with an edit button.

Four things enforce it, and the fourth is the one that actually gets in the way:

1. **No column for one.** A setting has a key, a type, a value, a description and whoever
   last changed it. There is no capability, no scope, no principal, no grant and no leash
   rung, and adding one is the change this docstring exists to refuse.
   `tests/unit/test_config_table.py` fails on a column named after any of them.
2. **A string value may not be a capability string.** `read:client.margin` sitting in a
   value column is a capability written somewhere nothing treats as a capability, which is
   how one gets read as one later.
3. **A value may not be a `Scope.model_dump()`.** The same argument one layer along:
   `brain.tables.gate` explains at length that a scope document read as the wrong shape is
   unrestricted rather than empty, and a scope nobody meant to store is the shape most
   likely to be read by whoever adds the first scope-aware setting.
4. **A key may not sit in a namespace the permission model owns.** `RESERVED_KEY_PREFIXES`
   refuses `leash.default_rung` and `gate.default_scope` outright. This is the one that
   matters in practice, because an operator reaching for a permission knob reaches for its
   name first, and a refusal at the name is a refusal before the value exists.

**And what is not enforced, said plainly.** Nothing here stops a number in this table being
read by code that then decides something about reach - a timeout is harmless, a
`max_rows_returned` is not obviously so. A table cannot check its readers. The rule is that
no reader may consult this table for an entitlement, and it is written here rather than only
in a review comment because this file is the one somebody adding a column has open.

**A row carries a type and a validated value.** `value` is jsonb and `value_type` says which
of five shapes it holds, with a check constraint pinning the two together. The alternative -
a text column every reader parses for itself - is how the same setting becomes an int in one
caller, a string in another and `"true"` in the one that matters. Reading is still a cast,
but it is a cast against a declared type rather than a guess.

**Absence means the compiled default.** There is no `default_value` column: the default is
in the code that reads the setting, a row here is an override, and retiring the row returns
the system to the default. That is the whole reason `SoftDeleteMixin` is right here and
wrong on `gate.grants_version`, where a hidden row silently resetting a counter to its
default is the failure the counter exists to prevent.

**No `env` column.** Staging has its own database - `docker-compose.staging.yml` shares no
volume with production, and `tests/unit/test_sweeps.py` is what keeps it that way - so an
environment column would be a second, editable answer to a question the connection string
already settles, and a production row sitting in the staging table would be a trap nobody
was looking for.

**No secrets.** A credential in a table the application role can read, with no encryption and
no envelope, is a credential in the application's ordinary query path. Secrets stay in the
environment. A check constraint refusing keys that *look* like secrets was written and
dropped: it catches `smtp_password` and misses `smtp_pw`, so it is a constraint that is
right for a reason it cannot state, and its real effect is the belief that the table is safe.

**What is missing, and it is not small.** This table records who changed a setting last and
when, and nothing else. There is no history, so "what was this a week ago" has no answer, and
the deploy log that used to answer it is exactly what M31.3.1.4 removes. The obvious fix is
an audit entry from a trigger, the way `0003` writes one for every grant - and it cannot be
written here, because `brain.audit.ledger.SUBJECT_KINDS` has no kind for a setting and
`AuditAction` has no member for tuning one. Both vocabularies are closed on purpose, and
`brain.tables.audit` is explicit that widening one to make a trigger read better is how a
closed set stops being closed. Widening them is a decision for whoever owns the ledger, with
its own migration; until then the gap is recorded rather than papered over.

Task ids: M31.3.1.4
"""

from __future__ import annotations

import enum
import uuid
from typing import Any

from sqlalchemy import CheckConstraint, Index, String, Text, Uuid, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from brain.db import Base, SoftDeleteMixin, TimestampMixin
from brain.tables.gate import CAPABILITY_PATTERN
from brain.tables.identity import PRINCIPAL_ID_CHARS, one_of

#: Long enough for a deep namespace and short enough that a key is a name rather than a
#: sentence. `gate.field_policy.field` is 120 for the same reason.
SETTING_KEY_CHARS = 120

#: `<namespace>.<name>`, at least two segments, lowercase. The dot is required: a bare
#: `timeout` is a setting nobody can tell the owner of, and the first collision between two
#: subsystems that both wanted the word is silent, because whichever row exists wins.
#:
#: It cannot collide with a capability in either direction, and that is load-bearing rather
#: than incidental. A capability is `verb:noun`, and the colon is not in this character
#: class; a key is dotted, and `CAPABILITY_RE` requires the colon. So no key parses as a
#: capability and no capability parses as a key, which `tests/unit/test_config_table.py`
#: asserts in both directions rather than trusting the reading.
SETTING_KEY_PATTERN = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$"

#: Namespaces the permission model owns. A setting key may not begin with one of these, so
#: `leash.default_rung`, `gate.default_scope` and `grant.max_duration_days` are refused by
#: the column rather than by whoever happens to review the row.
#:
#: `auth` and `gate` are two of `brain.db.SCHEMAS`; the rest are the nouns the permission
#: model is written in - the words on `brain.core.entitlement`, `brain.gate.leash` and the
#: grant tables. The list is deliberately a little wider than it strictly needs to be,
#: because the cost of a false positive is renaming a harmless key and the cost of a false
#: negative is a permission with an edit button.
RESERVED_KEY_PREFIXES: tuple[str, ...] = (
    "auth",
    "capability",
    "entitlement",
    "gate",
    "grant",
    "leash",
    "pack",
    "policy",
    "principal",
    "scope",
)

#: The refusal above, as SQL. Built from the tuple for the reason `one_of` is built from an
#: enum: a hand-written second copy stops matching the first time somebody adds a word.
KEY_IS_NOT_A_PERMISSION = "key !~ '^({})\\.'".format("|".join(RESERVED_KEY_PREFIXES))

#: A capability string may not be stored as a setting value. Only the scalar-string case is
#: checkable without iterating, which is why the reserved-prefix rule above carries the
#: weight: this one catches the value and that one catches the intent.
VALUE_IS_NOT_A_CAPABILITY = (
    f"NOT (value_type = 'string' AND (value #>> '{{}}') ~ '{CAPABILITY_PATTERN}')"
)

#: A `Scope.model_dump()` is `{"clauses": [...]}`, and `brain.tables.gate` explains what
#: happens when one is read as the other shape. `value -> 'clauses'` is null for anything
#: that is not an object carrying that key, so `IS DISTINCT FROM` rather than `<>`: the
#: plain comparison yields null for every ordinary value, and a check constraint that
#: evaluates to null passes by accident rather than on purpose.
VALUE_IS_NOT_A_SCOPE = "jsonb_typeof(value -> 'clauses') IS DISTINCT FROM 'array'"


class SettingType(enum.StrEnum):
    """What shape a setting's value holds. Closed, and every member is a json type.

    Five members, mapped onto what jsonb can tell apart. `INTEGER` and `NUMBER` are both
    json numbers to `jsonb_typeof`, so the integer case carries a digit check as well -
    without it a `max_attempts` of `2.5` would be stored happily and fail at the cast, in
    whichever caller read it first.

    There is deliberately no `SECRET` member and no `DURATION` one. The first is refused by
    the module docstring's argument about credentials; the second confuses a unit with a
    type, and a `timeout_seconds` key already says its unit in the only place a reader looks.
    """

    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    JSON = "json"


#: `value_type` and `value` have to agree, or the type is a label rather than a contract.
#:
#: `ELSE false` rather than an open CASE, and the difference is the whole value of the
#: constraint. A CASE with no ELSE yields null for an unlisted type and a check constraint
#: passes on null, so adding a member to `SettingType` and updating only the vocabulary
#: constraint would leave the new type accepting anything at all. With `ELSE false` the
#: forgotten member is refused instead, which is the failure that gets fixed.
VALUE_MATCHES_ITS_TYPE = (
    "CASE value_type"
    " WHEN 'string' THEN jsonb_typeof(value) = 'string'"
    " WHEN 'integer' THEN jsonb_typeof(value) = 'number'"
    " AND (value #>> '{}') ~ '^-?[0-9]+$'"
    " WHEN 'number' THEN jsonb_typeof(value) = 'number'"
    " WHEN 'boolean' THEN jsonb_typeof(value) = 'boolean'"
    " WHEN 'json' THEN jsonb_typeof(value) IN ('object', 'array')"
    " ELSE false END"
)


def _present(column: str) -> str:
    """Length only, never content. `brain.tables.gate._reason_present` makes the argument."""
    return f"length(btrim({column})) > 0"


class SettingRow(TimestampMixin, SoftDeleteMixin, Base):
    """`ops.setting`. One tunable value, edited without a deploy (M31.3.1.4).

    In `ops` because `brain.db.SCHEMAS` calls it "scheduled jobs, budgets, deployment
    records" and a tuned value is a deployment record - it is the part of a deployment that
    stopped needing a deploy. Not in `gate`, and the distance is the point rather than
    tidiness: the schema holding grants is the last place to put a table whose whole purpose
    is that anybody with the console can change it.

    Note what putting it in `ops` used to cost, and no longer does.
    `brain.ops.sweeps.sweep_rls` checked eight of the nine schemas and not this one, so a
    table here could ship with row-level security off and nothing in CI would say so.
    `0003` recorded that gap on three routing tables; the sweep's list now includes `ops`,
    which is what makes the policy below checked rather than merely written.

    **The key is the identity and the uuid is not.** `id` exists because every other table
    here has one and because a row has to be addressable while its key is being corrected,
    but every reader looks a setting up by `key`, and the unique index on live rows is what
    makes that lookup single-valued. Two live rows for one key would make the effective
    configuration depend on which came back first, which is a class of incident that reads
    as intermittent rather than as a duplicate row.

    **`updated_by` is a plain column and not a foreign key to `auth.principal`**, for the
    reason `CapabilityGrantRow.granted_by` gives: a setting can be changed by something that
    is not a principal row - a provisioning job, a migration, an operator at a psql prompt -
    and the record of who changed it has to outlive their account.
    """

    __tablename__ = "setting"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )

    #: `<namespace>.<name>`. See `SETTING_KEY_PATTERN` on why the namespace is mandatory and
    #: `RESERVED_KEY_PREFIXES` on which namespaces are refused.
    key: Mapped[str] = mapped_column(String(SETTING_KEY_CHARS), nullable=False)

    value_type: Mapped[str] = mapped_column(String(16), nullable=False)

    #: The value, in the shape `value_type` declares. No server default: a setting with no
    #: value is a row that exists and configures nothing, which reads in the console as
    #: having been set.
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)

    #: What turning this knob does, and which way. Required for the same reason a grant's
    #: reason is: an undescribed setting is one nobody can safely change, so it never gets
    #: changed, and the deploy this table was meant to remove happens anyway.
    description: Mapped[str] = mapped_column(Text, nullable=False)

    #: Who changed it last. Once tuning is not a deploy, the deploy log stops being the
    #: record of who changed what, and this column is what replaces it - partially, per the
    #: module docstring's note about there being no history.
    updated_by: Mapped[str] = mapped_column(String(PRINCIPAL_ID_CHARS), nullable=False)

    __table_args__ = (
        CheckConstraint(f"key ~ '{SETTING_KEY_PATTERN}'", name="key_grammar"),
        CheckConstraint(KEY_IS_NOT_A_PERMISSION, name="key_is_not_a_permission"),
        CheckConstraint(one_of("value_type", SettingType), name="value_type"),
        CheckConstraint(VALUE_MATCHES_ITS_TYPE, name="value_matches_its_type"),
        CheckConstraint(VALUE_IS_NOT_A_CAPABILITY, name="value_is_not_a_capability"),
        CheckConstraint(VALUE_IS_NOT_A_SCOPE, name="value_is_not_a_scope"),
        CheckConstraint(_present("description"), name="described"),
        CheckConstraint(_present("updated_by"), name="updated_by_present"),
        # Live rows only, as everywhere else here: a setting that was retired and is wanted
        # again should be writable under its own name, and a total constraint would make
        # retiring one a one-way door on the name.
        Index(
            "uq_setting_key_live",
            "key",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        {"schema": "ops"},
    )


#: Every table this module declares, in the order a migration must create them.
#: `migrations/versions/0004_capability_registry_and_config.py` keeps the same order and its
#: downgrade reverses it, and `brain.tables.TABLES_IN_DEPENDENCY_ORDER` ends with it.
CONFIG_TABLES_IN_DEPENDENCY_ORDER: tuple[str, ...] = ("ops.setting",)
