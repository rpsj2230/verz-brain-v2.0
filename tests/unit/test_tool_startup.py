"""The registry the application builds, and the fact that anything builds one at all.

**This file exists because of an absence rather than a behaviour.** `brain.tools.registry`
carries every rule about what may be registered, and `ToolRegistry.freeze` says in its own
docstring that it is "called at startup". Nothing called it. Outside the tests, no code in
`src/` had ever constructed a `ToolRegistry`, so a careful set of rules had never refused
anything, and the first tool to break one would have broken it against a registry somebody
assembled the afternoon they needed one.

That is the same failure this repository has now found four times: a mechanism that is
correct, tested, and never invoked. The deploy that reported success and deployed nothing,
the RLS sweep that had never run, the traceability count that was stale, and this.

So the tests below are mostly about wiring: that there is one builder, that the application
calls it, that what comes back is frozen, and that a tool registered through it went through
every door on the way in.

Task ids: M12.1.5
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from brain.app import Settings, create_app
from brain.core.envelope import IdentityMode
from brain.knowledge.columns import PRICE_LIST
from brain.knowledge.rows import RowQuery
from brain.tools.registry import ToolRegistrationError, ToolRegistry
from brain.tools.startup import (
    BUILT_IN_ROW_ENTITIES,
    ROW_TOOL_DESCRIPTIONS,
    build_registry,
)


class _Rows:
    """A `RowSource` that answers nothing. What it returns is not what these tests are about;
    that it can be supplied at all is."""

    def rows(self, query: RowQuery) -> Sequence[Mapping[str, Any]]:
        del query
        return ()


# --------------------------------------------------------------- there is a builder
def test_the_registry_the_application_builds_comes_back_frozen() -> None:
    """A caller handed an unfrozen registry can register into it after the whole-registry
    checks have run, which is the same as not running them.

    Delete this and `build_registry` can return before `freeze`, and the duplicate-description
    rule stops applying to anything registered afterwards."""
    registry = build_registry(source="local", records=_Rows())

    assert registry.is_frozen is True
    with pytest.raises(ToolRegistrationError):
        registry.register(
            next(iter(registry)),
            lambda: None,
        )


def test_a_row_tool_is_registered_for_every_entity_that_has_a_classification() -> None:
    """The positive case, and the one that proves the builder builds rather than returns an
    empty registry that passes every other assertion here.

    Named against `BUILT_IN_ROW_ENTITIES` rather than against the number one, so adding a
    second classification does not require editing this test to keep it honest."""
    registry = build_registry(source="xero", records=_Rows())

    assert len(registry) == len(BUILT_IN_ROW_ENTITIES)
    assert registry.names() == ("xero.read_price_list",)
    assert PRICE_LIST in BUILT_IN_ROW_ENTITIES


def test_the_source_is_part_of_every_tools_name() -> None:
    """A row tool is pinned to one source as well as one entity, because `proj.record` is
    keyed that way and two systems' record ids collide by coincidence of integers. The name
    carries it, so a catalogue holding two sources' price lists describes two tools rather
    than one ambiguous one."""
    xero = build_registry(source="xero", records=_Rows()).names()
    freshdesk = build_registry(source="freshdesk", records=_Rows()).names()

    assert xero == ("xero.read_price_list",)
    assert freshdesk == ("freshdesk.read_price_list",)
    assert not set(xero) & set(freshdesk)


def test_a_registry_built_with_no_row_source_offers_no_row_tools() -> None:
    """**Not a degraded registry: an honest one.** A tool that is present and cannot answer
    is worse than one that is absent, because a missing tool is a gap somebody notices and an
    empty answer is a fact somebody believes. A model that picks `read_price_list` and is told
    nothing came back reports that the company has no price list.

    Delete this and the builder can be made to register tools bound to a source it invented,
    which is a second path to data with its own idea of what may be seen."""
    registry = build_registry(source="local")

    assert len(registry) == 0
    assert registry.is_frozen is True


def test_every_built_in_entity_has_a_description_written_for_it() -> None:
    """`ToolRegistry.validate` refuses two tools sharing a description, folded and stripped of
    punctuation, and that is a property of a pair rather than of one tool. Keeping the
    descriptions in one mapping makes a collision visible while it is being written instead of
    at the freeze that follows.

    Delete this and a new classification is added with no description, and the builder fails
    with a KeyError at startup rather than at the edit."""
    for classification in BUILT_IN_ROW_ENTITIES:
        assert classification.entity in ROW_TOOL_DESCRIPTIONS
        assert ROW_TOOL_DESCRIPTIONS[classification.entity].strip()


def test_a_registered_row_tool_declares_service_identity_and_carries_its_pin() -> None:
    """The rule the registry checks and the reason the scope is passed rather than omitted. A
    projected row was fetched under somebody else's credentials long before the query runs, so
    the source is enforcing nothing now and ours are the only permissions there are.
    `assert_service_tool_is_scoped` refuses that combination unscoped, and passing the tool's
    own pin is what satisfies it honestly rather than with an unrestricted scope.

    Delete this and the tool can be registered with `IdentityMode.REQUESTER`, which claims the
    source is checking permissions it is not."""
    registry = build_registry(source="local", records=_Rows())
    definition = registry.get("local.read_price_list").definition

    assert definition.identity_mode is IdentityMode.SERVICE
    assert definition.source == "local"
    assert definition.required_capability == "read:price_list"


# --------------------------------------------------------------- the application calls it
def test_the_application_builds_the_registry_during_startup() -> None:
    """**The whole point of the module.** Every rule in `brain.tools.registry` runs at
    registration and `freeze` runs the rest, and none of it ran anywhere until a process
    built one.

    Asserted through the real lifespan rather than by calling the builder again, because
    calling the builder proves the builder works and proves nothing about whether anybody
    calls it. Delete this and the wiring can be removed with every other test here green."""
    from fastapi.testclient import TestClient

    app = create_app(Settings(env="development", run_migrations=False))

    with TestClient(app):
        registry = app.state.tools

        assert isinstance(registry, ToolRegistry)
        assert registry.is_frozen is True
        assert app.state.ready["tools"] is True


def test_the_source_the_application_uses_comes_from_configuration() -> None:
    """`RowTool` refuses an empty source, so this cannot be left unset and discovered at
    startup on a fresh install. It is a real setting with a real default rather than a
    placeholder that would fail the first time somebody deployed without it."""
    assert Settings().tool_source == "local"
    assert Settings(tool_source="xero").tool_source == "xero"
