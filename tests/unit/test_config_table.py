"""`ops.setting`: configuration as data, and the reach it must never have.

M31.3.1.4 asks for configuration as data in Postgres so that tuning is not a deploy. The
shape of the table - that the migration builds what the model declares, that it carries a
policy and a grant, that it reverses - is checked with every other table in
`tests/unit/test_tables.py`. What is here is the argument the table exists to make, and it
has two halves.

The first is that a value has a declared type. A settings table whose values are bare
strings is a table where the same row is an int in one caller, a string in another and
`"true"` in the one that matters, and none of them is wrong about the column.

The second matters more. A settings table is by construction the easiest thing in the
system to change: no review, no deploy, no diff. That is the point of it, and it is exactly
why a grant must never be reachable from it - a permission with an edit button beside it is
a permission model with a bypass. The tests below are the enforcement of "nothing an
operator can tune may be a permission", one per mechanism, so that removing any one of them
is a visible decision rather than a diff nobody read.

None of this needs a database. Every assertion is a property of the metadata or of the DDL
the migration renders, and what only CI can check is stated in
`test_tables.py`'s own docstring: that PostgreSQL 18 accepts these constraints and that they
refuse at run time what they are written to refuse.

Task ids: M31.3.1.4
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, String, Table

from brain.core.entitlement import CAPABILITY_RE, VERBS
from brain.db import metadata
from brain.tables import config as config_table

SETTING = "ops.setting"


def table(qualified: str = SETTING) -> Table:
    return metadata.tables[qualified]


def checks(qualified: str = SETTING) -> dict[str, str]:
    return {
        str(c.name): str(c.sqltext)
        for c in table(qualified).constraints
        if isinstance(c, CheckConstraint)
    }


def indexes(qualified: str = SETTING) -> dict[str, Index]:
    return {str(ix.name): ix for ix in table(qualified).indexes}


# ------------------------------------------------------ a knob, not a permission
#: Every word the permission model is written in. A column named after one of these is the
#: change this file exists to refuse, and the list is deliberately wider than the four the
#: brief names, because the failure is somebody adding the *next* one.
PERMISSION_SHAPED = (
    "capability",
    "scope",
    "grant",
    "granted_by",
    "leash",
    "rung",
    "tier",
    "entitlement",
    "principal_id",
    "pack",
    "policy",
    "ent_hash",
    "assurance",
)


@pytest.mark.parametrize("word", PERMISSION_SHAPED)
def test_a_setting_has_no_column_named_after_a_permission(word: str) -> None:
    """The first and bluntest of the four refusals. A `scope` column on a table an operator
    edits without a deploy is a scope an operator can widen without a deploy, and the reason
    a grant is hard to make - the reason, the expiry, the ledger entry the trigger writes -
    would all sit one table away from something with none of them.

    Matched on substring rather than on the exact name, so `default_scope` and `scope_json`
    are caught too. A false positive here costs a rename; a false negative costs the
    property."""
    names = tuple(table().columns.keys())
    for name in names:
        assert word not in name, f"ops.setting carries a column named {name!r}"


def test_a_setting_points_at_nothing_at_all() -> None:
    """No foreign key, and in particular none to `auth.principal`. `updated_by` is a plain
    column for the reason `capability_grant.granted_by` is one - a setting can be changed by
    a provisioning job or by somebody at a psql prompt, and the record has to outlive their
    account - but the stronger property is that this table is not joinable to the permission
    graph in either direction."""
    for constraint in table().constraints:
        assert not isinstance(constraint, ForeignKeyConstraint), constraint


def test_a_setting_key_may_not_sit_in_a_namespace_the_permission_model_owns() -> None:
    """The refusal that actually gets in the way, and the one worth having. An operator
    reaching for a permission knob reaches for its name first: `leash.default_rung`,
    `gate.default_scope`, `grant.max_duration_days`. Refusing at the name is refusing before
    the value exists, and before anybody has written the reader that would honour it."""
    sql = checks()["ck_setting_key_is_not_a_permission"]
    pattern = sql.split("!~ '", 1)[1].rstrip("'")
    for refused in (
        "leash.default_rung",
        "gate.default_scope",
        "grant.max_duration_days",
        "capability.implied",
        "principal.default_employment",
        "scope.fallback",
    ):
        assert re.match(pattern, refused), f"{refused} is not refused"
    for allowed in ("routing.timeout_seconds", "answer.cache_ttl_seconds", "ingest.batch_size"):
        assert not re.match(pattern, allowed), f"{allowed} is refused and should not be"


def test_every_reserved_prefix_is_actually_in_the_constraint() -> None:
    """The tuple and the SQL are two copies of one list, and the SQL is the one that runs.
    Generated from the tuple in `brain.tables.config` for the reason `one_of` is generated
    from an enum; this is what says the generation happened."""
    for prefix in config_table.RESERVED_KEY_PREFIXES:
        assert f"{prefix}|" in config_table.KEY_IS_NOT_A_PERMISSION or (
            f"|{prefix})" in config_table.KEY_IS_NOT_A_PERMISSION
        )


def test_a_setting_value_may_not_be_a_capability_string() -> None:
    """A capability sitting in a value column is a capability written somewhere nothing
    treats as one, which is how it gets read as one later - by whoever adds the first reader
    that resolves a setting into a permission check. The constraint is the string case,
    which is the only one checkable without iterating."""
    sql = checks()["ck_setting_value_is_not_a_capability"]
    assert CAPABILITY_RE.pattern in sql
    assert "value_type = 'string'" in sql


def test_a_setting_value_may_not_be_a_scope_document() -> None:
    """`brain.tables.gate` explains at length that a scope read as the wrong shape is
    unrestricted rather than empty, so a stored `{"clauses": [...]}` is the single most
    dangerous json object in this system to leave lying in a column nothing validates.

    `IS DISTINCT FROM` rather than a plain comparison, and that is not style: `value ->
    'clauses'` is null for every ordinary value, `null = 'array'` is null, and a check
    constraint evaluating to null passes. The plain spelling would refuse nothing at all."""
    sql = checks()["ck_setting_value_is_not_a_scope"]
    assert "jsonb_typeof(value -> 'clauses')" in sql
    assert "IS DISTINCT FROM" in sql


def test_no_capability_can_be_a_setting_key_and_no_key_can_be_a_capability() -> None:
    """The two grammars cannot overlap, which is what makes the previous three checks a
    boundary rather than a filter. A capability carries a colon and the key grammar has no
    colon in its character class; a key is dotted and `CAPABILITY_RE` requires the colon. It
    is asserted in both directions rather than read off the patterns, because "these two
    regexes cannot both match" is exactly the kind of reading that is right until one of
    them is edited."""
    key_re = re.compile(config_table.SETTING_KEY_PATTERN)
    for capability in (f"{verb}:client.name" for verb in sorted(VERBS)):
        assert CAPABILITY_RE.match(capability)
        assert not key_re.match(capability), f"{capability} would pass as a setting key"
    for key in ("routing.timeout_seconds", "answer.cache_ttl_seconds", "a.b.c"):
        assert key_re.match(key)
        assert not CAPABILITY_RE.match(key), f"{key} would pass as a capability"


# ------------------------------------------------------- a type and a validated value
def test_a_setting_declares_the_type_of_its_value() -> None:
    """M31.3.1.4's other half. Without the type column the value is a bare json blob and
    every reader decides for itself what it is, which is how one caller's integer becomes
    another's string and the disagreement shows up as a cast error in whichever ran first."""
    columns = table().columns
    assert not columns["value_type"].nullable
    assert not columns["value"].nullable
    assert columns["value"].server_default is None, (
        "a defaulted value is a row that exists and configures nothing, "
        "which reads in the console as having been set"
    )


def test_the_value_types_are_a_closed_set_written_from_the_enum() -> None:
    """A hand-typed list is a second copy of the enum and stops matching it the first time
    somebody adds a member - and the failure is a row the database refuses in production
    after passing every test that only exercised the Python side."""
    sql = checks()["ck_setting_value_type"]
    assert sorted(re.findall(r"'([a-z_]+)'", sql)) == sorted(
        t.value for t in config_table.SettingType
    )


def test_the_declared_type_and_the_stored_value_have_to_agree() -> None:
    """A type nothing enforces is a label. Each branch pins the value to what
    `jsonb_typeof` reports, and the integer branch carries a digit check as well, because
    jsonb has one number type: without it a `max_attempts` of `2.5` stores happily and fails
    at the cast, in whichever caller reads it first."""
    sql = checks()["ck_setting_value_matches_its_type"]
    for member in config_table.SettingType:
        assert f"WHEN '{member.value}' THEN" in sql, f"{member.value} is not pinned to a shape"
    assert "'^-?[0-9]+$'" in sql, "an integer setting could hold 2.5"


def test_an_unlisted_value_type_is_refused_rather_than_unchecked() -> None:
    """`ELSE false`, and the difference is the whole value of the constraint. A CASE with no
    ELSE yields null for a type it does not list, a check constraint passes on null, and a
    sixth member added to `SettingType` and updated in the vocabulary constraint but not
    here would accept any value at all. Refusing the forgotten member is the failure that
    gets noticed and fixed."""
    assert checks()["ck_setting_value_matches_its_type"].rstrip().endswith("ELSE false END")


# ------------------------------------------------------------------ the row itself
def test_a_setting_says_what_it_does_and_who_last_changed_it() -> None:
    """Once tuning is not a deploy, the deploy log stops being the record of who changed
    what. `updated_by` is what replaces it, and `description` is what makes the knob safe to
    turn - an undescribed setting is one nobody dares change, so the deploy this table was
    meant to remove happens anyway."""
    columns = table().columns
    assert not columns["description"].nullable
    assert not columns["updated_by"].nullable
    assert checks()["ck_setting_described"] == "length(btrim(description)) > 0"
    assert checks()["ck_setting_updated_by_present"] == "length(btrim(updated_by)) > 0"


def test_a_setting_key_is_namespaced() -> None:
    """A bare `timeout` is a setting nobody can tell the owner of, and the first collision
    between two subsystems that both wanted the word is silent: whichever row exists wins,
    and the other subsystem is configured by accident."""
    key_re = re.compile(config_table.SETTING_KEY_PATTERN)
    assert not key_re.match("timeout")
    assert key_re.match("routing.timeout_seconds")
    assert checks()["ck_setting_key_grammar"] == f"key ~ '{config_table.SETTING_KEY_PATTERN}'"


def test_one_live_row_per_key_and_a_retired_key_may_return() -> None:
    """Every reader looks a setting up by key. Two live rows for one key make the effective
    configuration depend on which came back first, which is an incident that reads as
    intermittent rather than as a duplicate row. Partial rather than total, so a setting
    retired last quarter can be set again under its own name."""
    index = indexes()["uq_setting_key_live"]
    assert index.unique
    assert [c.name for c in index.columns] == ["key"]
    assert "deleted_at IS NULL" in str(index.dialect_options["postgresql"]["where"])


def test_retiring_a_setting_is_what_returns_the_system_to_its_default() -> None:
    """There is no `default_value` column, and its absence is the design: the default lives
    in the code that reads the setting, a row here is an override, and `deleted_at` is how
    the override goes away.

    That is the opposite of the rule on `gate.grants_version`, where a hidden row silently
    resetting a counter to its default is the exact failure the counter exists to prevent.
    The two are worth stating together, because "soft delete everywhere" and "soft delete
    nowhere" are both wrong and the difference is what the default means."""
    columns = table().columns
    assert "deleted_at" in columns
    assert "default_value" not in columns
    assert "deleted_at" not in metadata.tables["gate.grants_version"].columns


def test_a_setting_carries_no_environment_column() -> None:
    """Staging has its own database - `docker-compose.staging.yml` shares no volume with
    production - so an environment column would be a second, editable answer to a question
    the connection string already settles, and a production row sitting in the staging table
    would be a trap nobody was looking for."""
    columns = set(table().columns.keys())
    for name in ("env", "environment", "profile", "stage"):
        assert name not in columns


def test_the_key_column_is_wide_enough_to_be_a_name_and_not_a_sentence() -> None:
    """A key is looked up by an exact match and printed in a console list. The width is the
    same one `gate.field_policy.field` uses, for the same reason."""
    kind = table().columns["key"].type
    assert isinstance(kind, String)
    assert kind.length == config_table.SETTING_KEY_CHARS


def test_the_settings_table_lives_in_ops_and_not_in_gate() -> None:
    """`brain.db.SCHEMAS` calls `ops` "scheduled jobs, budgets, deployment records", and a
    tuned value is the part of a deployment that stopped needing a deploy. The schema holding
    grants is the last place to put a table whose whole purpose is that anybody with the
    console can change it, and the distance is enforcement rather than tidiness: row-level
    security, the grant sweeps and `sweep_grant_isolation` are all written against schema
    names."""
    assert table().schema == "ops"


def test_the_ops_schema_is_now_swept_for_row_level_security() -> None:
    """This table is in `ops`, and until the sweep's list grew a table in `ops` could ship
    with row-level security off and nothing in CI would say so - which is the state the three
    routing tables shipped in and recorded on themselves. The DDL check lives in
    `test_tables.py`; this is the sweep that would catch a table added later by somebody who
    did not read either file."""
    from brain.ops import sweeps

    source = sweeps.sweep_rls.__code__.co_consts
    query = next(c for c in source if isinstance(c, str) and "nspname" in c)
    for schema in ("auth", "gate", "obs", "proj", "know", "agent", "mem", "er", "ops"):
        assert f"'{schema}'" in query, f"sweep_rls does not cover {schema}"
