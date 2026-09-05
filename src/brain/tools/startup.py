"""The one place a `ToolRegistry` is built, and the reason it did not exist until now.

`brain.tools.registry` refuses a tool with a name that breaks the grammar, a service tool
that is unscoped, a handler whose result the redactor cannot read, and two tools with the
same description. `ToolRegistry.freeze` says in its own docstring that it is "called at
startup". None of that ran anywhere, because until this module no code outside the tests
ever constructed a registry: `RowTool` existed, its `definition` and `reader` existed, and
nothing put the two together.

**That is the failure this repository keeps finding rather than a missing feature.** The
deploy that reported success and deployed nothing, the RLS sweep that had never run, the
document of record carrying a stale count: each was a mechanism that was correct, tested,
and never called. A registry that no process builds is a set of rules that has never
refused anything, and the first tool to break one of them would break it in production
against a registry assembled by whoever needed one that afternoon.

**So there is exactly one builder and the application calls it.** Not a helper the routes
may call if they need tools, and not a module-level singleton: `build_registry` returns a
frozen registry, `brain.app` puts it on `app.state` during startup, and a second registry
built later would have to be built deliberately by somebody who could see they were doing
it. The singleton is rejected for the reason the registry's own docstring rejects one, that
one test's registration would be visible to the next.

**Freezing at startup is the point, not tidiness.** `freeze` runs the checks that can only
be made once every tool is present, and it raises. A process that comes up with a broken
catalogue is a process answering questions from a tool set nobody validated, and the
alternative to raising is a warning at boot, which is a warning nobody reads after the
first week.

**What is registered today is one tool, and that is honest rather than embarrassing.**
`knowledge.columns.PRICE_LIST` is the only `TableClassification` in the source tree, so it
is the only entity there is a row tool for. The value here is not the count; it is that the
count is now produced by a builder that runs every rule, so the second tool is registered
through a door rather than beside one.

**The row source is injected and there is no default.** `RowTool.reader` binds to a
`RowSource`, and a builder that supplied its own would be a second path to data with its
own idea of what may be seen, which is exactly what `channels.adapter.ChannelAdapter`
refuses by having no `query` method. A caller with no source registers no row tools rather
than registering tools that read from nowhere, because a tool that is present and cannot
answer is worse than one that is absent: the catalogue offers it, the model picks it, and
the person is told the system has no data on a subject it has plenty of.

**So the application registers no row tools today, and the reason is a real mismatch rather
than an oversight.** `RowSource.rows` is synchronous and `brain.session` builds an
`AsyncEngine`, so nothing can implement the protocol against the pool the application
already has. There are exactly three ways out and none is a small edit:

- Make the row plane async, which changes `RowSource`, `read_rows` and every caller.
- Run the sync reader in a worker thread the way `lifespan` already runs migrations, which
  works and needs a second, synchronous engine.
- Give the application a sync engine beside the async one.

The last two both add a connection pool, and `docker-compose.yml` sizes PgBouncer at
`DEFAULT_POOL_SIZE=20` against `max_connections=100` on a host `brain.ops.wiring` shows is
already overcommitted. Picking one of these at the same time as wiring the registry would
be changing the deployed resource profile inside a commit about something else, and the
sizing deserves its own measurement. `build_registry` takes the source as a parameter
precisely so that whichever is chosen is one call site.

What this module does fix is the thing that was actually broken: a registry now exists, one
builder makes it, the application calls that builder at startup, and every rule runs on the
way in. Registering the first real tool is a `records=` argument, not an afternoon.

Task ids: M12.1.5
"""

from __future__ import annotations

from typing import Final

from brain.knowledge.columns import PRICE_LIST
from brain.knowledge.rows import RowSource, RowTool
from brain.tools.registry import ResultContract, ToolRegistry

#: The description each built-in row tool carries into the catalogue.
#:
#: Written here rather than at the construction site because `ToolRegistry.validate` refuses
#: two tools that share one, folded and stripped of punctuation, and the refusal is a
#: property of a pair. Keeping the descriptions in one mapping means the collision is visible
#: while it is being written rather than at the freeze that follows.
ROW_TOOL_DESCRIPTIONS: Final[dict[str, str]] = {
    "price_list": (
        "Read rows from the price list: SKU, product name, sell price, and the cost and "
        "margin columns for callers entitled to them"
    ),
}

#: Every entity the application ships a row tool for, with the classification that governs
#: it. One entry today. A second is one line here and no change anywhere else, which is the
#: shape a registry is supposed to have.
BUILT_IN_ROW_ENTITIES: Final = (PRICE_LIST,)


def build_registry(*, source: str, records: RowSource | None = None) -> ToolRegistry:
    """Every tool this application offers, checked and frozen (M12.1.5).

    `source` names the system the rows came from and is required. `RowTool` refuses an empty
    one already, with the argument that two sources' record ids collide by coincidence of
    integers; passing it through rather than defaulting keeps that refusal reachable instead
    of satisfying it with a placeholder nobody chose.

    `records` may be absent, and then no row tool is registered at all. See the module
    docstring: a tool that is present and cannot answer tells a person the system has no
    data on a subject it has plenty of, which is worse than the tool being missing, because
    a missing tool is a gap somebody notices and an empty answer is a fact somebody believes.

    Returns frozen. A caller receiving an unfrozen registry could register into it after the
    whole-registry checks had run, which is the same as not running them.
    """
    registry = ToolRegistry()

    if records is not None:
        for classification in BUILT_IN_ROW_ENTITIES:
            tool = RowTool(
                source=source,
                classification=classification,
                description=ROW_TOOL_DESCRIPTIONS[classification.entity],
            )
            registry.register(
                tool.definition(),
                tool.reader(records),
                # The reader returns a `TypedResult[RowRecord]`, which is what the redactor
                # reads. Declared rather than inferred, so a handler changed to return a
                # dictionary fails at registration instead of at the first redaction.
                result_contract=ResultContract.TYPED,
                scope=tool.scope,
            )

    return registry.freeze()
