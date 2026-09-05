"""The last thing between the database and a person.

Everything above this module can be wrong. The gate can compute an entitlement too wide, a
connector can fetch a column nobody asked for, a tool can return more rows than its scope
predicate should have admitted, a model can be talked into asking for something it should
not have. None of that reaches anybody if the walker in this file does its job, which is
why the architecture calls redaction "the last line of defence, and the only one that
catches a bug in the layers above".

**What breaks without it.** Field-level permission stops existing. A person who may see a
client record sees every column on it, so "one person sees hours remaining and not
contract value" becomes a promise rather than a mechanism, and the only remaining control
is document-level: either you can read the record or you cannot. That is the model every
competing product settles for, and retrofitting field-level redaction onto it is a
rewrite.

Four rules run through everything here, and they are worth stating before the code.

**Default-deny.** A field the policy does not classify is withheld. A policy that does not
mention a field is not permission to show it. See `brain.core.field_policy` for why.

**A refusal and an absence are the same event, at record level.** If a person can see
nothing about a record, they get nothing, phrased identically to a record that does not
exist. A refusal that explains itself has already confirmed the thing exists: "you may not
see SNM's contract value" tells the asker that SNM has one. `brain.core.errors` collapses
DENIED into ABSENT on the message side; this module does the same on the data side, by
dropping the record rather than returning an empty husk of it.

Note where that rule stops. Within a record the caller is already entitled to see, a
withheld field renders as a lock, and the lock is the product: screen 3 shows a client
record with contract value marked Restricted, and the account manager asking the same
question in the same thread gets the figure. Disclosing that the field exists is
deliberate there, because the record's existence was legitimately disclosed first. The
indistinguishability rule is about records, and the lock is about fields inside one.

**No count of hidden items ever reaches a person.** "3 results hidden" tells the asker
exactly how much they were not allowed to know, and repeated with different filters it is
a search interface over data they cannot read. Counts belong in the trace, which is read
by an auditor, and `ChannelPayload` has no field that could carry one.

That rule has a second half, and the walker enforcing the first half strictly is exactly
what hides it (M4.2.5). A count can survive as an ordinary field. A client record showing
`ticket_count: 40` beside a list of tickets filtered to the twelve in the asker's own
department has told them "28 hidden" by subtraction, through a field the policy classified
and a capability they legitimately hold. So a field policy rule may declare the collection
its field counts, and this module withholds such a field whenever the collection it names
was filtered for this asker. A count over a collection that came back whole stays visible,
because the alternative is that every count in the system disappears and somebody switches
the rule off.

**The trace records names and never values.** It is the one artifact of an answer that
outlives the answer, so it is the worst possible place to put the thing that was just
withheld. `RedactionTrace` refuses anything that is not a name, in the same way and for
the same reason as `brain.audit.ledger`.

Scope: this is domain logic. Nothing here touches a database, a channel or a model. The
policy is a value passed in, not a table read.

Task ids: M4.1.1, M4.1.2, M4.1.3, M4.1.4, M4.1.5, M4.1.6, M4.2.3, M4.2.5, M4.3.1, M4.3.2,
M4.3.3, M4.3.4, M4.4.1, M4.4.2, M4.4.4
"""

from __future__ import annotations

import enum
import inspect
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime
from typing import Any, Final, cast

import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator

from brain.core.entitlement import Capability, EntitlementSet
from brain.core.envelope import Entity, Redaction, TypedResult
from brain.core.errors import Denied
from brain.core.field_policy import FieldPolicy

log = structlog.get_logger()


# --------------------------------------------------------------------- grammars

#: A field or entity name. The same grammar as `brain.core.field_policy.NAME_PATTERN` and
#: as the field half of a capability, restated here rather than imported so that this
#: module's guarantee does not move when somebody widens the policy grammar.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,119}$")

#: Real record ids in this system are mixed case (`recuA1B2C3` from Lark, `c_0447` from
#: Laravel), so case cannot be part of the rule. Matches `brain.audit.ledger.IDENTIFIER`.
_RECORD_ID_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,128}$")

#: A whole trace path: `records[0].tickets[1]`, and `payload[0][2]` for a list of lists.
#: The index group repeats, because a nested array produces consecutive subscripts with no
#: name between them. Found by the generative suite, which built one; before that this
#: pattern rejected the path and the redactor raised on a shape it should simply have
#: recorded.
_PATH_RE = re.compile(
    r"^[a-z][a-z0-9_]{0,119}(?:\[\d{1,9}\])*(?:\.[a-z][a-z0-9_]{0,119}(?:\[\d{1,9}\])*)*$"
)


# ------------------------------------------------------------------- vocabulary

#: The keys that carry the entity tag itself. Both spellings are accepted: the
#: architecture's result contract writes `@entity` and `@id`, and `brain.core.envelope`
#: models them as ordinary pydantic fields named `entity` and `id`, so a walker that
#: understood only one of the two would silently drop half the system's data.
ENTITY_KEYS: Final[tuple[str, ...]] = ("@entity", "entity")
ID_KEYS: Final[tuple[str, ...]] = ("@id", "id")
RESERVED_KEYS: Final[frozenset[str]] = frozenset(ENTITY_KEYS + ID_KEYS)

#: How deep the walk will go before it gives up and drops the branch.
#:
#: A guard rather than a limit anybody should reach. A JSON tree from `model_dump` cannot
#: cycle, but this walker also accepts hand-built mappings, and a self-referential one
#: would recurse until the interpreter stopped it. Failing closed on an absurd shape is
#: right for the same reason failing closed on an untagged one is: a shape we cannot
#: reason about is not a shape we should return.
MAX_DEPTH: Final = 12

#: What a withheld field renders as. One constant string, from screen 3.
LOCK_TEXT: Final = "Restricted"

#: The capability the opaque escape hatch demands (M4.1.6). Its own capability, held by
#: almost nobody, rather than an admin flag: a flag is a thing a person is, and this has
#: to be a thing a person holds, so it appears in an entitlement set, an ent_hash, a
#: joiners-movers-leavers report and an audit query like every other grant.
OPAQUE_CAPABILITY: Final = Capability(value="read:opaque_payload")

#: What an opaque answer is labelled with. The label is not decoration: an unredacted
#: payload that looks like a redacted one is how unredacted data ends up pasted into a
#: channel by somebody who believed the gate had already run.
OPAQUE_LABEL: Final = "unredacted opaque payload"


class RedactionReason(enum.StrEnum):
    """Why a field was withheld. Recorded in the trace, never shown to the asker.

    Closed, because the trace validator checks membership: a free-text reason is where
    somebody eventually writes "salary was 92000, above the threshold".

    These never reach a person, and the difference between them is exactly why. Telling an
    asker that a field is unclassified rather than ungranted tells them about the policy;
    telling them it is out of scope rather than ungranted tells them the field exists on
    records in some other department. `render_lock` takes no arguments precisely so that
    no path exists from these values to a rendering.
    """

    #: Nothing in the field policy classifies this field. The default-deny path (M4.2.2).
    UNCLASSIFIED = "unclassified"
    #: The policy classifies it and the caller holds no grant covering the capability.
    NO_GRANT = "no grant"
    #: The caller holds the capability, and not in a scope that admits this row.
    OUT_OF_SCOPE = "out of scope"
    #: The field counts a collection, and that collection was filtered for this caller
    #: (M4.2.5). The one reason here that is not about the field at all: the caller may hold
    #: every grant the count needs and still not be told the number, because the number and
    #: the list they were shown differ by exactly what was withheld from them.
    FILTERED_COLLECTION = "count of a filtered collection"


class DropReason(enum.StrEnum):
    """Why a whole object was dropped rather than a field withheld.

    Separate from `RedactionReason` because these answer a different question and are
    followed to different places. A withheld field is the system working; a dropped object
    is almost always a connector returning a shape it should not have.
    """

    #: No entity tag. The fail-closed path (M4.1.4).
    UNTAGGED = "untagged"
    #: Tagged, but carrying no usable record id.
    UNIDENTIFIED = "unidentified"
    #: Tagged and identified, and nothing on it is visible to this caller. This is the
    #: record-level collapse of DENIED into ABSENT (M4.3.3).
    NO_VISIBLE_FIELD = "no visible field"
    #: Deeper than `MAX_DEPTH`, or a mapping key that is not a name.
    TOO_DEEP = "too deep"
    UNNAMED_KEY = "unnamed key"


class UntypedShapeError(Exception):
    """A tool tried to return something the redactor cannot walk (M4.4.2).

    Not part of the user-facing error taxonomy, deliberately, and for the reason
    `ProjectionRefusedError` gives: nobody asking a question should ever see this. It is a
    contract violation by a tool, and it should stop that tool from being written rather
    than degrade somebody's answer at request time.

    The alternative considered and rejected was to walk an untyped shape defensively,
    dropping what could not be attributed. That fails quietly in the worst direction: a
    tool would appear to work, return progressively less as the redactor got stricter, and
    nobody would file a bug because a thin answer looks like a narrow entitlement.
    """


# -------------------------------------------------------------- the lock (M4.3.1)


def render_lock() -> str:
    """How a withheld field renders. Identical for every viewer, by construction.

    This function takes no arguments, and that is the mechanism rather than an accident of
    its implementation. A lock that varied by viewer, by field, by classification or by
    reason would make its own shape a side channel: two people comparing screens could
    read the difference and learn which of them was refused for which reason. A signature
    with nothing in it cannot vary by anything, so the property is checked by reading the
    signature rather than by trusting the body, and the invariant suite checks exactly
    that.
    """
    return LOCK_TEXT


# ------------------------------------------------------------- the mask (M4.1.2)


def has_substance(keys: Iterable[str]) -> bool:
    """True when a record carries anything beyond its entity tag.

    A record whose only keys are `@entity` and `@id` has had everything about it withheld,
    and returning that husk announces that the record exists to somebody who was not
    entitled to learn it. This is the predicate behind M4.3.3, and it has one definition
    because the walker and the mask must not be able to disagree about what "nothing"
    means.

    The walker applies it once, to the record it is about to return, and deliberately not
    also to the mask beforehand. An earlier draft did both; mutation testing showed the
    first check was an equivalent mutant, because a mask with no substance admits no keys,
    so no child is walked and the record arrives at the second check as a bare tag anyway.
    Two checks that look like two enforcement points and are really one is worse than one
    check, because the next person to edit this deletes whichever they find first.
    """
    return any(key not in RESERVED_KEYS for key in keys)


class Mask(BaseModel):
    """Which keys of one record this caller may see, and why the rest were withheld."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity: str
    allowed: frozenset[str] = frozenset()
    withheld: tuple[tuple[str, RedactionReason], ...] = ()

    def has_substance(self) -> bool:
        """Whether this mask admits anything beyond the entity tag.

        Used by the console preview and by tests rather than by the walker; see
        `has_substance` above for why the walker asks the question of the output instead.
        """
        return has_substance(self.allowed)


def compute_mask(
    entity: str,
    present_fields: Iterable[str],
    *,
    entitlement: EntitlementSet,
    policy: FieldPolicy,
    row: Mapping[str, Any],
    now: datetime | None = None,
) -> Mask:
    """The set of keys this caller may see on one record (M4.1.2).

    `present_fields` is what the record actually carries, not what the policy knows about.
    That direction matters for the lock: a field is locked only when the record has it, so
    a lock never advertises a column this record does not hold.

    `row` is the record's own values, used to evaluate the grant's scope predicate. It is
    computed by the caller from the record **before** any deletion, and that ordering is
    load-bearing: `Clause.matches` treats a missing field as not matching, so evaluating a
    departmental scope against a record whose `department` key had already been removed
    would refuse everything for everybody, and the failure would look like a permission
    problem rather than an ordering one.
    """
    allowed: set[str] = set()
    withheld: list[tuple[str, RedactionReason]] = []
    row_dict = dict(row)

    for name in present_fields:
        if name in RESERVED_KEYS:
            # The tag is not a field. Stripping it would make the record untyped, which
            # the next walk over the same data would treat as a reason to drop it whole.
            # It is safe to keep only because a record with nothing else left is dropped:
            # see `Mask.has_substance`.
            allowed.add(name)
            continue

        rule = policy.rule_for(entity, name)
        if rule is None:
            # Default-deny (M4.2.2). Not a gap, an answer.
            withheld.append((name, RedactionReason.UNCLASSIFIED))
            continue

        scope = entitlement.scope_for(rule.required_capability, now)
        if scope is None:
            withheld.append((name, RedactionReason.NO_GRANT))
            continue

        if not scope.matches(row_dict):
            # The capability is held, and not here. This is the case a per-person
            # permission cache gets wrong: one person can hold a field in one department
            # and not in another.
            withheld.append((name, RedactionReason.OUT_OF_SCOPE))
            continue

        allowed.add(name)

    return Mask(entity=entity, allowed=frozenset(allowed), withheld=tuple(sorted(withheld)))


# ------------------------------------------------------------------- the trace


class DroppedObject(BaseModel):
    """One object the walker refused to return, and where it was (M4.1.4).

    Carries a path and a reason and nothing else. It deliberately does not carry the
    object: the whole point is that we did not trust it, and copying an untrusted shape
    into the longest-lived record of the request would be a strange way to express that.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    entity: str = ""
    reason: DropReason

    @field_validator("path")
    @classmethod
    def _path_is_a_path(cls, v: str) -> str:
        if not _PATH_RE.match(v):
            msg = f"trace path {v!r} is not a path of names and indices"
            raise ValueError(msg)
        return v


class RedactionTrace(BaseModel):
    """What the redactor did, in names and counts, with no values anywhere (M4.4.4).

    The validator duplicates what the walker already guarantees, on purpose and for the
    reason `brain.audit.ledger.AuditEntry` gives about its own details: a trace also
    arrives by being loaded from a store, replayed by a test helper, or constructed by a
    later version of the code, and enforcing the rule at the type means there is one
    answer to "can a value be in a trace" rather than one answer per code path.

    Counts live here and nowhere else. The architecture's telemetry list asks every
    request to record a redaction count, and M4.3.2 forbids ever emitting one to a person;
    both are satisfied by putting the count in the object an auditor reads and leaving
    `ChannelPayload` without a field that could hold it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_epoch: str
    ent_hash: str
    redactions: tuple[Redaction, ...] = ()
    dropped: tuple[DroppedObject, ...] = ()
    #: True when the opaque escape hatch was used, so the trace says so before anybody
    #: reads the payload and assumes the gate ran over it (M4.1.6).
    opaque: bool = False

    @field_validator("redactions")
    @classmethod
    def _names_only(cls, v: tuple[Redaction, ...]) -> tuple[Redaction, ...]:
        reasons = {r.value for r in RedactionReason}
        bad: list[str] = []
        for item in v:
            if not _NAME_RE.match(item.entity):
                bad.append(f"entity {item.entity!r} is not a name")
            if not _NAME_RE.match(item.field):
                bad.append(f"field {item.field!r} is not a name")
            if not _RECORD_ID_RE.match(item.record_id):
                bad.append(f"record id {item.record_id!r} is not an identifier")
            if item.reason not in reasons:
                bad.append(f"reason {item.reason!r} is not one of {sorted(reasons)}")
        if bad:
            msg = "a redaction record would put a value in the trace: " + "; ".join(bad)
            raise ValueError(msg)
        return v

    @property
    def redaction_count(self) -> int:
        return len(self.redactions)

    @property
    def dropped_count(self) -> int:
        return len(self.dropped)

    def withheld_field_names(self) -> tuple[str, ...]:
        """Every field withheld, as `entity.field`, sorted and deduplicated.

        Deduplicated on purpose. A list with one entry per record would let a reader count
        the records, and a report is not a place to reintroduce a hidden-item count by the
        back door.
        """
        return tuple(sorted({f"{r.entity}.{r.field}" for r in self.redactions}))


# ----------------------------------------------------------------- the payload


class LockedField(BaseModel):
    """A field withheld from a record the caller may otherwise see (M4.3.1).

    Carries no reason. The reason is the part that leaks: "out of scope" tells the asker
    the field exists on records elsewhere, and "unclassified" tells them about the policy.
    Every lock is the same lock.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity: str
    record_id: str
    field: str

    def render(self) -> str:
        return render_lock()


class ChannelPayload(BaseModel):
    """The only shape that may leave for a channel (M4.4.1).

    What is absent from this model is the design. There is no count of dropped records, no
    redaction reason, no policy epoch and no trace: none of those is a thing an asker is
    allowed to learn, and `extra="forbid"` means one cannot be attached to a payload later
    by a channel adapter that thought it would be helpful.

    `source`, `fetched_at` and `truncated` are suppressed when no record survives. A
    payload that named the source it found nothing in would answer a question nobody may
    ask: "I looked in the finance ledger and found nothing" and "I found nothing" have to
    be the same sentence, or the set of sources a person cannot reach becomes enumerable
    by asking about each in turn. This is the same rule the architecture applies to named
    progress steps, where "reading finance ledger" leaks the ledger's existence.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    records: tuple[dict[str, Any], ...] = ()
    locked: tuple[LockedField, ...] = ()
    #: Empty normally. `OPAQUE_LABEL` when the escape hatch was used.
    label: str = ""
    source: str = ""
    fetched_at: str = ""
    truncated: bool = False


class RedactedAnswer(BaseModel):
    """The payload and the trace, kept apart.

    Two objects rather than one, so that the thing which goes to a channel and the thing
    which goes to an auditor cannot be confused for one another by a caller holding a
    single variable. `serialise_for_channel` returns only the first half, which is what
    makes "the serializer is the only path to a channel" a shape rather than a convention.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    payload: ChannelPayload
    trace: RedactionTrace


# -------------------------------------------------------------- the walk (M4.1.1)


@dataclass(frozen=True)
class _Node:
    """One node's result. `kept` is separate from `value` because None is a real value.

    The accumulators travel up rather than into a shared list so that a dropped record can
    discard its children's findings. A record nobody may see must not contribute locks to
    the payload, because a lock on a field of a record that was never disclosed would
    disclose the record.
    """

    value: Any
    kept: bool
    redactions: tuple[Redaction, ...] = ()
    dropped: tuple[DroppedObject, ...] = ()
    locked: tuple[LockedField, ...] = dataclass_field(default=())
    #: True when this node returned fewer elements than it was given (M4.2.5). Only a
    #: sequence ever sets it, and a record absorbs it rather than passing it up.
    #:
    #: The absorbing is the whole design of the flag, and the alternative was tried first.
    #: If a record passed its children's filtering upward, one untagged blob dropped inside
    #: one ticket would mark the client that holds it as filtered, then the list of clients,
    #: then every count anywhere above it. Every count in the company would vanish the first
    #: time a connector returned an odd shape, which is how a control gets switched off. A
    #: record that survives is one element of whatever holds it however much was pruned
    #: inside it, so counting is unaffected by anything below the element boundary.
    filtered: bool = False


@dataclass(frozen=True)
class _Context:
    """Everything the walk needs that does not change between nodes."""

    entitlement: EntitlementSet
    policy: FieldPolicy
    now: datetime | None


def _first_present(node: Mapping[Any, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in node:
            return node[key]
    return None


def _walk(node: Any, *, path: str, row: Mapping[str, str], depth: int, ctx: _Context) -> _Node:
    """Depth-first over one node (M4.1.1).

    Depth-first rather than breadth-first because the decision at a parent depends on what
    survived beneath it: a record is dropped when nothing of it is visible, and "nothing"
    cannot be known until its children have been walked.
    """
    if depth > MAX_DEPTH:
        return _Node(
            value=None,
            kept=False,
            dropped=(DroppedObject(path=path, reason=DropReason.TOO_DEEP),),
        )
    if isinstance(node, Mapping):
        return _walk_mapping(node, path=path, row=row, depth=depth, ctx=ctx)
    if isinstance(node, list | tuple):
        return _walk_sequence(node, path=path, row=row, depth=depth, ctx=ctx)
    # A scalar belongs to the key above it, and that key was mask-checked before we
    # recursed. There is nothing left to decide here.
    return _Node(value=node, kept=True)


def _walk_sequence(
    node: list[Any] | tuple[Any, ...],
    *,
    path: str,
    row: Mapping[str, str],
    depth: int,
    ctx: _Context,
) -> _Node:
    """Arrays, including arrays of mixed shapes (M4.1.5).

    Elements that drop are removed rather than replaced with a placeholder. A placeholder
    would be a hidden-item count written one element at a time.

    Removing them silently is right for the array and wrong for anything that counted it,
    which is what `filtered` is for (M4.2.5). An array that came back shorter than it went
    in is the subtraction a count field would complete.
    """
    kept_values: list[Any] = []
    redactions: list[Redaction] = []
    dropped: list[DroppedObject] = []
    locked: list[LockedField] = []
    filtered = False

    for index, item in enumerate(node):
        child = _walk(item, path=f"{path}[{index}]", row=row, depth=depth + 1, ctx=ctx)
        redactions.extend(child.redactions)
        dropped.extend(child.dropped)
        locked.extend(child.locked)
        # An array of arrays is one collection, so a loss in the inner array is a loss in
        # this one. Without this, `[[t1, t2], [t3]]` losing t2 keeps its outer length and a
        # count over it stays visible while the asker can see one ticket fewer than it says.
        filtered = filtered or child.filtered
        if child.kept:
            kept_values.append(child.value)

    return _Node(
        value=kept_values,
        kept=True,
        redactions=tuple(redactions),
        dropped=tuple(dropped),
        locked=tuple(locked),
        filtered=filtered or len(kept_values) != len(node),
    )


def _count_would_be_subtractable(
    collection: str,
    *,
    present: frozenset[str],
    out: Mapping[str, Any],
    children: Mapping[str, _Node],
) -> bool:
    """Whether emitting a count over `collection` would hand this caller a subtraction.

    Four cases, and the first is the one that keeps the rule usable (M4.2.5).

    **The record does not carry the collection at all.** The count stays. A summary record
    with `ticket_count` and no ticket bodies is the ordinary case, and there is nothing on
    screen to subtract the count from. Withholding here is the over-correction that empties
    every count in the system and gets the rule switched off a week later.

    **The record carries it and it did not come back.** Withheld. This is the mask having
    refused the collection, or the walk having dropped it whole, and it is the strongest
    version of the leak rather than an exemption from it: `ticket_count: 40` beside no
    tickets says all forty were withheld.

    **It came back and it is not a sequence.** Withheld, on the default-deny principle the
    rest of the module runs on. A count over a mapping or a scalar is a declaration this
    walker cannot check, and a check that cannot run is not a reason to emit the number.

    **It came back as a sequence.** Withheld if that sequence lost an element anywhere
    inside it, and emitted otherwise.
    """
    if collection not in present:
        return False
    if collection not in out:
        return True
    child = children.get(collection)
    if child is None:
        # Unreachable: a key reaches `out` only by way of `children`. Written as a refusal
        # rather than an assert because the one thing this function must never do is fall
        # through to emitting a count it did not manage to check.
        return True
    if not isinstance(child.value, list):
        return True
    return child.filtered


def _walk_mapping(
    node: Mapping[Any, Any], *, path: str, row: Mapping[str, str], depth: int, ctx: _Context
) -> _Node:
    """One object: tag it, mask it, recurse, and delete what is outside the mask.

    The key type is `Any` rather than `str` on purpose. A record that arrived through
    `model_dump` has string keys by construction, but this walker also accepts hand-built
    mappings from a connector, and a mapping keyed by something else is precisely the shape
    the loop below refuses. Annotating the parameter as it should be rather than as it is
    would make that branch unreachable to the type checker and, sooner or later, deleted.
    """
    tag = _first_present(node, ENTITY_KEYS)
    if not isinstance(tag, str) or not _NAME_RE.match(tag):
        # M4.1.4. An untagged object has no entity to ask a question about, so there is no
        # question we could ask. Passing it through would mean returning data nobody
        # checked, and returning it partially would mean guessing which half was safe.
        log.warning("redaction.object_dropped", path=path, reason=DropReason.UNTAGGED.value)
        return _Node(
            value=None,
            kept=False,
            dropped=(DroppedObject(path=path, reason=DropReason.UNTAGGED),),
        )

    record_id = _first_present(node, ID_KEYS)
    if not isinstance(record_id, str) or not _RECORD_ID_RE.match(record_id):
        # Stricter than the leaf asks for, deliberately. A tagged object with no usable id
        # cannot be cited (the architecture requires a citation to name the record and the
        # field), cannot be attributed in the trace, and cannot be pointed at by a
        # request-access route. An unattributable redaction is one nobody can audit, and
        # the module's whole claim is that its decisions are auditable.
        log.warning("redaction.object_dropped", path=path, reason=DropReason.UNIDENTIFIED.value)
        return _Node(
            value=None,
            kept=False,
            dropped=(DroppedObject(path=path, entity=tag, reason=DropReason.UNIDENTIFIED),),
        )

    unnamed: list[DroppedObject] = []
    own: dict[str, str] = {}
    named_items: list[tuple[str, Any]] = []
    for key, value in node.items():
        if not isinstance(key, str) or not _NAME_RE.match(key):
            # A key that is not a name is not a field. No rule can classify it, no grant
            # can be phrased about it, and no citation can point at it, so the only honest
            # thing to do with it is drop it.
            #
            # It is recorded by path and never by name, for the reason
            # `brain.audit.ledger.redact_details` gives: in
            # `{"SNM Construction Pte Ltd": "overdue"}` the key is the leak, and recording
            # it while dropping the value would keep the half that named the client.
            #
            # Found by the generative suite. Before this, such a key reached
            # `compute_mask`, came back as an unclassified withholding, and was written
            # into a `Redaction` whose validator then refused it, so the whole answer
            # raised instead of being redacted. A redactor that raises is a redactor
            # somebody takes out of the path.
            unnamed.append(DroppedObject(path=path, entity=tag, reason=DropReason.UNNAMED_KEY))
            continue
        named_items.append((key, value))
        if isinstance(value, str | int | float | bool):
            # `str(value)` rather than a type-aware conversion, because that is exactly
            # what `Clause.matches` does on the other side of the comparison. Two different
            # renderings of the same value would make a scope silently stop matching.
            own[key] = str(value)

    # The scope predicate is evaluated against the record as it arrived, and a child
    # inherits its ancestors' values only for keys it does not carry itself. Inheriting is
    # necessary because a nested ticket usually does not repeat its client's department;
    # letting the child's own value win is what stops a ticket in another department being
    # admitted by the department of the record it happened to be nested under.
    child_row: dict[str, str] = {**row, **own}

    mask = compute_mask(
        tag,
        (key for key, _ in named_items),
        entitlement=ctx.entitlement,
        policy=ctx.policy,
        row=child_row,
        now=ctx.now,
    )

    out: dict[str, Any] = {}
    redactions: list[Redaction] = []
    dropped: list[DroppedObject] = list(unnamed)
    locked: list[LockedField] = []
    children: dict[str, _Node] = {}

    for key, value in named_items:
        if key not in mask.allowed:
            # M4.1.3. Deleted, not blanked. A key present with a placeholder value is
            # still a key, and the next thing to serialise it would carry the placeholder
            # into a model's context as though it were data.
            continue
        child = _walk(
            # `key` needs no sanitising here: it reached `named_items` only by matching
            # `_NAME_RE`, so it is already a name and cannot itself be the leak.
            value,
            path=f"{path}.{key}",
            row=child_row,
            depth=depth + 1,
            ctx=ctx,
        )
        children[key] = child
        redactions.extend(child.redactions)
        dropped.extend(child.dropped)
        locked.extend(child.locked)
        if child.kept:
            out[key] = child.value

    # M4.2.5, and it has to run here rather than in `compute_mask`. Whether a collection
    # was filtered is not knowable until the collection has been walked, so a count cannot
    # be decided at the same time as the fields around it. The mask has already applied the
    # count's own capability; this only ever takes away, never gives back.
    present = frozenset(key for key, _ in named_items)
    counts_withheld: list[tuple[str, RedactionReason]] = []
    for key in list(out):
        rule = ctx.policy.rule_for(tag, key)
        if rule is None or not rule.is_a_count:
            continue
        if _count_would_be_subtractable(rule.counts, present=present, out=out, children=children):
            del out[key]
            counts_withheld.append((key, RedactionReason.FILTERED_COLLECTION))

    if not has_substance(out):
        # M4.3.3, and the single enforcement point for it. The question is asked of what
        # survived rather than of the mask, because a record whose one permitted field held
        # nothing but an untagged object passes any mask-level check and still arrives here
        # as a bare tag and id. Whether a child survives is not knowable until it has been
        # walked, so this is the only place the question can be answered correctly.
        return _Node(
            value=None,
            kept=False,
            dropped=(
                *dropped,
                DroppedObject(path=path, entity=tag, reason=DropReason.NO_VISIBLE_FIELD),
            ),
        )

    # Sorted so that the order a source returned its columns in cannot be read back out of
    # the order the trace lists them, in the same way and for the same reason as `locked`.
    for name, reason in sorted([*mask.withheld, *counts_withheld]):
        redactions.append(
            Redaction(entity=tag, record_id=record_id, field=name, reason=reason.value)
        )
        if reason is RedactionReason.FILTERED_COLLECTION:
            # No lock, for the same reason an unclassified field gets none, arrived at from
            # the other direction. A lock is an offer to go and ask for the capability that
            # would reach the field, and here the caller may well hold that capability
            # already: what stopped them is that the collection beside it was filtered.
            # Granting the capability would change nothing, so the lock would route them
            # into a dead end.
            #
            # It also closes a side channel the lock would have opened. A lock here would
            # appear exactly when the collection beside it was filtered and not otherwise,
            # so its presence would say "records were withheld from that list" to anybody
            # who knew the rule. Withholding the count silently makes the answer identical
            # to a record whose source never carried a count at all.
            continue
        if reason is RedactionReason.UNCLASSIFIED:
            # No lock for a field nothing classifies, and the difference matters. A lock is
            # an offer: it says this field exists, somebody owns it, and there is a
            # capability that would reach it, which is what makes the request-access route
            # beside it lead somewhere. An unclassified field has no owner and no
            # capability, so a lock on it advertises a connector's column to everybody and
            # then routes anybody who asks into a dead end.
            #
            # It stays uniform, which is the property that matters: the decision depends on
            # the policy and not on the viewer, so every viewer who cannot see the field
            # sees exactly the same thing. The trace still records it, because "this
            # connector returns a column nobody classified" is precisely what an operator
            # needs to be told.
            continue
        # A governed key present on the record is locked whatever its value, including
        # null. Locking only non-null values would make the absence of a lock mean "this
        # one is empty", which is a value oracle built out of the thing meant to hide
        # values.
        locked.append(LockedField(entity=tag, record_id=record_id, field=name))

    return _Node(
        value=out,
        kept=True,
        redactions=tuple(redactions),
        dropped=tuple(dropped),
        locked=tuple(locked),
    )


# ------------------------------------------------------- the boundary (M4.4.2)


def require_typed_result(value: object) -> TypedResult[Entity]:
    """The only accepted return from a tool, checked rather than assumed (M4.4.2).

    Two checks, because the type alone is not enough. `TypedResult` is generic over
    `BaseModel`, so a tool could satisfy the annotation with a model carrying no entity
    tag, and the redactor would then have nothing to look a capability up by.

    mypy in strict mode catches the first case at build time; this catches both at the
    boundary, which is what a connector written in a hurry actually meets.
    """
    if not isinstance(value, TypedResult):
        msg = (
            f"a tool returned {type(value).__name__}, not a TypedResult; the redactor has "
            "no entity to ask a capability question about, so nothing can be returned"
        )
        raise UntypedShapeError(msg)
    untagged = sorted({type(r).__name__ for r in value.records if not isinstance(r, Entity)})
    if untagged:
        msg = (
            f"a tool returned records that are not entities: {untagged}; a record with no "
            "entity tag cannot be masked, cited or audited"
        )
        raise UntypedShapeError(msg)
    return cast("TypedResult[Entity]", value)


#: A return annotation of `TypedResult[Something]`, however it is spelled.
#:
#: Both spellings have to be admitted because both occur. A module with
#: `from __future__ import annotations` hands this a string exactly as written, and one
#: without it hands over a generic alias whose `str()` is fully dotted. Matching the text
#: rather than resolving the object is what lets the check run against a tool whose module
#: has not finished importing, which is when a registry actually runs it.
_TYPED_RESULT_RE = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_]*\.)*TypedResult\[\s*"
    r"(?:[A-Za-z_][A-Za-z0-9_]*\.)*([A-Za-z_][A-Za-z0-9_]*)\s*\]$"
)


def _annotation_text(annotation: object) -> str:
    """One rendering of an annotation, whether it arrived as a string or an object."""
    if isinstance(annotation, str):
        return annotation
    if isinstance(annotation, type):
        return annotation.__name__
    return str(annotation)


def assert_tool_returns_typed_result(tool: Callable[..., object]) -> None:
    """Refuse a tool whose declared return the redactor could not walk (M4.4.2).

    `require_typed_result` is the same rule enforced one request too late. By the time it
    fires, somebody has asked a question, a connector has been called, rows have been
    fetched, and the answer is an exception. This is the half a tool registry applies at
    registration, so the tool that cannot be redacted never becomes callable at all.

    Three refusals, and the first is the one that matters most:

    **No return annotation.** Refused, on the same default-deny principle as an
    unclassified field. An unannotated return is not "probably fine", it is a shape nobody
    has stated, and the redactor cannot check a shape nobody stated.

    **A return that is not a `TypedResult`.** Refused. A tool returning a dict gives the
    redactor no entity to ask a capability question about.

    **A bare `TypedResult`.** Refused too, and deliberately, even though it type-checks.
    `TypedResult` is generic over `BaseModel`, so a bare one promises only that something
    came back in a box; the entity parameter is the whole promise.

    The record type is checked against `Entity` when the name resolves in the tool's own
    module globals, and skipped when it does not, which happens for a class defined inside
    a function. A name that cannot be resolved is not evidence of anything, and refusing on
    it would refuse correct tools for where their types happen to be declared. mypy is the
    defence there, and `require_typed_result` is the one behind it.

    This checks a declaration rather than a value, and a tool can still lie by annotating
    one thing and returning another. That is why it does not replace `require_typed_result`
    at the boundary: this one catches the mistake, that one catches the lie.
    """
    try:
        signature = inspect.signature(tool)
    except (TypeError, ValueError) as exc:  # a builtin, or something not really a function
        msg = f"{getattr(tool, '__name__', tool)!r} has no readable signature to check"
        raise UntypedShapeError(msg) from exc

    name = getattr(tool, "__name__", repr(tool))
    if signature.return_annotation is inspect.Signature.empty:
        msg = (
            f"tool {name!r} declares no return type; the redactor cannot walk a shape "
            "nobody has stated, so an unannotated tool is refused at registration"
        )
        raise UntypedShapeError(msg)

    text = _annotation_text(signature.return_annotation)
    match = _TYPED_RESULT_RE.match(text.strip())
    if match is None:
        msg = (
            f"tool {name!r} declares it returns {text!r}; only TypedResult[SomeEntity] can "
            "be redacted, because only it carries the entity tag a capability is asked about"
        )
        raise UntypedShapeError(msg)

    record_type = getattr(tool, "__globals__", {}).get(match.group(1))
    if isinstance(record_type, type) and not issubclass(record_type, Entity):
        msg = (
            f"tool {name!r} returns TypedResult[{match.group(1)}], and {match.group(1)} is "
            "not an Entity; a record with no entity tag cannot be masked, cited or audited"
        )
        raise UntypedShapeError(msg)


# --------------------------------------------------- the path to a channel (M4.4.1)


class ChannelPathError(Exception):
    """A channel adapter was declared that could be handed something unredacted.

    Outside the user-facing taxonomy for the reason `UntypedShapeError` is: nobody asking a
    question should ever see this. It is a contract violation by an adapter, and it should
    stop that adapter being registered rather than degrade an answer at request time.
    """


#: What a channel adapter may never be given. Every one of these carries either data the
#: gate has not walked or the record of what the gate withheld, and an adapter holding one
#: is one line away from serialising it.
#:
#: `SimulationReport` is deliberately absent. It has nowhere to put a value, it is what
#: screen 13 renders, and the console is a channel like any other; banning it would refuse
#: the one legitimate adapter that shows an admin what a policy would withhold.
UNREDACTED_TYPE_NAMES: Final[frozenset[str]] = frozenset(
    {
        "TypedResult",
        "RedactedAnswer",
        "RedactionTrace",
        "Redaction",
        "DroppedObject",
        "Mask",
        "Entity",
    }
)


def _names_in(annotation: object) -> frozenset[str]:
    """Every identifier appearing in an annotation, however it is spelled.

    Crude on purpose. `ChannelPayload`, `"ChannelPayload | None"`,
    `redaction.ChannelPayload` and `list[ChannelPayload]` all have to read the same, and a
    parser that understood the type algebra would be a second, subtly different opinion
    about what an annotation means.
    """
    return frozenset(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", _annotation_text(annotation)))


def assert_channel_adapter(adapter: Callable[..., object]) -> None:
    """Refuse an adapter that could reach a channel by any route but the serialiser (M4.4.1).

    `serialise_for_channel` being the only function that returns a `ChannelPayload` is a
    proof about this module, and the invariant suite checks it. It says nothing at all
    about the code on the other side, which is where a channel adapter lives and where the
    rule is actually broken: an adapter handed a `RedactedAnswer` "because it needed the
    source name too" reaches the trace, the reasons and the dropped records, and every one
    of those is a hidden-item count or a value.

    So the rule is enforced on what an adapter can be given rather than on what it does. An
    adapter whose parameters are a `ChannelPayload` and some scalars cannot serialise
    unredacted data, because it was never handed any. That is checkable by reading a
    signature, in the same way and for the same reason `render_lock` is checked by reading
    a signature rather than by trusting its body.

    Four refusals:

    **An unannotated parameter.** Default-deny. An unannotated parameter can hold anything,
    including the whole answer, so it cannot be shown safe.

    **`*args` or `**kwargs`.** The same argument. A signature that accepts anything has
    declared nothing.

    **A parameter naming an unredacted type.** The leak itself.

    **No `ChannelPayload` parameter at all.** An adapter that takes no payload is either
    fetching its own data, which is the bypass this exists to stop, or is not a channel
    adapter and should not be registered as one.

    Rejected: scanning the adapter's source for `redact(` or `.trace`. It reads as
    stricter and is weaker, because it is defeated by any indirection at all, and it
    forbids an adapter's own tests from importing the module they test.
    """
    try:
        signature = inspect.signature(adapter)
    except (TypeError, ValueError) as exc:
        msg = f"{getattr(adapter, '__name__', adapter)!r} has no readable signature to check"
        raise ChannelPathError(msg) from exc

    name = getattr(adapter, "__name__", repr(adapter))
    takes_a_payload = False
    for parameter in signature.parameters.values():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            msg = (
                f"channel adapter {name!r} takes {parameter.name!r} as *args or **kwargs; "
                "a signature that accepts anything has declared nothing, and an adapter "
                "that could be handed the whole answer is a path around the serialiser"
            )
            raise ChannelPathError(msg)
        if parameter.annotation is inspect.Parameter.empty:
            msg = (
                f"channel adapter {name!r} has an unannotated parameter {parameter.name!r}; "
                "an unannotated parameter can hold the unredacted answer, so it is refused "
                "for the same reason an unclassified field is withheld"
            )
            raise ChannelPathError(msg)
        names = _names_in(parameter.annotation)
        forbidden = sorted(names & UNREDACTED_TYPE_NAMES)
        if forbidden:
            msg = (
                f"channel adapter {name!r} would be handed {forbidden} in {parameter.name!r}; "
                "only a ChannelPayload may cross to a channel, and an adapter holding "
                "anything else can serialise what the gate removed"
            )
            raise ChannelPathError(msg)
        takes_a_payload = takes_a_payload or "ChannelPayload" in names

    if not takes_a_payload:
        msg = (
            f"channel adapter {name!r} takes no ChannelPayload; an adapter with no payload "
            "is fetching its own data, which is the path around the serialiser this refuses"
        )
        raise ChannelPathError(msg)


@dataclass
class ChannelAdapterRegistry:
    """The door a channel adapter passes through, and the list of the ones that did.

    An instance rather than a module-level singleton, because a singleton is process state
    in a module whose whole claim is that it holds none: it would make one test's
    registration visible to the next, and "which adapters are registered" would depend on
    import order. Whoever owns the channel layer owns one of these.

    The registry is not the guarantee. `assert_channel_adapter` is, and it can be called
    on its own by a test that never registers anything. This exists so that the check has
    somewhere it is unavoidably applied, and so that an operator can ask what passed it.
    """

    _adapters: dict[str, Callable[..., object]] = dataclass_field(default_factory=dict)

    def register[F: Callable[..., object]](self, adapter: F) -> F:
        """Check an adapter and record it. Usable as a decorator; returns it unchanged.

        Returning the function unchanged rather than a wrapper is deliberate. A wrapper
        would put this module in the call path of every message the company sends, and a
        redaction module that can break message delivery is one somebody routes around.
        """
        assert_channel_adapter(adapter)
        name = getattr(adapter, "__qualname__", "") or adapter.__name__
        existing = self._adapters.get(name)
        if existing is not None and existing is not adapter:
            msg = (
                f"two different channel adapters are registered as {name!r}; one of them "
                "would be unreachable, and which one is decided by import order"
            )
            raise ChannelPathError(msg)
        self._adapters[name] = adapter
        return adapter

    def names(self) -> tuple[str, ...]:
        """Every registered adapter, sorted. What an operator asks this object."""
        return tuple(sorted(self._adapters))

    def __len__(self) -> int:
        return len(self._adapters)


# ---------------------------------------------------------------- the entry point


def redact[T: Entity](
    result: TypedResult[T],
    *,
    entitlement: EntitlementSet,
    policy: FieldPolicy,
    now: datetime | None = None,
    opaque: bool = False,
) -> RedactedAnswer:
    """Walk a typed result and return what this caller may see, plus the trace.

    `opaque=True` is the escape hatch (M4.1.6): the payload is passed through whole, for
    data that genuinely cannot be field-typed. It demands `OPAQUE_CAPABILITY`, flags the
    trace and labels the answer, and those three together are the entire compensating
    control, so none of them is optional.

    A caller who asks for it without holding it is refused rather than quietly downgraded
    to a redacted answer. The downgrade is the safer of the two outcomes for the data and
    the worse one for the system: a caller who believed they had the raw payload and
    silently received a narrowed one has no way to tell, and would go on to treat a
    redacted export as complete. `Denied` collapses to the same public message as `Absent`
    on the way out, so refusing here still says nothing to a person.
    """
    typed = require_typed_result(result)

    if opaque:
        if not entitlement.holds(OPAQUE_CAPABILITY, now):
            msg = f"opaque passthrough requires {OPAQUE_CAPABILITY.value}"
            raise Denied(msg)
        raw = tuple(record.model_dump(mode="json") for record in typed.records)
        return RedactedAnswer(
            payload=ChannelPayload(
                records=raw,
                label=OPAQUE_LABEL,
                source=typed.source if raw else "",
                fetched_at=typed.fetched_at if raw else "",
                truncated=typed.truncated if raw else False,
            ),
            trace=RedactionTrace(
                policy_epoch=policy.epoch(), ent_hash=entitlement.ent_hash(), opaque=True
            ),
        )

    ctx = _Context(entitlement=entitlement, policy=policy, now=now)
    kept_records: list[dict[str, Any]] = []
    redactions: list[Redaction] = []
    dropped: list[DroppedObject] = []
    locked: list[LockedField] = []

    for index, record in enumerate(typed.records):
        child = _walk(
            record.model_dump(mode="json"),
            path=f"records[{index}]",
            row={},
            depth=0,
            ctx=ctx,
        )
        redactions.extend(child.redactions)
        dropped.extend(child.dropped)
        locked.extend(child.locked)
        if child.kept:
            kept_records.append(cast("dict[str, Any]", child.value))

    anything_survived = bool(kept_records)
    payload = ChannelPayload(
        records=tuple(kept_records),
        # Sorted so that the order locks are reported in cannot carry the order the source
        # returned its columns in, which would differ between callers and be readable as a
        # signal about what each of them was refused.
        locked=tuple(sorted(locked, key=lambda item: (item.entity, item.record_id, item.field))),
        source=typed.source if anything_survived else "",
        fetched_at=typed.fetched_at if anything_survived else "",
        truncated=typed.truncated if anything_survived else False,
    )
    trace = RedactionTrace(
        policy_epoch=policy.epoch(),
        ent_hash=entitlement.ent_hash(),
        redactions=tuple(redactions),
        dropped=tuple(dropped),
    )
    return RedactedAnswer(payload=payload, trace=trace)


def serialise_for_channel[T: Entity](
    result: TypedResult[T],
    *,
    entitlement: EntitlementSet,
    policy: FieldPolicy,
    now: datetime | None = None,
    opaque: bool = False,
) -> ChannelPayload:
    """The only path from a typed result to a channel (M4.4.1).

    It returns the payload and nothing else. That is the whole point of it existing beside
    `redact`: a channel adapter calling this cannot reach the trace, the redaction reasons
    or the dropped records, because the value it is handed does not contain them. A rule
    that said "channels must not read the trace" would be a rule; this is a shape.
    """
    return redact(result, entitlement=entitlement, policy=policy, now=now, opaque=opaque).payload


# ------------------------------------------------------------- simulate (M4.2.3)


class SimulationReport(BaseModel):
    """What a policy would withhold from a person, in names only (M4.2.3).

    This model has nowhere to put data, and that is deliberate. A simulate mode that
    returned the unredacted payload alongside a report would be one boolean away from a
    breach, and that boolean is exactly the kind that gets set by a caching layer, a retry
    path or a test helper. So simulation is a separate function with a return type that
    physically cannot carry a value, rather than a flag on the redactor.

    It matches what screen 13 renders when an admin previews another person: "fields that
    would redact: contract_value, margin". Names, deduplicated, and no count of records.

    Shadow-testing a policy change is a composition rather than a mode: run this twice,
    once with the live policy and once with the proposed one, and diff the two reports.
    Building that in as a mode would have meant the redactor holding two policies at once
    and choosing between them, which is one more place to choose wrongly.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_epoch: str
    ent_hash: str
    #: `entity.field`, sorted and deduplicated.
    would_withhold: tuple[str, ...] = ()
    #: True when at least one record would be withheld whole. A flag rather than a count,
    #: because a count is a hidden-item count wherever it is read (M4.3.2), and this is
    #: read by an admin previewing a person rather than by that person.
    would_withhold_a_record: bool = False


def simulate_redaction[T: Entity](
    result: TypedResult[T],
    *,
    entitlement: EntitlementSet,
    policy: FieldPolicy,
    now: datetime | None = None,
) -> SimulationReport:
    """Run the full enforcing walk and report only what it withheld.

    Note what this does not do: it does not relax anything. Simulation runs the real gate,
    which is the same reason screen 13's preview is computed by the real gate rather than
    by an estimator. A preview with its own logic is the component most likely to lie.
    """
    answer = redact(result, entitlement=entitlement, policy=policy, now=now)
    withheld_records = any(
        item.reason is DropReason.NO_VISIBLE_FIELD for item in answer.trace.dropped
    )
    return SimulationReport(
        policy_epoch=answer.trace.policy_epoch,
        ent_hash=answer.trace.ent_hash,
        would_withhold=answer.trace.withheld_field_names(),
        would_withhold_a_record=withheld_records,
    )


# ------------------------------------------------------- request access (M4.3.4)

#: What the asker is told, always. One constant, with nothing in it that depends on the
#: entity, the field, the owner, whether an owner exists, whether the record exists or
#: whether the request will be granted. It is the same sentence for a field that is locked
#: and for one that was never there.
ASKER_ACKNOWLEDGEMENT: Final = "Your request has been passed on."


class OwnerNotice(BaseModel):
    """What the person who could grant the capability sees (M4.3.4).

    They see the question in the asker's own words, because a request stripped down to
    "grant read:client.contract_value to u_weiling" is a request nobody can judge. The
    owner is deciding whether this person should see this field for this reason, and the
    reason is the question.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    asker_id: str
    entity: str
    field: str
    question: str
    requested_capability: Capability


class AccessRequest(BaseModel):
    """A refusal turned into a route, with the information flowing one way only.

    The asymmetry is the design. The owner learns everything: who asked, what they asked,
    and which capability would answer it. The asker learns nothing they did not already
    have, because anything else would turn the request route into an oracle: "your request
    was sent to the Finance owner" names a department, "there is no owner for that field"
    says the field does not exist, and either can be asked repeatedly with different
    guesses until the shape of the company falls out.

    This is why the route belongs beside a lock rather than beside a record-level refusal.
    A lock is offered on a record the caller was already entitled to see, so asking about
    it discloses nothing new. Offering the same route where a record was withheld whole
    would break the rule that a refusal and an absence are the same event.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    asker_id: str = Field(min_length=1, max_length=128)
    entity: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    field: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    question: str = Field(min_length=1, max_length=2000)
    requested_capability: Capability

    def for_owner(self) -> OwnerNotice:
        return OwnerNotice(
            asker_id=self.asker_id,
            entity=self.entity,
            field=self.field,
            question=self.question,
            requested_capability=self.requested_capability,
        )

    def for_asker(self) -> str:
        """The constant, whatever this request is about."""
        return ASKER_ACKNOWLEDGEMENT
