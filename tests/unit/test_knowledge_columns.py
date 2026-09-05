"""Column classification inside one document, and the subtraction it has to survive.

Task ids: M7.5.1, M7.5.2
"""

from __future__ import annotations

import pytest

from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.field_policy import Classification
from brain.core.redaction import render_lock
from brain.core.scope import Clause, Op, Scope
from brain.knowledge.columns import (
    PRICE_LIST,
    ColumnClassificationError,
    ColumnRule,
    TableClassification,
    close_over_derivations,
    project_row,
)

ROW = {
    "sku": "PKG-CARE-1",
    "name": "Care Plan",
    "sell_price": 1200,
    "cost": 400,
    "margin": 800,
    "department": "web",
}

SEES_SELL_PRICE = ("read:price_list.sku", "read:price_list.name", "read:price_list.sell_price")
SEES_MARGIN_TOO = (*SEES_SELL_PRICE, "read:price_list.margin")
SEES_EVERYTHING = (*SEES_MARGIN_TOO, "read:price_list.cost")


def _ents(*caps: str, principal: str = "p_wei_ling", scope: Scope | None = None) -> EntitlementSet:
    return EntitlementSet(
        principal_id=principal,
        grants=tuple(Grant(capability=Capability(value=c), scope=scope or Scope()) for c in caps),
    )


# ------------------------------------------------------- the price list (M7.5.2)
def test_a_caller_sees_the_sell_price_while_cost_and_margin_are_locked() -> None:
    """M7.5.2 itself, and the reason the whole feature exists. Without it the price list is
    restricted as a document, a second sanitised copy appears for everybody else, and within a
    year one of the two is stale and being quoted to clients."""
    view = project_row(PRICE_LIST, ROW, entitlement=_ents(*SEES_SELL_PRICE))
    assert view.values["sell_price"] == 1200
    assert "cost" not in view.values
    assert "margin" not in view.values
    assert view.locked == ("cost", "department", "margin")


def test_the_finance_director_sees_every_column() -> None:
    """The other half of "one document, different answers". If the wide case failed, the
    workaround would be a second unclassified copy of the price list, which is exactly the
    outcome the classification is meant to prevent."""
    view = project_row(PRICE_LIST, ROW, entitlement=_ents(*SEES_EVERYTHING))
    assert view.values["cost"] == 400
    assert view.values["margin"] == 800


def test_a_column_nobody_classified_is_withheld() -> None:
    """Default-deny, inherited from `brain.core.redaction`. `department` is on the row because
    the scope predicate needs it, and a row field that leaks because nothing classified it is
    the failure mode that looks exactly like a field meant to be public."""
    view = project_row(PRICE_LIST, ROW, entitlement=_ents(*SEES_EVERYTHING))
    assert "department" not in view.values
    assert "department" in view.locked


def test_a_column_out_of_scope_is_locked_even_when_the_capability_is_held() -> None:
    """One person can hold a column in one department and not in another. Deleting this makes
    the classification ignore the scope half of a grant, so a departmental grant reads as a
    company-wide one."""
    finance_only = _ents(
        *SEES_EVERYTHING,
        scope=Scope(clauses=(Clause(field="department", op=Op.EQ, value="finance"),)),
    )
    view = project_row(PRICE_LIST, ROW, entitlement=finance_only)
    assert view.values == {}
    assert "sell_price" in view.locked


# ------------------------------------------------------ the subtraction (M7.5.2)
def test_a_withheld_column_derivable_from_visible_ones_costs_its_most_sensitive_input() -> None:
    """Classifying `cost` and leaving `sell_price` and `margin` visible hides nothing: cost is
    a subtraction away and the person doing it has done nothing wrong. This is the same rule
    the redactor applies to a count beside a filtered list."""
    view = project_row(PRICE_LIST, ROW, entitlement=_ents(*SEES_MARGIN_TOO))
    assert "cost" not in view.values
    assert "margin" not in view.values, "margin plus sell price reconstructs the cost"


def test_breaking_a_derivation_keeps_the_column_the_company_needs() -> None:
    """Withholding every input would take the sell price away from everybody and return the
    whole company to asking Finance for a number, which is the outcome that gets the feature
    turned off."""
    view = project_row(PRICE_LIST, ROW, entitlement=_ents(*SEES_MARGIN_TOO))
    assert view.values["sell_price"] == 1200


def test_the_closure_keeps_going_until_nothing_is_derivable() -> None:
    """One removal can leave a second derivation standing, because the column just withheld
    may itself be reconstructable. A single pass would fix the first and leave the second,
    and every test about the first would pass."""
    cascade = TableClassification(
        entity="widget",
        rules=(
            ColumnRule(
                column="a",
                required_capability=Capability(value="read:widget.a"),
                classification=Classification.PUBLIC,
            ),
            ColumnRule(
                column="b",
                required_capability=Capability(value="read:widget.b"),
                classification=Classification.INTERNAL,
            ),
            ColumnRule(
                column="c",
                required_capability=Capability(value="read:widget.c"),
                classification=Classification.INTERNAL,
                derived_from=frozenset({"a", "b"}),
            ),
            ColumnRule(
                column="d",
                required_capability=Capability(value="read:widget.d"),
                classification=Classification.CONFIDENTIAL,
                derived_from=frozenset({"a", "c"}),
            ),
        ),
    )
    # d is withheld and reconstructable from a and c, so c goes; c is then withheld and
    # reconstructable from a and b, so b goes too.
    assert close_over_derivations(frozenset({"a", "b", "c"}), cascade) == frozenset({"a"})


def test_a_visible_column_with_a_derivation_is_left_alone() -> None:
    """The closure only breaks derivations of columns that were withheld. If it also fired on
    visible ones it would withhold the cost from the Finance Director because the cost is
    derivable from two numbers they are entitled to see."""
    assert close_over_derivations(
        frozenset({"sell_price", "cost", "margin"}), PRICE_LIST
    ) == frozenset({"sell_price", "cost", "margin"})


# ------------------------------------------------ the classification (M7.5.1)
def test_a_column_classified_twice_is_refused() -> None:
    """Two rules for one column make "may this person see it" an evaluation-order question,
    which is the ambiguity `PolicyConflictError` exists to refuse one layer down. Deleting this
    makes the answer depend on the order rows came back from a table."""
    with pytest.raises(ColumnClassificationError, match="classified twice"):
        TableClassification(
            entity="price_list",
            rules=(
                ColumnRule(
                    column="cost",
                    required_capability=Capability(value="read:price_list.cost"),
                    classification=Classification.CONFIDENTIAL,
                ),
                ColumnRule(
                    column="cost",
                    required_capability=Capability(value="read:price_list.sell_price"),
                    classification=Classification.INTERNAL,
                ),
            ),
        )


def test_a_derivation_naming_a_column_that_is_not_classified_is_refused() -> None:
    """A derivation pointing at a column that does not exist never fires, and a rule that never
    fires reads as a control while being a comment. It is the shape a typo takes."""
    with pytest.raises(ColumnClassificationError, match="not classified here"):
        TableClassification(
            entity="price_list",
            rules=(
                ColumnRule(
                    column="cost",
                    required_capability=Capability(value="read:price_list.cost"),
                    classification=Classification.CONFIDENTIAL,
                    derived_from=frozenset({"markup"}),
                ),
            ),
        )


def test_a_column_derived_from_itself_is_refused() -> None:
    """A rule with no answer, and one that would make the closure remove the column in order
    to protect it. Failing loudly at authoring time is where a typo should be caught."""
    with pytest.raises(ColumnClassificationError, match="derived from itself"):
        ColumnRule(
            column="cost",
            required_capability=Capability(value="read:price_list.cost"),
            classification=Classification.CONFIDENTIAL,
            derived_from=frozenset({"cost"}),
        )


def test_a_write_capability_cannot_govern_a_column() -> None:
    """Inherited from `FieldRule`, and worth a test here because this is the door those rules
    are built through. Without it, permission to change a price would confer permission to see
    the cost as a side effect."""
    rule = ColumnRule(
        column="cost",
        required_capability=Capability(value="write:price_list.cost"),
        classification=Classification.CONFIDENTIAL,
    )
    with pytest.raises(ValueError, match="must require a read capability"):
        rule.as_field_rule("price_list")


def test_the_locks_are_reported_in_a_stable_order() -> None:
    """An order carrying the source's column order differs between callers and is readable as
    a signal about what each of them was refused. It is the same reason the redactor sorts its
    own locked list."""
    view = project_row(PRICE_LIST, ROW, entitlement=_ents(*SEES_SELL_PRICE))
    assert list(view.locked) == sorted(view.locked)


def test_every_lock_renders_identically() -> None:
    """A lock that varied by column, classification or reason would let two people comparing
    screens work out which of them was refused for which reason. The lock text takes no
    arguments for exactly that reason."""
    view = project_row(PRICE_LIST, ROW, entitlement=_ents(*SEES_SELL_PRICE))
    assert {view.render(column) for column in view.locked} == {render_lock()}
    assert view.render("sell_price") == "1200"


def test_the_policy_epoch_moves_when_a_classification_changes() -> None:
    """The epoch goes into the answer cache key. Without it, tightening a column's
    classification leaves a window in which cached answers keep disclosing the column that was
    just restricted."""
    tightened = TableClassification(
        entity="price_list",
        rules=(
            *(rule for rule in PRICE_LIST.rules if rule.column != "sell_price"),
            ColumnRule(
                column="sell_price",
                required_capability=Capability(value="read:price_list.sell_price"),
                classification=Classification.CONFIDENTIAL,
            ),
        ),
    )
    assert tightened.policy().epoch() != PRICE_LIST.policy().epoch()
