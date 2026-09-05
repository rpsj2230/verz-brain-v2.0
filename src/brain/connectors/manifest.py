"""What a connector declares about itself, and what is refused before it can be installed.

Everything here is checked once, at manifest review, in front of whoever wrote the
connector. That is the whole reason the file exists as a separate thing from the transports:
the questions below are about the connector rather than about a request, so asking them per
request would be asking a settled question repeatedly and finding out about the answer at
the worst moment.

Three rules do the work.

**The projection is a pointer, and five clauses keep it one.** The architecture states the
rule in two sentences: project a field if the fast lane must filter, sort or count on it
*and* the source will tell us when it changes; otherwise fetch it live. `projectability`
expands that into five clauses because two sentences are not reviewable, and because three
of the five come from the tier table rather than from the sentence: the permanent denylist,
the pointer shapes, and the twelve-field cap. Each clause has a distinct failure and each
failure names what to do instead.

**No change signal means no projected fields.** This is the clause that stops the projection
becoming a mirror, and it is the one a hurried author reaches for first, because a field
that cannot be kept fresh is exactly the field somebody wants to copy once and be done with.
A projection with no change signal is not a stale projection: it is a value that will be
quoted as current, forever, with nothing anywhere reporting it.

**A visibility predicate, never a resolved ACL.** We store the source's predicate and
evaluate it against the live entitlement set, so a person changing department gets a
different row set on their next query with zero writes and zero invalidation. Storing the
resolved list instead is what makes Microsoft's own connector documentation admit their
incremental crawls do not update permissions at all, and Glean's full crawls run on 28-day
cycles. `_assert_predicate_is_not_an_acl` refuses the resolved list in both the shapes it
arrives in: as a projected field full of principal ids, and as a predicate whose clause is
an `IN` over principal ids, which is the same list wearing a predicate's clothes.

And one thing that is pinned rather than checked. A third-party server can redefine a tool
between one connection and the next, and a description is what the model reads when it
chooses. So the digest covers the whole manifest including every tool description, and
`brain.connectors.registry` fails closed on reconnect when it moves. See
`WHAT_THE_DIGEST_COVERS_AND_WHY`.

Scope: domain logic. Nothing here opens a connection or reads a table.

Task ids: M11.1.7, M11.4.2, M11.4.3, M11.4.5, M11.4.6, M11.4.7
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Final

from brain.connectors.contract import (
    AccessMode,
    ConnectorContractError,
    ConnectorScope,
    CredentialBinding,
    TransportKind,
    identity_mode_default,
)
from brain.core.envelope import OBJECT_NAME_PATTERN, TOOL_NAME_PATTERN, IdentityMode, SideEffect
from brain.core.projection import MAX_PROJECTED_FIELDS, is_forbidden
from brain.core.scope import Op, Scope

# ------------------------------------------------------------------ written-down reasons
#: Why the digest covers descriptions, and why it does not cover the credential.
WHAT_THE_DIGEST_COVERS_AND_WHY = (
    "Everything a caller or a model reads is inside the digest, tool descriptions included: "
    "a description is what the model chooses on, so a server that silently rewrites one has "
    "changed what the connector does without changing a single name. The credential binding "
    "is deliberately outside it, so that rotating or rebinding a credential is not "
    "indistinguishable from a third party redefining the connector. If the binding were "
    "pinned, every rotation would fail closed on reconnect and the first fix anybody reached "
    "for would be to stop pinning."
)

#: Why the five-clause test is five clauses rather than the architecture's one sentence.
WHY_FIVE_CLAUSES = (
    "The architecture's rule is 'if the fast lane must filter, sort or count on it and the "
    "source will tell us when it changes, project it'. That is two clauses, and the other "
    "three are in the tier table beside it: the permanent denylist, the pointer shapes, and "
    "the twelve-field cap. Written as one sentence they get reviewed as one judgement; "
    "written as five they each have a distinct failure, and each failure names what to do "
    "instead, which is what stops the author renaming the field rather than fixing the "
    "design."
)

#: Why an entity with no change signal may project nothing at all.
NO_SIGNAL_MEANS_NO_PROJECTION = (
    "A projected field with no change signal is not a stale field. It is a value that will "
    "be filtered, sorted and counted on as though it were current, indefinitely, with "
    "nothing anywhere reporting that it stopped being true. The projection is 40 MB at this "
    "scale precisely because this clause holds; without it the projection is whatever "
    "anybody found convenient to copy, which is a mirror with a smaller name."
)


# ------------------------------------------------------------ change signals (M11.4.6)
class ChangeSignal(enum.StrEnum):
    """How the source tells us a projected record moved.

    Three real ones and an explicit absence. `NONE` exists as a value rather than as a null
    so that "this source tells us nothing" is a thing an author writes down and a reviewer
    reads, instead of a field somebody left blank.

    The three differ in what they cost and in what they miss, and the ordering below is not
    a preference. A webhook is immediate and drops silently when a delivery fails. CDC sees
    every change including the ones an application makes behind its own API, and needs
    database access we do not always have. An updated-since cursor misses hard deletes
    entirely: a record that was removed is simply one the cursor never mentions again, so a
    connector relying on one needs a periodic full pass to notice a deletion at all.
    """

    WEBHOOK = "webhook"
    CDC = "cdc"
    UPDATED_SINCE = "updated_since"
    NONE = "none"

    @property
    def is_a_signal(self) -> bool:
        return self is not ChangeSignal.NONE


class HotUse(enum.StrEnum):
    """What the fast lane needs the field for.

    Closed, and every member is a thing the fast lane genuinely cannot do against a
    federated field within its latency budget. `DISPLAY` is deliberately absent: wanting to
    show a value is not a reason to store it, and it is the reason every single field is
    wanted.
    """

    FILTER = "filter"
    SORT = "sort"
    COUNT = "count"
    #: Resolving one company's records across sources. The entity registry is local, and a
    #: join key is what makes federation possible at all.
    JOIN = "join"
    #: Naming which record this is, to a person or to a later fetch.
    IDENTIFY = "identify"


class FieldShape(enum.StrEnum):
    """What kind of pointer this field is.

    From the tier table: record ids, join keys, status enums, timestamps, and display labels
    of at most 120 characters. There is no shape here that can hold a body, and that is the
    design rather than an omission of one.
    """

    IDENTIFIER = "identifier"
    JOIN_KEY = "join_key"
    STATUS = "status"
    TIMESTAMP = "timestamp"
    LABEL = "label"


class ManifestError(ConnectorContractError):
    """A manifest was declared in a shape that cannot be installed.

    A subclass of `ConnectorContractError` rather than a sibling, so that a caller wanting to
    refuse any badly-shaped connector catches one thing, while a manifest reviewer wanting to
    report on manifests specifically can still catch this.
    """


# ---------------------------------------------------------- the resolved-ACL detector
#: Field names that hold a resolved permission list rather than a record attribute. Matched
#: by shape for the reason `brain.core.projection.NEVER_PROJECT_PATTERNS` gives about salary:
#: listing every spelling a connector might use is a losing game, and an author writing
#: `shared_with_users` is not evading the rule.
RESOLVED_ACL_RE: Final = re.compile(
    r"(^|_)(acl|acls|permissions|allowed|permitted|shared|viewers|readers|members|grantees)"
    r"(_|$)"
)

#: Fields whose values are principals. An `IN` clause over one of these is a resolved ACL
#: written as a predicate: it enumerates who may see the row, so it goes stale the moment
#: somebody moves department, which is the failure the predicate rule exists to prevent.
PRINCIPAL_FIELD_RE: Final = re.compile(
    r"(^|_)(user|users|user_id|principal|principal_id|member|members|owner_id|assignee_id"
    r"|email|account_id)(_|$)"
)

_NAME_RE: Final = re.compile(OBJECT_NAME_PATTERN)
_TOOL_NAME_RE: Final = re.compile(TOOL_NAME_PATTERN)


# ------------------------------------------------------- the five-clause test (M11.4.3)
@dataclass(frozen=True)
class ProjectedField:
    """One field somebody wants to keep locally, and the reasons they gave.

    `uses` is required rather than defaulted. A default would make "the fast lane needs this"
    the thing an author does not have to think about, and it is the clause the whole rule
    turns on.
    """

    name: str
    shape: FieldShape
    uses: tuple[HotUse, ...] = ()

    def __post_init__(self) -> None:
        if not _NAME_RE.match(self.name):
            msg = (
                f"projected field {self.name!r} is not a name; the field policy is looked up "
                "by this string, and a name nothing matches is withheld from everybody"
            )
            raise ManifestError(msg)


@dataclass(frozen=True)
class ClauseVerdict:
    """One clause of the five, and what it decided.

    Carries the remedy rather than only the objection. A refusal that does not say "fetch it
    live" invites the author to rename the field, which is the outcome
    `brain.core.projection` names in its own error text and the one this has to avoid too.
    """

    clause: str
    passed: bool
    reason: str = ""

    def __str__(self) -> str:
        return f"{self.clause}: {self.reason}" if not self.passed else f"{self.clause}: ok"


#: The five clauses, in the order they are evaluated, as the reviewer reads them.
CLAUSE_NAMES: Final[tuple[str, str, str, str, str]] = (
    "hot",
    "signalled",
    "permitted",
    "pointer-shaped",
    "within the cap",
)


def projectability(
    field: ProjectedField,
    *,
    signal: ChangeSignal,
    label_count: int,
    field_count: int,
) -> tuple[ClauseVerdict, ...]:
    """Every clause's verdict on one field, not just the first failure.

    All five are evaluated every time. Stopping at the first turns writing a connector into a
    guessing game where each fix reveals the next objection, which is the argument
    `brain.core.projection.check_projection` makes about its own violations, and the reason
    this returns a tuple rather than raising.

    The clauses, and what each one is protecting:

    **hot.** The fast lane must filter, sort, count, join or identify on it. Wanting to
    display a value is not on the list, deliberately: display is the reason every field is
    wanted, so admitting it admits everything.

    **signalled.** The source will tell us when it changes. See
    `NO_SIGNAL_MEANS_NO_PROJECTION`.

    **permitted.** Not on the permanent denylist in `brain.core.projection`. That list is a
    hard constant rather than configuration, and this clause is where a manifest meets it, so
    the refusal happens at review rather than at the first ingest.

    **pointer-shaped.** An identifier, join key, status enum, timestamp, or *the* label. At
    most one label per entity kind: two labels is a payload arriving in instalments, and the
    120-character limit does not stop six of them adding up to a ticket body.

    **within the cap.** Twelve fields per entity kind. Counted here as well as at ingest
    because a manifest that declares thirteen should be refused by review, and the ingest-time
    check in `assert_projectable` is the one that catches a connector returning more than it
    declared.
    """
    hot = ClauseVerdict(
        clause=CLAUSE_NAMES[0],
        passed=bool(field.uses),
        reason=(
            f"nothing in the fast lane filters, sorts, counts, joins or identifies on "
            f"{field.name!r}; fetch it live"
        ),
    )
    signalled = ClauseVerdict(
        clause=CLAUSE_NAMES[1],
        passed=signal.is_a_signal,
        reason=(
            f"the source offers no change signal, so a projected {field.name!r} would be "
            "quoted as current forever; fetch it live"
        ),
    )
    permitted = ClauseVerdict(
        clause=CLAUSE_NAMES[2],
        passed=not is_forbidden(field.name),
        reason=(f"{field.name!r} is on the permanent denylist; fetch it live, never store it"),
    )
    pointer = ClauseVerdict(
        clause=CLAUSE_NAMES[3],
        passed=not (field.shape is FieldShape.LABEL and label_count > 1),
        reason=(
            f"{field.name!r} is a second label on this entity kind; one label identifies a "
            "record, and several are a payload arriving in instalments"
        ),
    )
    within = ClauseVerdict(
        clause=CLAUSE_NAMES[4],
        passed=field_count <= MAX_PROJECTED_FIELDS,
        reason=(
            f"{field_count} fields declared, over the {MAX_PROJECTED_FIELDS} limit; the "
            "projection is a pointer, and past this it is a mirror"
        ),
    )
    return (hot, signalled, permitted, pointer, within)


def failed_clauses(verdicts: tuple[ClauseVerdict, ...]) -> tuple[ClauseVerdict, ...]:
    return tuple(v for v in verdicts if not v.passed)


# --------------------------------------------------------- one entity kind's projection
@dataclass(frozen=True)
class ProjectedEntity:
    """What is kept locally about one entity kind from one source, and why it is allowed.

    The visibility predicate is a `brain.core.scope.Scope` and is required. An unrestricted
    one is refused: a projection stored with no predicate has silently discarded the source's
    own permission model, and every row in it is then visible to anybody holding the entity's
    capability. That is not a narrower version of the source's rules, it is the absence of
    them, and nothing downstream can tell the difference.
    """

    entity: str
    fields: tuple[ProjectedField, ...]
    change_signal: ChangeSignal
    visibility: Scope

    def __post_init__(self) -> None:
        if not _NAME_RE.match(self.entity):
            msg = f"entity {self.entity!r} is not a name"
            raise ManifestError(msg)
        self._assert_no_duplicate_fields()
        # M11.4.7 is enforced by the `signalled` clause and nowhere else. An earlier draft
        # also refused here, at entity level, on the grounds that the clause explains one
        # field while the entity is the level an author has to change something at. Mutation
        # testing showed it was an equivalent mutant: with fields and no signal, every field
        # fails `signalled`, so this check could be deleted whole without a single test
        # noticing. Two checks that look like two enforcement points and are really one is
        # worse than one check, because the next person to edit this deletes whichever they
        # find first. The entity-level explanation moved into the failure text instead.
        self._assert_clauses_pass()
        self._assert_predicate_is_not_an_acl()

    def _assert_no_duplicate_fields(self) -> None:
        """Two declarations of one field are two different opinions about its shape.

        Refused rather than deduplicated, because deduplicating picks one silently and the
        one it picks decides whether the field counts as a label.
        """
        counts = Counter(f.name for f in self.fields)
        duplicated = sorted(name for name, count in counts.items() if count > 1)
        if duplicated:
            msg = (
                f"{self.entity} declares {duplicated} more than once; two declarations are "
                "two opinions about the field's shape and one of them would win silently"
            )
            raise ManifestError(msg)

    def _assert_clauses_pass(self) -> None:
        """Every field against every clause, reported together (M11.4.2, M11.4.3, M11.4.7).

        The single enforcement point for all three, which is why the entity-level
        explanations are appended here rather than raised separately above. A whole-entity
        failure and a one-field failure read very differently to the author, and the whole
        difference is text: the decision is the same decision either way.
        """
        labels = sum(1 for f in self.fields if f.shape is FieldShape.LABEL)
        problems: list[str] = []
        clauses: set[str] = set()
        for field in self.fields:
            verdicts = projectability(
                field,
                signal=self.change_signal,
                label_count=labels,
                field_count=len(self.fields),
            )
            failures = failed_clauses(verdicts)
            problems.extend(f"  - {v}" for v in failures)
            clauses.update(v.clause for v in failures)
        if not problems:
            return
        listed = "\n".join(dict.fromkeys(problems))
        msg = f"{self.entity} cannot be projected from this source:\n{listed}"
        if CLAUSE_NAMES[1] in clauses:
            # Every field failed the same clause for the same reason, and the reason is
            # about the source rather than about any of them. Saying so is what stops the
            # author reading a list of per-field objections as a list of per-field fixes.
            msg = (
                f"{msg}\n{self.entity} has {len(self.fields)} projected field(s) and no "
                f"change signal. {NO_SIGNAL_MEANS_NO_PROJECTION}"
            )
        raise ManifestError(msg)

    def _assert_predicate_is_not_an_acl(self) -> None:
        """Refuse a resolved permission list in either of the two shapes it arrives in.

        As a *field*, it is `shared_with` or `allowed_users`, and it is stale the moment
        somebody moves department. As a *predicate*, it is `IN` over a list of principal ids,
        which is the same list with a predicate's shape: it does not re-evaluate against the
        live entitlement set, because there is nothing in it that depends on the caller.

        `EQ` over a principal field is deliberately allowed. `owner_id = u_weiling` is a
        property of the record rather than an enumeration of who may read it, and refusing it
        would refuse the ordinary case of a source whose visibility genuinely follows
        ownership.
        """
        acl_fields = sorted(f.name for f in self.fields if RESOLVED_ACL_RE.search(f.name))
        if acl_fields:
            msg = (
                f"{self.entity} projects {acl_fields}, which is a resolved permission list; "
                "store the source's visibility predicate and evaluate it against the live "
                "entitlement set, so a mover gets a different row set with zero writes"
            )
            raise ManifestError(msg)

        if self.visibility.is_unrestricted():
            msg = (
                f"{self.entity} stores no visibility predicate; a projection with none has "
                "discarded the source's permission model rather than narrowed it, and every "
                "row is then visible to anybody holding the entity's capability"
            )
            raise ManifestError(msg)

        enumerated = sorted(
            clause.field
            for clause in self.visibility.clauses
            if clause.op is Op.IN and PRINCIPAL_FIELD_RE.search(clause.field)
        )
        if enumerated:
            msg = (
                f"{self.entity}'s visibility predicate enumerates principals in "
                f"{enumerated}; that is a resolved ACL wearing a predicate's shape, and it "
                "goes stale on the next joiner, mover or leaver with nothing reporting it"
            )
            raise ManifestError(msg)

    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields)


# ------------------------------------------------------------------ tools on a connector
@dataclass(frozen=True)
class ToolDeclaration:
    """One tool a connector offers, as the manifest declares it.

    Not a `brain.core.envelope.ToolDefinition`. That model is what the gate projects and the
    model reads, and it is registered by `brain.tools.registry` with a handler attached; this
    is what a manifest claims before any of that exists. Keeping them apart is what lets the
    registry refuse a declaration, rather than having to un-register something.

    `verifies_write` is the architecture's read-back requirement made declarable: a connector
    that cannot answer "did operation X land?" is restricted to read-only tools, and the
    restriction is declared here rather than discovered after a crash left an operation in
    UNKNOWN with no way out of it.
    """

    name: str
    description: str
    entity: str
    side_effect: SideEffect = SideEffect.NONE
    identity_mode: IdentityMode = dataclasses.field(default_factory=identity_mode_default)
    verifies_write: bool = False

    def __post_init__(self) -> None:
        if not _TOOL_NAME_RE.match(self.name):
            msg = (
                f"tool {self.name!r} is not source.verb_noun; the model picks from what it "
                "is shown, and a name that does not read as one is picked for the wrong reason"
            )
            raise ManifestError(msg)
        if not self.description.strip():
            msg = (
                f"tool {self.name!r} has no description; the model has one line and the name "
                "to choose from, and the description is inside the pinned digest for that reason"
            )
            raise ManifestError(msg)
        if not _NAME_RE.match(self.entity):
            msg = f"tool {self.name!r} returns entity {self.entity!r}, which is not a name"
            raise ManifestError(msg)
        if self.side_effect is not SideEffect.NONE and not self.verifies_write:
            msg = (
                f"tool {self.name!r} has side effect {self.side_effect} and declares no "
                "read-back; after a crash its operation sits in UNKNOWN with no way to "
                "resolve it except retrying, which repeats the action"
            )
            raise ManifestError(msg)


# --------------------------------------------------------------------- the manifest
@dataclass(frozen=True)
class ConnectorManifest:
    """Everything a connector declares, in one value that can be hashed and pinned.

    Frozen, like every declaration in this package, and for a reason particular to this one:
    the digest below is what a reconnect is checked against, and a manifest that could be
    mutated after registration would let the pinned value and the live value drift apart
    inside one process.
    """

    name: str
    version: str
    transport: TransportKind
    scope: ConnectorScope
    credential: CredentialBinding
    tools: tuple[ToolDeclaration, ...] = ()
    projections: tuple[ProjectedEntity, ...] = ()
    #: The name this connector's ceiling is registered under in `brain.ops.limits`. Empty
    #: means no verified ceiling exists, which `throttle.limits_for` treats as a reason to
    #: refuse rather than as a reason to invent one.
    ceiling: str = ""

    def __post_init__(self) -> None:
        if not _NAME_RE.match(self.name):
            msg = f"connector name {self.name!r} is not a name"
            raise ManifestError(msg)
        if not self.version.strip():
            msg = (
                f"connector {self.name!r} declares no version; an upgrade is recognised by a "
                "version change, so a connector without one can be redefined in place"
            )
            raise ManifestError(msg)
        self._assert_tools_are_covered_by_the_binding()
        self._assert_one_projection_per_entity()

    def _assert_tools_are_covered_by_the_binding(self) -> None:
        """A write tool on a read-only binding is a tool that will fail at the source.

        Refused here rather than left to fail, because the failure at the source arrives as
        a 403 during somebody's request and reads as a permission problem with the caller.
        The mismatch is between two things the manifest already states, so it is knowable
        without calling anything.
        """
        uncovered = sorted(
            tool.name for tool in self.tools if not self.credential.permits(tool.side_effect)
        )
        if uncovered:
            msg = (
                f"connector {self.name!r} declares {uncovered} with side effects, and its "
                f"credential binding is {AccessMode.READ_ONLY}; write is a separate "
                "deliberate grant and this manifest has not been given one"
            )
            raise ManifestError(msg)

    def _assert_one_projection_per_entity(self) -> None:
        counts = Counter(p.entity for p in self.projections)
        duplicated = sorted(name for name, count in counts.items() if count > 1)
        if duplicated:
            msg = (
                f"connector {self.name!r} projects {duplicated} twice; the twelve-field cap "
                "is per entity kind, and two declarations make it twenty-four by arithmetic"
            )
            raise ManifestError(msg)

    def projection_for(self, entity: str) -> ProjectedEntity | None:
        for projection in self.projections:
            if projection.entity == entity:
                return projection
        return None

    def tool_names(self) -> tuple[str, ...]:
        return tuple(sorted(tool.name for tool in self.tools))


# ------------------------------------------------------------- the pinned digest (M11.1.7)
#: Manifest fields deliberately left outside the digest. One entry, and adding a second
#: should be hard: every field not named here is pinned, so a field added to
#: `ConnectorManifest` is covered without anybody remembering to cover it. The opposite
#: default, an explicit include list, is how a new field ends up unpinned silently.
UNPINNED_FIELDS: Final[frozenset[str]] = frozenset({"credential"})

#: How many hex characters of the digest are carried. Sixty-four, the whole SHA-256: this is
#: compared for equality rather than looked up, so there is no index to keep small, and a
#: truncated digest is a collision surface offered for no benefit.
DIGEST_CHARS: Final = 64


def _canonical(value: Any) -> Any:
    """One JSON-shaped rendering of any declaration in this module.

    Recursive over dataclasses, enums, tuples and mappings, because the manifest is a tree of
    those and nothing else. It renders a dataclass as a sorted mapping of its fields, so the
    digest does not move when somebody reorders a declaration for readability, which would
    otherwise read as a third party redefining the connector.
    """
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: _canonical(getattr(value, f.name))
            for f in sorted(dataclasses.fields(value), key=lambda f: f.name)
        }
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, list | tuple):
        return [_canonical(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in sorted(value.items())}
    if hasattr(value, "model_dump"):
        # pydantic models, which is how `Scope` arrives. `mode="json"` so an enum inside a
        # clause renders as its value rather than as a repr that carries the class name.
        return value.model_dump(mode="json")
    return value


def digest_input(manifest: ConnectorManifest) -> str:
    """Exactly what is hashed, as text, so a test can read it and a reviewer can diff it.

    Separate from `manifest_digest` on purpose. A digest is a number nobody can inspect, so
    when a pin fails the first question is always "what changed", and a function that returns
    the input answers it without anybody having to reconstruct the serialisation by hand.
    """
    body = {
        f.name: _canonical(getattr(manifest, f.name))
        for f in sorted(dataclasses.fields(manifest), key=lambda f: f.name)
        if f.name not in UNPINNED_FIELDS
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def manifest_digest(manifest: ConnectorManifest) -> str:
    """The pin. SHA-256 over the whole manifest except the credential binding.

    See `WHAT_THE_DIGEST_COVERS_AND_WHY`. Rejected: hashing the tool names alone, which is
    the version most third-party integrations ship. It pins the shape of the catalogue and
    not its meaning, so a server that rewrites `xero.read_invoice`'s description from
    "one invoice" to "every invoice for the tenant" passes the pin and changes what the model
    does on the next request.
    """
    return hashlib.sha256(digest_input(manifest).encode("utf-8")).hexdigest()
