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

**The trace records names and never values.** It is the one artifact of an answer that
outlives the answer, so it is the worst possible place to put the thing that was just
withheld. `RedactionTrace` refuses anything that is not a name, in the same way and for
the same reason as `brain.audit.ledger`.

Scope: this is domain logic. Nothing here touches a database, a channel or a model. The
policy is a value passed in, not a table read.

Task ids: M4.1.1, M4.1.2, M4.1.3, M4.1.4, M4.1.5, M4.1.6, M4.2.3, M4.3.1, M4.3.2, M4.3.3,
M4.3.4, M4.4.1, M4.4.2, M4.4.4
"""

from __future__ import annotations

import enum
import re
from collections.abc import Iterable, Mapping
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
    """
    kept_values: list[Any] = []
    redactions: list[Redaction] = []
    dropped: list[DroppedObject] = []
    locked: list[LockedField] = []

    for index, item in enumerate(node):
        child = _walk(item, path=f"{path}[{index}]", row=row, depth=depth + 1, ctx=ctx)
        redactions.extend(child.redactions)
        dropped.extend(child.dropped)
        locked.extend(child.locked)
        if child.kept:
            kept_values.append(child.value)

    return _Node(
        value=kept_values,
        kept=True,
        redactions=tuple(redactions),
        dropped=tuple(dropped),
        locked=tuple(locked),
    )


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
        redactions.extend(child.redactions)
        dropped.extend(child.dropped)
        locked.extend(child.locked)
        if child.kept:
            out[key] = child.value

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

    for name, reason in mask.withheld:
        redactions.append(
            Redaction(entity=tag, record_id=record_id, field=name, reason=reason.value)
        )
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
