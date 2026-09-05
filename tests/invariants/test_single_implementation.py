"""One implementation each of the rules the whole system rests on.

Every module in this repository argues against reimplementing a central rule, and several
record it as a rejected alternative in their own docstrings. Nothing checked it.

**This exists because of how the code is now being written.** Large parts of tonight's build
were produced by several agents working in parallel on files they could not see each other
touch. That is a good way to get work done and a very good way to end up with two functions
that compile a scope predicate, two enumerations of freshness, or a second way to intersect
an entitlement set. Each copy is reasonable in isolation. The damage is that they drift, and
the one that drifts is discovered by a permission being wrong rather than by a test.

The audit that prompted this was manual and it came out clean: `knowledge/rows.py` and
`knowledge/search.py` both import `core.scope_sql.compile_where` rather than rendering scope
SQL themselves, and `connectors/projection.py` reuses `gate.provenance.Freshness` rather than
declaring its own bands. A manual audit is worth exactly as much as the last time somebody
ran it, which is why it is written down here instead.

**What this does not do.** It counts definitions by name across the source tree. It cannot
tell that two differently-named functions do the same thing, and it is not meant to: the
failure it addresses is the literal one, where somebody writes `compile_where` again because
they did not know the first one existed. A conceptual duplicate needs a reader.

Task ids: M0.6.4
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "brain"

#: Name to the one module allowed to define it. The value is the argument, so a second
#: definition fails with the reason rather than with a count.
SOLE_DEFINITIONS: dict[str, tuple[str, str]] = {
    "compile_where": (
        "core/scope_sql.py",
        "a second scope-to-SQL renderer is a second place for the LIKE escaping and the "
        "IN-versus-string trap to be got wrong, and the wrong one decides who sees a row",
    ),
    "Freshness": (
        "gate/provenance.py",
        "two freshness scales means an answer described as ageing by one half of the system "
        "and live by the other, and a reader cannot tell which they were shown",
    ),
    "compute_mask": (
        "core/redaction.py",
        "the mask is the whole of field-level permission; a second one is a second answer to "
        "what a person may read",
    ),
    "identity_hash": (
        "gate/ingress.py",
        "the channel salt is what stops a binding on a weak channel being used to find one "
        "on a strong channel, and an unsalted second copy would not obviously look wrong",
    ),
}


def _definitions(name: str) -> list[str]:
    """Every module defining a function or class with this name, as a path relative to the
    package root. Parsed rather than grepped, so a mention in a docstring or a comment is not
    a definition."""
    found: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a file that will not parse fails elsewhere
            continue
        for node in ast.walk(tree):
            defines = isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            if defines and node.name == name:  # type: ignore[union-attr]
                found.append(path.relative_to(SRC).as_posix())
                break
    return found


@pytest.mark.parametrize(("name", "expected"), sorted(SOLE_DEFINITIONS.items()))
def test_a_central_rule_is_implemented_exactly_once(name: str, expected: tuple[str, str]) -> None:
    """Delete this and the next parallel build produces a second one, which reads as
    reasonable in the file it appears in and is wrong the day the two disagree.

    Asserted against the module as well as the count: moving the definition is a decision
    somebody should have to make deliberately, because everything importing it is written
    against where it lives."""
    home, why = expected
    found = _definitions(name)

    assert found == [home], f"{name} should exist once, in {home}, because {why}. Found: {found}"


def test_intersect_is_defined_only_where_a_type_owns_its_own_meaning() -> None:
    """`intersect` is the exception that proves the rule, and it is here so nobody deletes
    the parametrised test above on the grounds that this name breaks it.

    Three types define it and all three are correct: an entitlement set, a scope, and a
    residency requirement each intersect with their own kind and mean something different by
    it. A single generic `intersect` over all three would be a function that had to know
    about every type it might be handed, which is the shape that ends up with a default.

    What matters is that `EntitlementSet.intersect` is the only one anything computes reach
    with, and `gate.invoke` and `ops.automation.flow_reach` both call it rather than doing
    the set arithmetic themselves. Delete this and a fourth `intersect` looks like it
    belongs."""
    owners = set(_definitions("intersect"))

    assert owners == {
        "core/entitlement.py",
        "core/scope.py",
        "models/routing.py",
    }, "a new intersect appeared; it needs its own argument for owning that meaning"
