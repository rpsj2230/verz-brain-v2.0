"""One document's column classification over HTTP, and a review of a change to one column.

`brain.knowledge.columns` closed M7.5.1 and M7.5.2: a `TableClassification` is one entity,
a `ColumnRule` per column, and the derivations between them, and `project_row` narrows a
price-list row to the columns a caller may see. Nothing outside the process could read any
of it. So the rules that decide whether a person sees the cost beside the sell price were
legible only to whoever had the source tree open, which is the population that already
knows.

**This module adds no second model of what a classification is.** Every shape below is a
projection of `ColumnRule` and `TableClassification`, the current classification is read
through `brain.tools.startup.classification_for`, and the one interesting computation is
`brain.knowledge.columns.close_over_derivations` run twice. A route that built its own idea
of a column rule would be a second answer to "may this person see this field", and the
second answer is the one nobody keeps in step.

**The write capability carries the `admin` verb, and that is the whole security argument
for this surface.** A classification decides what other people may see. `gate.admission.
CHANNEL_VERBS` gives `admin` to CONSOLE and withholds it from API, so a client-credentials
token cannot retune what is confidential; `ASSURANCE_VERBS` gives it only to STRONG, so a
password-only session cannot either. Neither ceiling had to be written here and neither can
be argued with from here, which is the point: the verb is the declaration, and everything
that enforces it is upstream. `brain.routing_routes.MATRIX_WRITE` is the same choice for the
same reason one surface across.

**A caller who may not read a classification is refused in the words used for one that does
not exist.** An entity nothing classifies, an entity this caller holds nothing over, and a
caller who may read but not review all get `Absent` and one sentence. That is
`brain.api_routes.AN_ENTITY_IS_AS_ENUMERABLE_AS_A_RECORD` applied one level up and it is
why there is no route here that lists the classified entities: a list of them is a map of
what this installation holds, handed over for the price of one capability, and the console
already knows how to make a person name an entity because the records screen makes them.

**The review requires the read capability as well as the write one.** A review is a
comparison between the classification that stands and the one proposed, so its body carries
the current rules restated as a difference. Requiring only the write capability would make
the review a way to read what the read capability guards, one probe at a time, which is the
oracle the refusal above spends itself closing. `editable` on the read is therefore true
exactly when a review would be answered, and it is still presentation: the review checks
both capabilities again and refuses whatever the flag said. See
`AN_EDITABLE_FLAG_DECIDES_WHAT_IS_DRAWN_AND_NOTHING_ELSE`.

**Nothing here writes anything, and there is nowhere for it to write.** A
`TableClassification` is a constant in `brain.knowledge.columns` compiled into the process.
There is no `field_classification` table, no migration that creates one, and therefore no
audit row for a change to one: `brain.audit.ledger` records what it is given and it is given
nothing from here. Saying that plainly is the point of saying it at all. The review is the
half of an editor that can exist honestly today, and it is the half worth having first,
because it is the half that names a widening before somebody makes it.

**What a review answers is what the proposal does, including that it would not load.** A
classification that raises on construction leaves the previous one in place while a person
believes they changed it, which is the worst outcome available on this surface, so it is
reported as a finding rather than as a 422 about a malformed body. The words come from
`brain.knowledge.columns` and `brain.core.field_policy`, so a rule this module has never
heard of still explains itself.

**The widening verdict is computed here and never in the console.** See
`WIDENING_IS_DECIDED_ON_THIS_SIDE`. Two checks, and they catch different things. The
syntactic one compares two rules for one column. The closure one runs
`close_over_derivations` over both classifications for every caller short of exactly one
column and reports which columns such a caller would newly reach, which is the check that
names `margin` when somebody drops the derivation on `cost`.

**Found while building this, and not fixed here: the policy epoch does not move when a
derivation changes.** `FieldPolicy.epoch` digests the entity, the field, the capability, the
classification and the count declaration, and not `FieldRule.derived_from`. So dropping the
derivation on `cost` changes what `compute_mask` returns for everybody who lacks the cost
capability and leaves the epoch identical, which is precisely the failure that docstring
describes for `counts`: "a tightening that did not move the epoch would leave every cached
answer still emitting the count it was just told to withhold", with the sign reversed. The
answer cache would keep serving rows computed under the old closure. Two epochs are
therefore proof that a proposal is a change and never proof that it is not, which is what
`epoch_after` says about itself below, and it is why `widens` is computed from the rules
rather than from the digests. `brain.core.field_policy` belongs to another agent this
afternoon, so this module states the gap rather than reaching into it, and
`test_a_dropped_derivation_is_a_change_the_epoch_does_not_record` holds it where somebody
will see it.

**The closure check is bounded at callers short of one column, and that bound is real.**
Every subset of the columns is the honest question and it is exponential. One missing column
is the shape the derivation rule was written for, which is the price list: Finance holds
`read:price_list.cost` and nobody else does. A widening visible only to a caller short of
two columns at once is not reported, and this module does not pretend otherwise.

Rejected: a route that lists classified entities. See above; it is the installation's map.

Rejected: a body carrying the entity or the column. Both are path segments, for the reason
`brain.routing_routes` keeps `role` off `RungEdit`: a value that arrives twice is a value
two readers disagree about, and the address is the copy a person sent their colleague.

Rejected: taking a whole classification in one body. It reads as the safer shape and it is
the more dangerous one here, because the columns a proposal does not mention are then either
deleted or preserved, and whichever this module chose would be wrong for somebody. One
column's rule at a time cannot silently drop a column. What that costs is real and is stated
below: there is no way here to remove a classification, and removing one is how a field is
withheld from everybody at once.

Rejected: reporting a count of anything. `RungPage` argues it for the matrix and the console
keeps the rule everywhere; a classification is answered whole to every caller who may read
it at all, so there is nothing here for a count to disclose, and that is a fact about this
collection rather than a licence to start counting.

Task ids: M7.5.3
"""

from __future__ import annotations

import enum
from typing import Annotated, Final

import structlog
from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from brain.api import API_PREFIX, COMMON_RESPONSES
from brain.api_routes import Asked
from brain.core.entitlement import CAPABILITY_RE, Capability
from brain.core.errors import Absent
from brain.core.field_policy import Classification
from brain.knowledge.columns import (
    ColumnClassificationError,
    ColumnRule,
    TableClassification,
    close_over_derivations,
)
from brain.tools.startup import classification_for

log = structlog.get_logger()


# ------------------------------------------------------------ written-down reasons

#: Why a console may not work out for itself whether a change widens access.
WIDENING_IS_DECIDED_ON_THIS_SIDE: Final = (
    "Whether a proposed rule widens who may reach a column is computed here, from the "
    "classification that stands and the one proposed, and it is sent as an answer. A "
    "console that worked it out from the two rule sets would hold a second copy of the "
    "derivation closure, in the one place an attacker can edit, and the copy would drift "
    "the first time the closure changed. What the console does with the answer is "
    "presentation: it draws the widened columns loudly, and nothing about the request it "
    "made depends on what came back."
)

#: Why an editable flag is on the read, and the one thing it must never be.
AN_EDITABLE_FLAG_DECIDES_WHAT_IS_DRAWN_AND_NOTHING_ELSE: Final = (
    "editable says whether this caller holds both capabilities, for this classification, "
    "on this request. It exists so a console can leave out a control nobody can use. It is "
    "not a permission: the review checks the same two capabilities again and refuses "
    "whatever the flag said, and a console that skipped the request because the flag was "
    "false would be enforcing a rule in the copy an attacker edits."
)

#: Why a review changes nothing, and what does not exist as a result.
A_REVIEW_STORES_NOTHING_AND_NO_AUDIT_ROW_IS_WRITTEN: Final = (
    "A TableClassification is a constant compiled into this process. There is no table "
    "holding one, no migration that creates such a table, and therefore no audit row when "
    "somebody proposes a change: brain.audit.ledger is never called from here and this "
    "module opens no session. A review reads two classifications, compares them and "
    "answers. Applying the change is a source edit and a deploy. Anything on a screen "
    "reading as a save would be describing a mechanism that does not exist, which is worse "
    "than the gap it hides, because a person would stop checking."
)

#: Why a classification is answered whole rather than narrowed to the caller.
A_CLASSIFICATION_IS_NOT_FILTERED_PER_CALLER: Final = (
    "Every column of a classification is answered to every caller who may read the "
    "classification at all. It is one decision rather than a filtered list, so there is no "
    "difference between the columns that exist and the columns this caller was shown, and "
    "nothing on the page discloses anything by subtraction. That is a fact about this "
    "collection and not a general licence: the console still renders no count, because the "
    "rule it keeps is about what a screen may do."
)


# ----------------------------------------------------------------- the capabilities

#: Reading a classification. Not the same as reading a row of the table it governs: what it
#: discloses is which columns this company treats as confidential and what it takes to see
#: them, which is a statement about the policy rather than about anybody's data.
CLASSIFICATION_READ: Final = Capability(value="read:field_classification")

#: Reviewing a change to one. An `admin` verb, which `gate.admission.CHANNEL_VERBS` grants
#: to CONSOLE and withholds from API, and which `ASSURANCE_VERBS` gives only to a caller who
#: used a second factor. Both ceilings matter here for one reason: a change to a
#: classification is a change to what everybody else may see, and a change attributable to a
#: secret in a configuration file is a change nobody made.
CLASSIFICATION_WRITE: Final = Capability(value="admin:field_classification")


# ------------------------------------------------------------------------ the bounds

#: How many sibling columns one rule may name as its inputs. Far above any real table, and
#: present so that one body cannot ask this module to close over an unbounded set. A
#: derivation naming more columns than the table has refuses to load anyway; this is the
#: bound that applies before anything is constructed.
MAX_DERIVED_FROM: Final = 64

#: The longest a `would_not_load` sentence may be. The words come from the classification
#: layer and quote the caller's own submitted rule, so there is nothing here to leak; the
#: bound exists because a pydantic message over a large body is long enough to fill a screen.
MAX_REFUSAL_CHARS: Final = 300


# ------------------------------------------------------------------------ the shapes


class Change(enum.StrEnum):
    """What one proposed rule does to the column it governs.

    A closed vocabulary rather than a sentence, because the console renders each of these
    and the console must not be composing prose about a permission change. There is no
    member for removing a column, and that is honest rather than an omission: this surface
    edits one column's rule and cannot express a deletion. See the module docstring.
    """

    #: Nothing classified this column, so it was withheld from everybody by default-deny.
    ADDED = "added"
    #: A different capability reaches it. Which way that goes is not knowable here.
    CAPABILITY = "capability"
    #: A higher classification. Narrower channels may carry it and artifacts live less long.
    MORE_SENSITIVE = "more_sensitive"
    #: A lower one, which is the widening direction of the same fact.
    LESS_SENSITIVE = "less_sensitive"
    #: An input this column was declared to be reconstructable from is no longer named, so
    #: the closure has one fewer reason to withhold something.
    DERIVATION_DROPPED = "derivation_dropped"
    #: An input was added, so the closure has one more.
    DERIVATION_ADDED = "derivation_added"


#: The changes that widen who or what may reach a column.
#:
#: `LESS_SENSITIVE` is here and the reason is worth stating, because
#: `brain.core.field_policy` says a classification never permits: lowering one does not hand
#: anybody the capability. What it does widen is which channels may carry the value and how
#: long an artifact built from it is kept, and it moves the column's place in
#: `close_over_derivations._most_sensitive`, which decides which input is withheld when a
#: derivation has to be broken. Both are exposure.
#:
#: `CAPABILITY` is deliberately absent. Whether `read:price_list.cost` reaches more people
#: than `read:finance.cost` is a question about who holds what, and this module has no grant
#: store in front of it. Calling it a narrowing would be a guess in the dangerous direction.
#:
#: **Two of these three overlap the closure check completely, and both are kept.** Mutation
#: showed it: removing `ADDED` or `DERIVATION_DROPPED` changes no answer, because under the
#: one-missing-column sweep both always leave a column in `exposed` as well. `ADDED` does
#: because a classified column is reachable by every caller short of some other column;
#: `DERIVATION_DROPPED` does because a derivation on a column always fires for the caller
#: short of that column, so removing it always gives that caller something back. They are
#: kept because the overlap is a property of the sweep's bound rather than of the rule: a
#: widening visible only to a caller short of two columns at once is outside what the closure
#: check looks at, and the syntactic verdict still names it. `LESS_SENSITIVE` does not
#: overlap, which is why `test_lowering_a_classification_widens_even_when_no_column_becomes_
#: reachable` exists and is written against a column no derivation touches.
WIDENING_CHANGES: Final = frozenset(
    {Change.ADDED, Change.LESS_SENSITIVE, Change.DERIVATION_DROPPED}
)


class ColumnView(BaseModel):
    """One column's rule, as a console reads it.

    Every field of `ColumnRule` and nothing else. `required_capability` is flattened to its
    string because that is the spelling a grant table, a policy row and every support
    conversation use; the `Capability` wrapper is a validator rather than a vocabulary.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    column: str
    required_capability: str
    classification: Classification
    #: Sorted, so two identical classifications answer identically. `ColumnRule` holds a
    #: frozenset, whose iteration order is a property of the hashes in it.
    derived_from: list[str]


class ClassificationView(BaseModel):
    """One document's classification, whole.

    Not a `Page`. There is no cursor, no limit and no truncation: a classification is every
    column of one table, it is answered entire or not at all, and a shape carrying
    `next_cursor` would invite a pager over something that has no second page. See
    `A_CLASSIFICATION_IS_NOT_FILTERED_PER_CALLER`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity: str
    columns: list[ColumnView]
    #: `FieldPolicy.epoch`, the digest that changes when any rule does. Safe to answer
    #: because it is a digest over rules this response already carries in full, and useful
    #: because it is what says a classification is the one a review was computed against.
    epoch: str
    #: Whether this caller may have a change reviewed. Presentation only. See
    #: `AN_EDITABLE_FLAG_DECIDES_WHAT_IS_DRAWN_AND_NOTHING_ELSE`.
    editable: bool


class ColumnEdit(BaseModel):
    """The rule a console proposes for one column.

    `extra="forbid"`, so a body carrying `entity`, `column` or anything else is refused with
    a 422 naming the key rather than accepted and quietly ignored. Both of those are path
    segments and a model that accepted them as well would let a console change a column the
    address does not name.

    Every field is required, the derivation included, and that is `RungEdit`'s argument
    rather than a new one: a partial body means the field the console left out is the field
    a reader assumed it sent, and the form on the screen always holds all three. A rule that
    names no inputs says so with an empty list, which is a thing somebody wrote, where an
    absent key is a thing nobody did. The difference matters here more than it does on a
    rung, because dropping a derivation is the widening this whole surface exists to name.

    The capability's grammar is `brain.core.entitlement.CAPABILITY_RE` itself rather than a
    copy of it, so the pattern in the published document is the pattern the model enforces.
    What the pattern does not check is the verb set and the rule that a field rule must
    require a read; those live in `Capability` and `FieldRule`, and a body that breaks either
    is reported as a classification that would not load rather than as a malformed request,
    because that is what it is.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    required_capability: Annotated[
        str, Field(min_length=3, max_length=200, pattern=CAPABILITY_RE.pattern)
    ]
    classification: Classification
    derived_from: Annotated[list[str], Field(max_length=MAX_DERIVED_FROM)]


class ReviewView(BaseModel):
    """What one proposed rule would do, decided here.

    Deliberately carries no identifier, no timestamp and no revision. There is nothing
    stored for any of them to name, and a field that looked like the handle of a saved thing
    is the first thing a console would render as a receipt. See
    `A_REVIEW_STORES_NOTHING_AND_NO_AUDIT_ROW_IS_WRITTEN`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity: str
    column: str
    #: Why the proposed classification would not construct, in the classification layer's
    #: own words, or empty. When this is set every field below it is empty, because a
    #: classification that does not load has no consequences to report.
    would_not_load: str = ""
    #: What changed about this column. Empty means the rule is the one that stands.
    changes: list[Change] = []
    #: Whether any of it widens exposure. Computed here. See `WIDENING_IS_DECIDED_ON_THIS_SIDE`.
    widens: bool = False
    #: Columns a caller short of exactly one column would reach under the proposal and does
    #: not reach now. Never a count of anything withheld: these are named because the person
    #: reading them is deciding whether to expose them.
    exposed: list[str] = []
    #: The epoch of the classification that stands, and of the proposed one.
    #:
    #: Two digests that differ are proof that a proposal is a change. Two that agree are not
    #: proof that it is not: `FieldPolicy.epoch` does not digest `derived_from`, so dropping
    #: a derivation moves nothing here while changing what every caller short of a column
    #: sees. See the module docstring. Nothing in this response is derived from these two,
    #: for that reason.
    epoch_now: str = ""
    epoch_after: str = ""


# -------------------------------------------------------------------- the comparison


def view_of(classification: TableClassification, *, editable: bool) -> ClassificationView:
    """One classification, copied field by field.

    Written out rather than built from `dataclasses.asdict`, for the reason
    `brain.routing_routes.view_of` gives about its own: a field added to `ColumnRule` would
    otherwise arrive in a response because a copy loop was generous, and the fields on a
    rule that governs disclosure are the last ones to publish by accident.

    Columns come back in `TableClassification.columns()` order, which is sorted, so the
    order a rule was declared in cannot be read off the answer.
    """
    return ClassificationView(
        entity=classification.entity,
        columns=[
            ColumnView(
                column=rule.column,
                required_capability=rule.required_capability.value,
                classification=rule.classification,
                derived_from=sorted(rule.derived_from),
            )
            for name in classification.columns()
            if (rule := classification.rule_for(name)) is not None
        ],
        epoch=classification.policy().epoch(),
        editable=editable,
    )


def replacing(current: TableClassification, rule: ColumnRule) -> TableClassification:
    """The classification with this rule in place of whatever governed the same column.

    Replacement rather than addition, which is the shape `FieldPolicy.with_rules` takes and
    for its stated reason: this is the deliberate-edit path, so the caller is saying what the
    rule should now be. Adding would raise `ColumnClassificationError` for a column that is
    already classified, and a review that refused to look at the commonest edit there is
    would be a review of nothing.
    """
    kept = tuple(existing for existing in current.rules if existing.column != rule.column)
    return TableClassification(entity=current.entity, rules=(*kept, rule))


def changes_between(current: ColumnRule | None, proposed: ColumnRule) -> tuple[Change, ...]:
    """What one proposed rule does to the column it governs.

    A tuple rather than a single verdict, because a rule can change in two directions at
    once: a column can become less sensitive while gaining a derivation, and a precedence
    order that reported only the first would hide whichever the reader needed. Sorted, so
    two identical proposals report identically.
    """
    if current is None:
        # Nothing classified this column, so `compute_mask` withheld it from everybody by
        # default-deny. Classifying it is therefore a widening whatever the rule says, and
        # nothing else about the rule is a change, because there was nothing to change from.
        return (Change.ADDED,)
    found: list[Change] = []
    if current.required_capability != proposed.required_capability:
        found.append(Change.CAPABILITY)
    if proposed.classification.rank > current.classification.rank:
        found.append(Change.MORE_SENSITIVE)
    elif proposed.classification.rank < current.classification.rank:
        found.append(Change.LESS_SENSITIVE)
    if current.derived_from - proposed.derived_from:
        found.append(Change.DERIVATION_DROPPED)
    if proposed.derived_from - current.derived_from:
        found.append(Change.DERIVATION_ADDED)
    return tuple(sorted(found))


def newly_reachable(current: TableClassification, proposed: TableClassification) -> tuple[str, ...]:
    """Columns a caller short of exactly one column would see under the proposal, and not now.

    The check the syntactic comparison cannot make. Dropping the derivation on `cost` is one
    line in a rule and its consequence is that `margin` stops being withheld from everybody
    who lacks the cost capability, which is a fact about a different column entirely. This
    runs `close_over_derivations`, the function M7.5.1 shipped, over both classifications
    rather than reimplementing the closure.

    One missing column at a time, and `None` for the caller missing none. Every subset is the
    honest question and there are two to the power of the column count of them; one missing
    column is the shape the derivation rule was written for, and it is the price list:
    Finance holds `read:price_list.cost` and nobody else does.

    **The caller missing none earns its place on exactly one classification: an empty one.**
    Mutation established the bound rather than a comment guessing at it. Wherever both
    classifications name two columns or more, anything the whole-set comparison would find is
    already found by the sweep, because a column newly reachable to everybody is newly
    reachable to each caller short of some other column. The case it does not cover is the
    first column ever classified, where the sweep's only iteration removes that very column
    from both sides and compares two empty sets.
    `test_the_only_column_of_a_new_classification_is_reported_as_newly_reachable` is that
    case.

    A widening that only a caller short of two columns at once would see is not found here.
    That is a gap and it is stated rather than papered over; `WIDENING_CHANGES` is the
    syntactic half that still names one.
    """
    names = set(current.columns()) | set(proposed.columns())
    gained: set[str] = set()
    for missing in [*sorted(names), None]:
        short = set() if missing is None else {missing}
        before = close_over_derivations(frozenset(set(current.columns()) - short), current)
        after = close_over_derivations(frozenset(set(proposed.columns()) - short), proposed)
        gained |= after - before
    return tuple(sorted(gained))


def _would_not_load(exc: ColumnClassificationError | ValidationError) -> str:
    """The classification layer's own sentence for why a proposal does not construct.

    Quoted rather than composed, so a rule this module has never heard of still explains
    itself and so there is one wording per failure rather than two. Bounded, because a
    pydantic report over a body with sixty-four derivation entries in it is longer than a
    screen and the useful part is at the front.
    """
    if isinstance(exc, ValidationError):
        first = exc.errors()[0]
        return str(first.get("msg", ""))[:MAX_REFUSAL_CHARS]
    return str(exc)[:MAX_REFUSAL_CHARS]


def review(entity: str, column: str, edit: ColumnEdit) -> ReviewView:
    """What this proposal does to this column of this classification.

    Pure: it takes the entity's name and a proposed rule, reads the classification out of
    the same registry `brain.tools.startup` builds row tools from, and returns an answer. No
    session, no request, no state. That is what makes the property below assertable at all:
    a review is answered identically on a process with a database and on one without,
    because there is nothing here that could want one.

    A classification that would not construct is an answer rather than a refusal. See the
    module docstring: the failure it stands for is a person believing they changed something
    while the previous rules stayed in place.
    """
    current = classification_for(entity)
    if current is None:
        # Unreachable through the route, which checks this first. Written as a refusal
        # anyway, because the alternative is an `AttributeError` for whoever calls this
        # function directly, and the one thing this module must never do is answer a
        # comparison against a classification it does not have.
        raise _no_classification_here()

    epoch_now = current.policy().epoch()
    try:
        rule = ColumnRule(
            column=column,
            required_capability=Capability(value=edit.required_capability),
            classification=edit.classification,
            derived_from=frozenset(edit.derived_from),
        )
        after = replacing(current, rule)
        # The policy is built here rather than at the end, because this is where `FieldRule`
        # checks that the column is a name and that the capability is a read, and both are
        # findings rather than crashes.
        epoch_after = after.policy().epoch()
    except (ColumnClassificationError, ValidationError) as exc:
        return ReviewView(
            entity=entity,
            column=column,
            would_not_load=_would_not_load(exc),
            epoch_now=epoch_now,
        )

    changes = changes_between(current.rule_for(column), rule)
    exposed = newly_reachable(current, after)
    return ReviewView(
        entity=entity,
        column=column,
        changes=list(changes),
        widens=bool(set(changes) & WIDENING_CHANGES) or bool(exposed),
        exposed=list(exposed),
        epoch_now=epoch_now,
        epoch_after=epoch_after,
    )


# ------------------------------------------------------------------------- the wiring


def _no_classification_here() -> Absent:
    """The one refusal this router makes.

    Named rather than raised inline in four places, so the four refusals are the same
    refusal. A caller who may not read a classification, a caller who may read but not
    review one, and anybody asking about an entity nothing classifies get one answer;
    `brain.app.handle_brain_error` sends `Absent.public_message` and the string below reaches
    a log.
    """
    return Absent("no classification is answerable for this caller")


router = APIRouter(prefix=API_PREFIX, tags=["classification"])


@router.get(
    "/classifications/{entity}", response_model=ClassificationView, responses=COMMON_RESPONSES
)
async def classification(entity: str, asked: Asked) -> ClassificationView:
    """Every column of one document's classification, and what it takes to see each.

    The capability first and the entity second, which reads as the ordering property
    `brain.routing_routes.rungs` has and is not one. What makes the two refusals one refusal
    here is that both raise `_no_classification_here`, so the answer is identical whichever
    check fires and swapping the two lines changes no response. The order is kept because it
    is the order that stays correct the day this route grows a second thing to look at, and
    because `classification_for` reading a module constant rather than a database is a fact
    about today rather than a guarantee. It is not the thing under test, and saying so is
    cheaper than a test that would pass with either arrangement.
    """
    if not asked.reach.holds(CLASSIFICATION_READ, asked.now):
        log.info("classification not answerable", principal=asked.caller.principal.id)
        raise _no_classification_here()

    found = classification_for(entity)
    if found is None:
        log.info("classification not found", entity=entity)
        raise _no_classification_here()

    return view_of(found, editable=asked.reach.holds(CLASSIFICATION_WRITE, asked.now))


@router.post(
    "/classifications/{entity}/columns/{column}/review",
    response_model=ReviewView,
    responses=COMMON_RESPONSES,
)
async def review_column(entity: str, column: str, edit: ColumnEdit, asked: Asked) -> ReviewView:
    """What a proposed rule for one column would do. Nothing is stored.

    Both capabilities, and the same refusal for either. A caller who may read a
    classification and not review a change to it gets the answer a caller who may do neither
    gets, so the reply says nothing about which half they are missing.

    The entity is checked after the capabilities and produces the same refusal again, so a
    caller cannot use a proposal to find out what this installation classifies.
    """
    if not (
        asked.reach.holds(CLASSIFICATION_READ, asked.now)
        and asked.reach.holds(CLASSIFICATION_WRITE, asked.now)
    ):
        log.info("classification not reviewable", principal=asked.caller.principal.id)
        raise _no_classification_here()

    if classification_for(entity) is None:
        log.info("classification not found", entity=entity)
        raise _no_classification_here()

    return review(entity, column, edit)
