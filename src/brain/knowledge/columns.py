"""Classifying the columns inside one document, so a price list can be shared once.

Pricing is a field-level problem wearing a document-level costume. A price list carries the
sell price everybody needs beside the cost and margin almost nobody may see, and the obvious
move is to restrict the document and keep a second, safer copy for the rest of the company.
Within a year there are three near-identical price lists and two of them are stale, which is
a worse outcome than the leak the restriction was meant to prevent, because a wrong price
quoted to a client is noticed by the client.

So the columns are classified and the document is not. A caller sees the sell price; the cost
column is a lock beside it.

**Derivation is the part that gets missed.** Classifying `cost` and leaving `sell_price` and
`margin` visible hides nothing at all: cost is a subtraction away, and the person doing the
subtraction is not doing anything wrong, because both numbers were shown to them. This is the
same rule `brain.core.redaction` applies to counts, where a total beside a filtered list hands
over the hidden remainder by subtraction. A column that can be reconstructed from visible
columns is a visible column, and the only way to withhold it is to withhold one of its inputs.

**The most sensitive input is the one withheld.** Two alternatives were rejected. Withholding
every input, which takes the sell price away from everybody and returns the whole company to
asking Finance for a number. And withholding nothing and reporting the risk, which is a
warning in a log about a disclosure that has already happened.

**This does not reimplement redaction.** The mask comes from `brain.core.redaction.compute_mask`
and the default-deny rule comes with it: a column nobody classified is withheld rather than
returned. What this module adds is the derivation closure, which the walker cannot do because
it sees one field at a time and derivation is a property of a set.

The rules are a knowledge-layer type rather than an extension of `brain.core.field_policy`,
because derivation is a relationship between the columns of one table and not a property every
field in the system has. It compiles down to ordinary `FieldRule`s, so the redactor stays the
single place a field-level decision is made.

Task ids: M7.5.1, M7.5.2
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from brain.core.entitlement import Capability, EntitlementSet
from brain.core.field_policy import Classification, FieldPolicy, FieldRule
from brain.core.redaction import compute_mask, render_lock


class ColumnClassificationError(Exception):
    """A column classification that could not do what it says.

    Outside the `brain.core.errors` taxonomy, like `PolicyConflictError` and for the same
    reason: this is a build-time failure that should stop a classification from loading,
    rather than something that degrades an answer at request time.
    """


@dataclass(frozen=True)
class ColumnRule:
    """One column of a tabular document, and what it takes to see it (M7.5.1).

    `derived_from` names the columns this one can be reconstructed from. It is a set rather
    than a formula because the arithmetic does not matter: whether the relationship is a
    subtraction, a ratio or a lookup, the disclosure is identical once every input is visible.
    Storing a formula would invite somebody to evaluate it, and a classification that computes
    values is a classification that has copied the data.
    """

    column: str
    required_capability: Capability
    classification: Classification
    derived_from: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if self.column in self.derived_from:
            msg = (
                f"{self.column!r} is declared as derived from itself, which is a rule with "
                "no answer; a derivation names the other columns that reconstruct this one"
            )
            raise ColumnClassificationError(msg)

    def as_field_rule(self, entity: str) -> FieldRule:
        """The ordinary field rule this column compiles to.

        `FieldRule` validates that the capability is a read, so a column governed by a write
        capability is refused here without this module restating the reason.
        """
        return FieldRule(
            entity=entity,
            field=self.column,
            required_capability=self.required_capability,
            classification=self.classification,
        )


@dataclass(frozen=True)
class TableClassification:
    """Every column of one tabular document, and the derivations between them.

    The entity name is the table's, not the document's, because that is what the field policy
    and every capability are written against: `read:price_list.cost` has to mean the same
    thing whether it arrives from an uploaded spreadsheet or from a connector.
    """

    entity: str
    rules: tuple[ColumnRule, ...]

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for rule in self.rules:
            if rule.column in seen:
                # Two rules for one column make "may this person see it" an evaluation-order
                # question, which is the ambiguity `PolicyConflictError` exists to refuse.
                msg = f"{self.entity}.{rule.column} is classified twice"
                raise ColumnClassificationError(msg)
            seen.add(rule.column)
        for rule in self.rules:
            unknown = sorted(rule.derived_from - seen)
            if unknown:
                # A derivation pointing at a column that does not exist never fires, and a
                # rule that never fires reads as a control while being a comment.
                msg = (
                    f"{self.entity}.{rule.column} is declared as derived from {unknown}, "
                    f"which {'are' if len(unknown) > 1 else 'is'} not classified here"
                )
                raise ColumnClassificationError(msg)

    def policy(self) -> FieldPolicy:
        """The field policy these columns compile to.

        Built fresh rather than cached, because a `FieldPolicy` carries the epoch that goes
        into the answer cache key, and a stale cached policy would hold an epoch that no
        longer describes the rules.
        """
        return FieldPolicy(rules=tuple(rule.as_field_rule(self.entity) for rule in self.rules))

    def rule_for(self, column: str) -> ColumnRule | None:
        for rule in self.rules:
            if rule.column == column:
                return rule
        return None

    def columns(self) -> tuple[str, ...]:
        """Every classified column, sorted, so two identical classifications report alike."""
        return tuple(sorted(rule.column for rule in self.rules))


def _most_sensitive(columns: Iterable[str], classification: TableClassification) -> str:
    """Which of these columns to withhold when a derivation has to be broken.

    Highest classification wins, and the column name breaks a tie. The tie-break is not
    cosmetic: without it, two equally sensitive inputs are chosen between by iteration order,
    so the same caller asking the same question twice could see different columns and would
    reasonably conclude the permission model is random.
    """
    ranked: list[tuple[int, str]] = []
    for column in columns:
        rule = classification.rule_for(column)
        rank = rule.classification.rank if rule is not None else Classification.RESTRICTED.rank
        ranked.append((rank, column))
    return max(ranked, key=lambda pair: (pair[0], pair[1]))[1]


def close_over_derivations(
    allowed: frozenset[str], classification: TableClassification
) -> frozenset[str]:
    """Remove visible columns that together reconstruct a withheld one.

    Iterated to a fixed point rather than swept once. Withholding one column can make a second
    derivation resolvable that was not before, and a single pass would leave the second one
    standing while every test about the first passed.

    The loop terminates because each round removes at least one column from a finite set, and
    a column is never added back.
    """
    surviving = set(allowed)
    changed = True
    while changed:
        changed = False
        for rule in classification.rules:
            if rule.column in surviving or not rule.derived_from:
                continue
            if rule.derived_from <= surviving:
                surviving.discard(_most_sensitive(rule.derived_from, classification))
                changed = True
    return frozenset(surviving)


@dataclass(frozen=True)
class ColumnView:
    """One row as one caller may see it.

    `locked` names the columns withheld and carries no reason, exactly as
    `brain.core.redaction.LockedField` does: the reason is the part that leaks, because "out
    of scope" tells the asker the column has values elsewhere and "unclassified" tells them
    about the policy. Every lock is the same lock.

    A locked column name is shown, and that is deliberate rather than an oversight. This is a
    row the caller is already entitled to see, and within such a row the lock is the product:
    it is what makes "you may see the sell price and not the cost" legible instead of leaving
    a person to wonder whether the column exists.
    """

    entity: str
    values: dict[str, Any]
    locked: tuple[str, ...] = ()

    def render(self, column: str) -> str:
        """What one column shows: its value, or the same lock text every viewer sees."""
        if column in self.values:
            return str(self.values[column])
        return render_lock()


def project_row(
    classification: TableClassification,
    row: Mapping[str, Any],
    *,
    entitlement: EntitlementSet,
    now: datetime | None = None,
) -> ColumnView:
    """One row of a tabular document, narrowed to what this caller may see (M7.5.2).

    The mask is computed by the redactor, so default-deny, the scope predicate and the reason
    taxonomy all behave here exactly as they do everywhere else. The derivation closure runs
    afterwards, because it needs the whole visible set and the walker only ever sees one field.

    The row is passed to the mask before anything is removed from it, which is the ordering
    `compute_mask` documents: a scope predicate evaluated against a row whose `department` key
    had already been dropped refuses everything for everybody, and that failure reads as a
    permission problem rather than as an ordering one.
    """
    present = [key for key in row if key not in ("entity", "id", "@entity", "@id")]
    mask = compute_mask(
        classification.entity,
        present,
        entitlement=entitlement,
        policy=classification.policy(),
        row=row,
        now=now,
    )
    visible = close_over_derivations(
        frozenset(name for name in mask.allowed if name in present), classification
    )
    return ColumnView(
        entity=classification.entity,
        values={name: row[name] for name in present if name in visible},
        # Sorted, so the order locks are reported in cannot carry the order the source
        # returned its columns in. That order differs between callers and is readable as a
        # signal about what each of them was refused.
        locked=tuple(sorted(name for name in present if name not in visible)),
    )


# ------------------------------------------------------- the price list (M7.5.2)

#: The classification a price list ships with. A default rather than a fixture, because the
#: architecture names this exact case as the one that drives the whole feature, and a default
#: nobody wrote down is a default every installation reinvents differently.
#:
#: `cost` and `margin` each name the other two columns as inputs, which is what makes the
#: subtraction visible to `close_over_derivations`. Sell price is deliberately not derived
#: from anything: it is the column the whole company needs, and a derivation on it would make
#: it the first thing withheld.
PRICE_LIST: Final = TableClassification(
    entity="price_list",
    rules=(
        ColumnRule(
            column="sku",
            required_capability=Capability(value="read:price_list.sku"),
            classification=Classification.INTERNAL,
        ),
        ColumnRule(
            column="name",
            required_capability=Capability(value="read:price_list.name"),
            classification=Classification.INTERNAL,
        ),
        ColumnRule(
            column="sell_price",
            required_capability=Capability(value="read:price_list.sell_price"),
            classification=Classification.INTERNAL,
        ),
        ColumnRule(
            column="cost",
            required_capability=Capability(value="read:price_list.cost"),
            classification=Classification.CONFIDENTIAL,
            derived_from=frozenset({"sell_price", "margin"}),
        ),
        ColumnRule(
            column="margin",
            required_capability=Capability(value="read:price_list.margin"),
            classification=Classification.CONFIDENTIAL,
            derived_from=frozenset({"sell_price", "cost"}),
        ),
    ),
)
