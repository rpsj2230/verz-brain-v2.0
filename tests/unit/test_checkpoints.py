"""Where a graph's saved state is allowed to go, and the connection it may not use.

Every test here is about a checkpointer that would be constructed, would appear to work, and
would either put payload rows somewhere no sweep enumerates or fail only in production.

No task ids. `brain.ops.checkpoints` claims none: langgraph is not a dependency and nothing
builds a graph, so M32.4.1.2 is served rather than closed. These tests exist because the
decisions are real even where the saver is not, and because the worker refuses a bad one
today.
"""

from __future__ import annotations

import pytest

from brain.db import SCHEMAS
from brain.ops.checkpoints import (
    CHECKPOINT_SCHEMA,
    CheckpointerConfig,
    CheckpointerError,
    connection_refusals,
    search_path_option,
)
from brain.ops.queue import pooler_url_findings

APP_URL = "postgresql+psycopg://brain:pw@pgbouncer:5432/brain"
DIRECT_URL = "postgresql+psycopg://brain:pw@db:5432/brain"


# --------------------------------------------------- the connection
def test_a_checkpointer_handed_the_applications_own_connection_string_is_refused() -> None:
    """The application connects through the transaction pooler, and the saver prepares its
    statements server-side. A pooler in transaction mode hands the next statement to a
    backend that never saw the prepare, which fails in production and nowhere else because a
    development machine has no pooler. Delete this and the fourth instance of the pooler bug
    in this repository ships."""
    refusals = connection_refusals(APP_URL, app_url=APP_URL)

    assert any("application's own connection string" in r for r in refusals)


def test_a_checkpointer_url_naming_the_pooler_is_refused_on_its_own() -> None:
    """The same fault reached differently: a checkpointer with its own variable, set to the
    pooler because that is what the other variable said. Delete this and only the exact-match
    case is caught, and the exact match is the one somebody fixes first."""
    assert any("transaction pooler" in r for r in connection_refusals(APP_URL))


def test_a_direct_checkpointer_url_is_accepted_without_comment() -> None:
    """A check that refuses everything is a check that gets disabled. Delete this and
    `connection_refusals` could return a finding unconditionally and every refusal test above
    would still pass while no worker could ever start."""
    assert connection_refusals(DIRECT_URL, app_url=APP_URL) == ()


def test_the_pooler_detection_is_the_queues_and_not_a_second_copy() -> None:
    """Asserted structurally rather than by both happening to be right today. The house rule
    is that the copy which drifts is the one nobody is looking at, and every failure in this
    family is silent, so the drift is found by the thing it was meant to prevent.

    Delete this and `brain.ops.checkpoints` can grow its own list of pooler hostnames, which
    reads as tidier and is the fourth copy of a rule this repository has already been bitten
    by three times."""
    assert connection_refusals(APP_URL) == pooler_url_findings(APP_URL)
    assert connection_refusals(f"{DIRECT_URL}?prepare_threshold=0") == pooler_url_findings(
        f"{DIRECT_URL}?prepare_threshold=0"
    )


# --------------------------------------------------- where the tables land
def test_the_checkpoint_schema_is_one_the_row_level_security_sweep_enumerates() -> None:
    """The whole of what the schema choice buys. `brain.ops.sweeps.sweep_rls` reads
    `brain.db.SCHEMAS` and looks nowhere else, and the saver creates its own tables with no
    row-level security on them. In a named schema that is a red sweep somebody acts on; in
    `public` it is nothing at all.

    Delete this and `CHECKPOINT_SCHEMA` can be set to a schema that reads sensibly and is not
    enumerated, which is the same outcome as leaving it at the default."""
    assert CHECKPOINT_SCHEMA in SCHEMAS


def test_the_checkpoint_schema_is_not_one_that_promised_to_hold_no_payloads() -> None:
    """`obs` holds metadata and no payloads, and its retention argument was made on the
    strength of that; `mem` holds what the system learnt rather than what happened; `chat` is
    a transcript. A checkpoint is the run so far, so filing it in any of the three makes one
    of those sentences quietly stop being true in a schema whose rules were argued elsewhere.

    Delete this and `CHECKPOINT_SCHEMA` can be moved to `obs`, which reads as the
    observability schema, is enumerated by the sweep, and passes every other test here."""
    assert CHECKPOINT_SCHEMA not in {"obs", "mem", "chat"}
    assert CHECKPOINT_SCHEMA == "agent"


def test_a_checkpointer_configured_into_a_schema_nobody_declared_is_refused() -> None:
    """`public` is the default the library would use and it is the one answer this repository
    has already ruled out: `brain.db` calls a table there one nobody decided the
    classification of. Delete this and the refusal becomes a comment."""
    with pytest.raises(CheckpointerError, match="row-level security"):
        CheckpointerConfig(url=DIRECT_URL, schema="public")


def test_the_search_path_names_the_schema_and_leaves_public_off_it() -> None:
    """A path of `agent,public` creates new tables in `agent` and finds existing ones in
    `public`, so an install that has already run once with the default keeps reading the
    tables nothing enumerates and the fix silently does nothing.

    Delete this and appending `public` looks like a safe compatibility measure, which is
    exactly the edit that makes the schema move a no-op."""
    option = search_path_option()

    assert option == f"-c search_path={CHECKPOINT_SCHEMA}"
    assert "public" not in option


# --------------------------------------------------- the two unsafe settings
def test_a_checkpointer_that_prepares_statements_server_side_is_refused() -> None:
    """`brain.session` sets `prepare_threshold=None` on the application engine for this exact
    reason, and a checkpointer that quietly did not would be the same bug in a second place.
    Delete this and the field becomes documentation."""
    with pytest.raises(CheckpointerError, match="prepare"):
        CheckpointerConfig(url=DIRECT_URL, prepare_threshold=5)


def test_a_checkpointer_in_pipeline_mode_is_refused() -> None:
    """Separate from the prepare threshold on purpose: they are two assumptions about the
    backend staying the same and a single "safe mode" flag would let one be fixed while the
    other stayed wrong. Delete this and pipeline mode arrives as a performance improvement
    with no error to connect it to the resume that stops working."""
    with pytest.raises(CheckpointerError, match="pipeline"):
        CheckpointerConfig(url=DIRECT_URL, pipeline=True)


def test_a_checkpointer_with_no_connection_string_is_refused() -> None:
    """An empty string is a valid value for a string field, which is how `DATABASE_URL`
    unset once produced an application that skipped its migrations and reported healthy.
    Delete this and a saver with nowhere to save is constructible."""
    with pytest.raises(CheckpointerError, match="nowhere to save"):
        CheckpointerConfig(url="   ")


def test_a_safe_configuration_is_constructible_and_says_where_its_tables_go() -> None:
    """The positive sibling of the three refusals above. A guard tested only by what it
    refuses is satisfied by a constructor that refuses everything, and that constructor would
    make the checkpointer unconfigurable while every refusal test stayed green.

    Delete this and the defaults can drift to values nothing can be built with."""
    config = CheckpointerConfig(url=DIRECT_URL)

    assert config.schema == CHECKPOINT_SCHEMA
    assert config.prepare_threshold is None
    assert config.pipeline is False
    assert config.connect_options == f"-c search_path={CHECKPOINT_SCHEMA}"
