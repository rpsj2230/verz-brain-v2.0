"""What a canonical entity is, and what merging two of them may not do to a reader.

Entity resolution decides that Freshdesk company 42 and Xero contact `CON-99` are one
client. In a system where every field is permissioned, that decision is the single most
dangerous one available, because **merging two records merges two permission surfaces**. If
finance may read the Xero contact and everybody may read the Freshdesk company, a merged
view built the obvious way hands the whole company the Xero fields. Nobody writes that
deliberately. It arrives as a helpful union.

Several properties below are structural rather than documented, and each is a shape a future
author would have to change on purpose.

**A canonical entity holds no field values, so a merge has nothing to widen.**
`CanonicalEntity` is an id, a type and where it came from. There is no name on it, no email,
no revenue, no `fields` mapping. What a reader sees of an entity is what they already saw of
the source records underneath it, and a merge changes which records are gathered under one id,
never what any of them says. See `A_CANONICAL_ENTITY_HOLDS_NO_FIELDS`. The rejected design is
the obvious one: a canonical row carrying the best-known name, domain and address, refreshed
from whichever source is most trusted. That row is a projection with no permission surface of
its own, and every reader who reaches the entity reaches all of it.

**A merged view is built by filtering and never by inheritance.** `resolved_view` asks the
caller's `reaches` predicate about every row, one source record at a time, and admits only
what it says yes to. Nothing is admitted because a sibling was, which is what makes the merged
view exactly the intersection of the reader's existing reach with the entity's membership. See
`A_ROW_IS_IN_THE_VIEW_ONLY_IF_ITS_OWN_RECORD_IS`.

**The reach predicate is never shown a match score.** `MemberReach` takes a `SourceRef` and
nothing else, and `resolved_view` calls it with `link.member` rather than with the `Link`.
`ResolvedView` carries no confidence either, so nothing downstream can widen on one because
there is nothing there to read. That is the shape `brain.memory.formation.Recollection` uses
to keep memory from becoming authoritative: something that wanted to do the wrong thing would
have to add a field, in a module whose docstring argues against it. See
`A_SCORE_IS_EVIDENCE_AND_NEVER_A_PERMISSION`.

**DENIED and ABSENT are the same answer.** `resolved_view` returns `None` for an id that does
not exist and `None` for an id whose every member is out of reach, and those two are
indistinguishable to the caller. It emits no count, no total and no "and others", so there is
nothing to subtract: a resolver that said "this looks like a client you cannot see" has
disclosed that client. See `A_VIEW_CARRIES_NO_COUNT_OF_WHAT_IT_WITHHELD`.

**Nothing is copied off the canonical row into the view except its id and its type.** That is
not tidiness. `created_from` names the source record that first evidenced the entity, and a
reader who reaches a different member has no claim on it: handing it over names a record they
cannot read, which is the same disclosure by a quieter route. See
`THE_VIEW_CARRIES_NOTHING_OFF_THE_CANONICAL_ROW_BUT_ITS_ID_AND_ITS_TYPE`.

**One residual disclosure is left, and it is worth naming rather than implying away.** A reader
who reaches only their own record, and whose entity has been merged into another, is handed the
survivor's id, which is not the id they asked with. From that they can tell that their record
was merged with something. They cannot tell what: no name, no source, no id of any other
member, and above all no count, so "something" stays exactly as large as it was before they
asked. That residue is the price of M14.1.5, because an id that resolves forever has to resolve
to the survivor, and the only way to remove it would be to mint a per-reader id, which is a
second identifier space and a worse trade. It is bounded and it is not nothing, and a reader of
this module should know it is here rather than discover it.

**A merge moves one pointer and changes nothing else.** `er.alias`, `er.identifier` and
`er.link` go on naming the entity they were attached to; only `merged_into` is written. That
is what makes the pointer worth having, and it is why an id issued before a merge still
resolves afterwards: resolution follows the pointer rather than depending on rows having been
rewritten. See `A_MERGE_MOVES_ONE_POINTER_AND_NOTHING_ELSE`.

**Nothing here records who merged, or why.** `merged_at` says when. There is no approver, no
evidence, no pre-image and no audit row anywhere in this module or in the tables it declares.
A merge performed today can be undone by clearing one pointer and cannot be explained. That is
M14.5.1 and M14.5.4 and it is not built. See
`NOTHING_HERE_RECORDS_WHO_MERGED_OR_ON_WHAT_EVIDENCE`, which is a constant so the gap has to
be deleted rather than merely forgotten.

**There is deliberately no `same_entity(a, b)` predicate**, and its absence is a decision
rather than an omission. Answering "are these two the same" for a caller who reaches only one
of the two is precisely the disclosure this module refuses, and a helper with that signature
would be reached for by every caller who did not want to think about it.

Scope: domain logic. Nothing here opens a connection, reads a clock or touches a table. The
timestamps and the hash pepper are parameters, for the reason `brain.gate.provenance` gives
about its own clock.

Task ids: M14.1.1, M14.1.2, M14.1.3, M14.1.4, M14.1.5, M14.1.6
"""

from __future__ import annotations

import enum
import hmac
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Final, Protocol

from brain.core.envelope import OBJECT_NAME_PATTERN

# ------------------------------------------------------------------ written-down reasons
#: Why the canonical row has no name, no domain and no revenue on it.
A_CANONICAL_ENTITY_HOLDS_NO_FIELDS = (
    "Merging two records merges two permission surfaces. A canonical row carrying the "
    "best-known name, domain and contact details would be a projection with no permission "
    "surface of its own, assembled from every member and readable by anybody who reaches any "
    "one of them, so a merge would hand the readers of the open record the fields of the "
    "restricted one. There is nowhere on CanonicalEntity to put such a value: it is an id, a "
    "type and its own provenance. What a reader sees of an entity is what they already saw of "
    "the source records underneath it, through the ordinary field policy, one record at a "
    "time, and a merge changes which records are gathered under one id rather than what any "
    "of them says."
)

#: Why every row in a view is admitted individually rather than by association.
A_ROW_IS_IN_THE_VIEW_ONLY_IF_ITS_OWN_RECORD_IS = (
    "A merged view is a filter over what the reader already reaches, never a union that "
    "inherits. Every member, every alias and every identifier is admitted by asking the "
    "caller's reach predicate about that row's own source record, and nothing is admitted "
    "because a sibling was. This is the clause a merge would otherwise defeat: an alias is a "
    "name, and a name observed on a record the reader may not see is a fact about that "
    "record. So the view of a merged entity is exactly what the reader would have seen of its "
    "members separately, which is the property that makes a merge safe to perform without "
    "knowing who is currently reading."
)

#: Why nothing in the reach path is ever handed a match score.
A_SCORE_IS_EVIDENCE_AND_NEVER_A_PERMISSION = (
    "A match is a probability and a permission is not, and the way that rule is lost is not "
    "somebody writing 'if confidence > 0.9: show it'. It is a reach check that happens to "
    "have a Link in hand and grows a threshold later, in a hurry, to make a review queue "
    "shorter. So MemberReach takes a SourceRef and resolved_view calls it with link.member: "
    "the score is not in scope at the call site and cannot be consulted there. ResolvedView "
    "carries no confidence either, so nothing downstream can widen on one, because there is "
    "nothing there to read. Adding either would be a visible change to a signature, in a "
    "module whose docstring argues against it."
)

#: Why a view says nothing whatever about the members it withheld.
A_VIEW_CARRIES_NO_COUNT_OF_WHAT_IT_WITHHELD = (
    "A resolver that says 'this looks like the same client as one you cannot see' has "
    "disclosed that client, and so has one that says 'showing 2 of 5'. The subtraction is the "
    "whole disclosure, so the view carries no total, no member count and no truncation flag, "
    "and an entity the reader reaches nothing of returns None rather than an empty view. That "
    "None is the same value an id that was never issued returns, which is what makes DENIED "
    "and ABSENT indistinguishable here."
)

#: Why the view does not carry the canonical row's own provenance.
THE_VIEW_CARRIES_NOTHING_OFF_THE_CANONICAL_ROW_BUT_ITS_ID_AND_ITS_TYPE = (
    "created_from names the source record that first evidenced the entity, and created_by "
    "names whoever or whatever minted it. A reader who reaches some other member has no claim "
    "on either: handing over created_from names a record they may not read, which is the same "
    "disclosure the member filter exists to prevent, arriving through a field nobody thought "
    "of as data. The id and the type are carried because the id is what the caller asked with "
    "and the type is a fact about their own record."
)

#: What a merge writes, and what it therefore costs to undo.
A_MERGE_MOVES_ONE_POINTER_AND_NOTHING_ELSE = (
    "Merging writes merged_into on the entity that stops being current, and touches no alias, "
    "no identifier, no link and no source record. Those rows go on naming the entity they "
    "were attached to, and resolution follows the pointer to find the survivor, which is why "
    "an id issued before a merge still resolves after one. The rejected design rewrites the "
    "child rows to point at the survivor, which is faster to read and destroys the only copy "
    "of where each row came from, so an unmerge has nothing to restore from and the audit "
    "question 'what did this look like before' has no answer at all."
)

#: The gap this task group does not close, kept as a constant so it has to be deleted.
NOTHING_HERE_RECORDS_WHO_MERGED_OR_ON_WHAT_EVIDENCE = (
    "merged_at records when an entity stopped being current. Nothing in this module or in "
    "er.canonical records who decided it, what evidence they had, or what the membership "
    "looked like beforehand, and there is no merge audit row and no pre-image table. A merge "
    "is therefore reversible, because clearing one pointer restores the entity and nothing "
    "else was written, and it is not explainable. M14.5.1 asks for the pre-image and M14.5.4 "
    "for the full audit of who, when and on what evidence; neither is built, and this "
    "constant is here so that claiming otherwise requires deleting it."
)

#: Why an identifier row cannot hold the value it identifies.
AN_IDENTIFIER_IS_A_HASH_AND_THERE_IS_NOWHERE_TO_PUT_THE_VALUE = (
    "A join key is an email address, a phone number or a company registration number, which "
    "is to say a contact record broken into columns. Identifier carries a hex digest and has "
    "no field for the value, and er.identifier constrains the column to 64 hex characters so "
    "a raw address cannot be stored there even by a hand-written INSERT. Joining on a keyed "
    "digest costs nothing, because equality is the only operation the cascade performs on "
    "one; what it buys is that the identifier table is not a mailing list, and that a copy of "
    "it taken without the pepper joins to nothing."
)

# ---------------------------------------------------------------------------- vocabulary
_NAME_RE: Final = re.compile(OBJECT_NAME_PATTERN)

#: A sha256 hex digest: what `identifier_hash` returns and the only thing `Identifier`
#: accepts. Lowercase only, so one digest has one spelling and two rows for one join key
#: cannot differ by case.
_DIGEST_RE: Final = re.compile(r"^[0-9a-f]{64}$")

#: How many forwarding hops a resolution follows before calling the chain corrupt.
#:
#: Sixteen, and the figure is a corruption detector rather than a policy. A chain grows by one
#: hop only when a surviving entity is itself later merged, so a real history reaches two or
#: three; sixteen is generous against any of that and small enough that a cycle is found in
#: microseconds rather than looping. Exceeding it raises rather than returning the last id
#: reached, because a non-current id handed back as though it were current is worse than an
#: error: the caller stores it, and the next merge makes it wrong again in silence.
MAX_FORWARD_DEPTH: Final = 16

#: How long an observed name form may be.
#:
#: Two hundred, which is the width `brain.connectors.contract` bounds a source's own
#: identifier at and is generous against any real company, person or project name. The bound
#: is load-bearing rather than tidy: the observation is the key of `er.alias`, so the name is
#: in a btree index tuple, and an unbounded name column in a key fails on insert against a
#: real server at whatever hour the longest name in the estate arrives. Refused here as well
#: as in the column, so the failure is a `ResolutionError` in a test rather than a rejected
#: INSERT in a backfill.
MAX_ALIAS_CHARS: Final = 200


class EntityType(enum.StrEnum):
    """What kind of thing a canonical entity is.

    Three, matching the three the cascade is applied to: companies first, then people and
    projects (M14.7). The type is on the entity rather than inferred from the sources it was
    built from, because one source's records are not all one kind: a HubSpot export carries
    companies and contacts, and inferring the type from the connector name would make every
    contact a company.
    """

    COMPANY = "company"
    PERSON = "person"
    PROJECT = "project"


class IdentifierKind(enum.StrEnum):
    """The kinds of join key a resolution may be made on.

    A closed vocabulary rather than a free string, because the kind is hashed together with
    the value: see `identifier_hash`. A kind nobody declared would produce digests that join
    to nothing, which is a silent miss rather than a loud failure.
    """

    #: Singapore's Unique Entity Number. The strongest identifier available here.
    UEN = "uen"
    #: A tax registration number, in whatever jurisdiction. Not unique across them.
    TAX_ID = "tax_id"
    #: A web domain, which identifies a company only when it is not a free-mail one.
    DOMAIN = "domain"
    EMAIL = "email"
    PHONE = "phone"


class ResolutionError(Exception):
    """A statement about the resolution graph that cannot be true.

    Deliberately outside the `brain.core.errors` taxonomy, like `VisibilityError`. Those five
    outcomes describe an answer to a person; this describes a graph that has been corrupted or
    a row that must not be stored, and nobody asking a question ever sees it.
    """


# ------------------------------------------------------------------- what a member is
@dataclass(frozen=True)
class SourceRef:
    """Which record in which system: the same triple `proj.record` is keyed by.

    A frozen dataclass compared field by field, and deliberately never a joined string. A
    rendered key would need a separator, and `("a:b", "c", "d")` and `("a", "b:c", "d")` would
    then address the same member, which here is two different source records becoming one and
    a reader who reaches one being handed the other's name. `brain.ops.limit_store` escapes
    its separator for the same reason; a tuple has none to escape.

    Each of the three parts is required for the reason `brain.tables.projection` gives about
    its own key: Freshdesk company 42 and Xero contact 42 are different companies, and
    Freshdesk ticket 42 and Freshdesk company 42 both exist.
    """

    source: str
    entity: str
    source_id: str

    def __post_init__(self) -> None:
        if not _NAME_RE.match(self.source):
            msg = f"source {self.source!r} is not a connector name"
            raise ResolutionError(msg)
        if not _NAME_RE.match(self.entity):
            msg = f"entity {self.entity!r} is not an entity name"
            raise ResolutionError(msg)
        if not self.source_id.strip():
            msg = (
                "a member with no source id names no record; two such members compare equal "
                "and become one, and a reader reaching either would be handed both"
            )
            raise ResolutionError(msg)

    def sort_key(self) -> tuple[str, str, str]:
        """A total order over members, so a view's order carries no information.

        Sorted output rather than store order, for the reason `brain.core.redaction.redact`
        sorts its locks: the order rows come back in differs between callers and between
        queries, and an order that differs by reader is readable as a signal about what each
        of them was refused.
        """
        return (self.source, self.entity, self.source_id)


class MemberReach(Protocol):
    """Whether this reader independently reaches one source record.

    The signature is the guard. It is handed a `SourceRef` and nothing else: no confidence, no
    canonical id, no sibling member and no view under construction, so a reach decision cannot
    be made on the strength of a merge or of a score. See
    `A_SCORE_IS_EVIDENCE_AND_NEVER_A_PERMISSION`.

    A protocol rather than a concrete implementation because this module holds no redactor and
    no field policy, in the split `brain.ops.limits` and `brain.ops.limit_store` keep: the
    module that decides what a merge may show holds no way to read a record, and the module
    that reads records holds no merge policy. The real implementation asks whether
    `brain.core.redaction.compute_mask` leaves that record with any substance, which is the
    question "may this person see that this record exists at all".
    """

    def __call__(self, member: SourceRef) -> bool: ...


# ---------------------------------------------------------------- the four canonical rows
@dataclass(frozen=True)
class CanonicalEntity:
    """`er.canonical` (M14.1.1). An id, a type, and where it came from. No field values.

    See `A_CANONICAL_ENTITY_HOLDS_NO_FIELDS`. Adding a `name`, a `domain` or a `fields` mapping
    here is the regression this whole module is shaped against, and it is the change that would
    look most like an improvement.
    """

    entity_id: str
    entity_type: EntityType
    #: Created provenance, part one: when it was minted.
    created_at: datetime
    #: Created provenance, part two: the principal or the job that minted it. A string rather
    #: than a foreign key into `auth.principal`, because a backfill is not a person and an
    #: entity has to outlive the account of whoever ran one.
    created_by: str
    #: Created provenance, part three: the source record that first evidenced it. Never copied
    #: into a `ResolvedView`; see the constant.
    created_from: SourceRef
    #: The forwarding pointer (M14.1.5). Empty while this entity is the current one. Empty
    #: rather than None so the type has one absent value instead of two, which is the choice
    #: `brain.connectors.projection.ProjectedRecord.local_id` makes for the same reason.
    merged_into: str = ""
    #: When it stopped being current. Agrees with `merged_into`, or the entity is refused.
    merged_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.entity_id.strip():
            msg = "a canonical entity with no id cannot be pointed at or resolved"
            raise ResolutionError(msg)
        if not self.created_by.strip():
            msg = (
                "a canonical entity with no created_by has no provenance at all, and M14.1.1 "
                "asks for the type and the provenance together"
            )
            raise ResolutionError(msg)
        if self.created_at.tzinfo is None:
            msg = "a naive created_at compares wrongly against an aware one"
            raise ResolutionError(msg)
        if self.merged_into == self.entity_id:
            msg = (
                f"{self.entity_id!r} is merged into itself, which is a one-hop cycle: "
                "following the pointer never reaches a surviving entity"
            )
            raise ResolutionError(msg)
        if bool(self.merged_into) is not (self.merged_at is not None):
            msg = (
                "merged_into and merged_at have to agree. An entity merged at no time reads "
                "as current to anything filtering on the timestamp, and one merged into "
                "nothing at a time reads as merged to anything filtering on the pointer, and "
                "the two halves of the system then disagree about which entities exist"
            )
            raise ResolutionError(msg)

    @property
    def is_current(self) -> bool:
        """Whether this is the surviving entity rather than a forwarding stub."""
        return not self.merged_into


@dataclass(frozen=True)
class Alias:
    """`er.alias` (M14.1.2). One observed name form, with its source and when it was first seen.

    Every form is kept, including the ones that differ only in punctuation, because the
    normalisation that collapses them is M14.2's and a table holding only normalised forms
    cannot be re-normalised when that changes. The observed form is what the source actually
    said, and it is the evidence a human reviewer is shown.

    The alias carries its own `source` rather than inheriting the entity's, and that is what
    makes a merged view filterable: after a merge an entity's aliases come from several
    records, and which of them a reader may see is decided per row.
    """

    entity_id: str
    #: The observed form, verbatim. Not normalised, not folded, not trimmed of its suffix.
    name: str
    source: SourceRef
    first_seen_at: datetime

    def __post_init__(self) -> None:
        if not self.entity_id.strip():
            msg = "an alias naming no entity is a name attached to nothing"
            raise ResolutionError(msg)
        if not self.name.strip():
            msg = (
                "an alias with a blank name is not an observed form; it matches every "
                "blank-name row in the estate and joins them"
            )
            raise ResolutionError(msg)
        if len(self.name) > MAX_ALIAS_CHARS:
            msg = (
                f"an observed name of {len(self.name)} characters is longer than the "
                f"{MAX_ALIAS_CHARS} er.alias can key on; refused here so it fails in a test "
                "rather than as a rejected INSERT in a backfill"
            )
            raise ResolutionError(msg)
        if self.first_seen_at.tzinfo is None:
            msg = "a naive first_seen_at compares wrongly against an aware one"
            raise ResolutionError(msg)


@dataclass(frozen=True)
class Identifier:
    """`er.identifier` (M14.1.3). A hashed join key, its kind, and the record that asserted it.

    There is no field for the value. See
    `AN_IDENTIFIER_IS_A_HASH_AND_THERE_IS_NOWHERE_TO_PUT_THE_VALUE`.
    """

    entity_id: str
    kind: IdentifierKind
    #: The output of `identifier_hash`. Sixty-four lowercase hex characters, checked here and
    #: again in the column, so a raw email cannot be stored in this field by any route.
    key_hash: str
    source: SourceRef
    first_seen_at: datetime

    def __post_init__(self) -> None:
        if not self.entity_id.strip():
            msg = "an identifier naming no entity is a join key attached to nothing"
            raise ResolutionError(msg)
        if not _DIGEST_RE.match(self.key_hash):
            msg = (
                f"key_hash {self.key_hash!r} is not a sha256 digest. This field is the whole "
                "of the privacy rule for join keys: a value that is not a digest is a "
                "contact detail stored in a table designed never to hold one"
            )
            raise ResolutionError(msg)
        if self.first_seen_at.tzinfo is None:
            msg = "a naive first_seen_at compares wrongly against an aware one"
            raise ResolutionError(msg)


@dataclass(frozen=True)
class Link:
    """`er.link` (M14.1.4). One source record mapped to one canonical entity, with confidence.

    The confidence is the whole of what the cascade decided and it lives only here. It reaches
    a human reviewer through M14.6's queue, and it reaches nothing that decides reach: see
    `A_SCORE_IS_EVIDENCE_AND_NEVER_A_PERMISSION` and `member` below.

    One link per source record, which the table enforces by keying on the source triple alone.
    Two links would be two answers to "what is this record part of", and whichever a query
    happened to read first would be the answer.
    """

    entity_id: str
    source: SourceRef
    #: What the cascade believed. A share, refused outside it: a figure above one is not a
    #: probability, and a negative one is a belief the cascade has no way to express.
    confidence: float
    linked_at: datetime

    def __post_init__(self) -> None:
        if not self.entity_id.strip():
            msg = "a link naming no entity maps a record to nothing"
            raise ResolutionError(msg)
        if not 0.0 <= self.confidence <= 1.0:
            msg = (
                f"confidence {self.confidence} is not a share; a match score outside nought "
                "to one is not a probability and nothing calibrated produces one"
            )
            raise ResolutionError(msg)
        if self.linked_at.tzinfo is None:
            msg = "a naive linked_at compares wrongly against an aware one"
            raise ResolutionError(msg)

    @property
    def member(self) -> SourceRef:
        """The part of this link a reach decision is allowed to see.

        Named rather than left as `.source` at the call site so that the stripping is the
        visible thing: `reaches(link.member)` says that the score was dropped on purpose,
        where `reaches(link.source)` reads as an accident of which attribute was handy.
        """
        return self.source


# --------------------------------------------------------------------- the hashed join key
def identifier_hash(kind: IdentifierKind, value: str, *, pepper: str) -> str:
    """The join key for one identifier: HMAC-SHA256 over the kind and the value.

    **Keyed rather than plain, and the key is not optional.** A bare sha256 of an email
    address is reversible by anybody with a list of addresses, which is everybody, so a plain
    digest would make `er.identifier` a mailing list that merely looks encrypted. The pepper
    is a deployment secret passed in rather than read here, for the reason this module reads
    no clock: a module that fetched its own secret could not be tested for the case where
    there is not one, and that is the case that is always wrong.

    **The kind is hashed with the value, not merely stored beside it.** Without it a phone
    number and a tax id that happen to be the same string produce the same digest, and stage
    one of the cascade joins two entities on a coincidence of digits. Storing the kind in its
    own column and hashing the value alone would leave that join available to any query that
    forgot to filter on the kind, which is the sort of clause a later index change drops.

    **Nothing is normalised here.** Case folding, whitespace collapse and suffix stripping are
    M14.2's and are deliberately not duplicated: two implementations of normalisation produce
    two sets of digests, and the day they disagree the entities stop joining with nothing
    reporting it. A caller that hashes an unnormalised value gets a key that matches nothing,
    which is a miss rather than a false join, and a miss is the safe direction.
    """
    if not pepper:
        msg = (
            "identifier_hash needs a pepper. Without one this is a plain digest, and a plain "
            "digest of an email address or a phone number is reversible by anybody holding a "
            "list of them, which turns er.identifier into the contact table it exists not to "
            "be"
        )
        raise ResolutionError(msg)
    if not value.strip():
        msg = (
            "a blank identifier value hashes to a single digest shared by every blank one in "
            "the estate, which joins every entity that has one to every other"
        )
        raise ResolutionError(msg)
    material = f"{kind.value}\n{value}".encode()
    return hmac.new(pepper.encode(), material, sha256).hexdigest()


# ----------------------------------------------------------- the forwarding pointer (M14.1.5)
def current_id(
    entity_id: str,
    entities: Mapping[str, CanonicalEntity],
    *,
    max_depth: int = MAX_FORWARD_DEPTH,
) -> str:
    """The surviving entity an issued id now resolves to.

    **Resolution runs over the whole graph, before anything is filtered for a reader**, and
    the ordering is load-bearing rather than incidental. Resolving over only the entities a
    reader can see would give two readers two different survivors for one id, and an id whose
    meaning depends on who is holding it is not an id. The reader's reach is applied
    afterwards, by `resolved_view`, to the rows rather than to the graph.

    Raises rather than returning a partial answer on a chain that is broken, a chain that
    cycles and a chain that is longer than `max_depth`. All three are corruption: a pointer
    into nothing cannot happen through `er.canonical`, whose foreign key refuses it, so
    meeting one means a row arrived some other way and the honest report is an error rather
    than the last id that happened to exist.
    """
    if entity_id not in entities:
        msg = (
            f"{entity_id!r} is not an entity in this graph, so nothing can be said about what "
            "it resolves to"
        )
        raise ResolutionError(msg)
    seen = {entity_id}
    current = entities[entity_id]
    while current.merged_into:
        nxt = current.merged_into
        if nxt in seen:
            msg = (
                f"the forwarding chain from {entity_id!r} returns to {nxt!r}; a cycle has no "
                "surviving entity in it, so every id on it resolves to nothing"
            )
            raise ResolutionError(msg)
        if len(seen) > max_depth:
            # Strictly greater, so `max_depth` hops are followed and the next is refused.
            # `er.resolved_alias` bounds its recursion at the same figure with `depth <
            # MAX_FORWARD_DEPTH`, which admits depths one to sixteen; an off-by-one between
            # the two would mean the view forwarding a chain this function calls corrupt.
            msg = (
                f"the forwarding chain from {entity_id!r} is longer than {max_depth} hops; a "
                "chain that long is a merge loop rather than a history"
            )
            raise ResolutionError(msg)
        if nxt not in entities:
            msg = (
                f"{current.entity_id!r} is merged into {nxt!r}, which is not an entity in "
                "this graph; er.canonical's foreign key refuses such a row, so this one "
                "arrived another way"
            )
            raise ResolutionError(msg)
        seen.add(nxt)
        current = entities[nxt]
    return current.entity_id


def _survivor(
    entity_id: str, entities: Mapping[str, CanonicalEntity], *, max_depth: int
) -> str | None:
    """`current_id`, answering None where it would raise.

    The two exist because the database view and this module answer a broken chain differently
    on purpose. `er.resolved_alias` anchors on entities that are not merged and walks
    backwards, so a cycle is simply unreachable from the anchor and contributes no rows: a
    view that raised would take out every query over every healthy entity because one pair of
    rows is corrupt. This module raises when a caller asks about one id, because that caller
    asked a question with no true answer. Omission is the safe direction for a listing and an
    error is the safe direction for a lookup, and both are silent about anything a reader may
    not see.
    """
    try:
        return current_id(entity_id, entities, max_depth=max_depth)
    except ResolutionError:
        return None


def family_of(
    entity_id: str,
    entities: Mapping[str, CanonicalEntity],
    *,
    max_depth: int = MAX_FORWARD_DEPTH,
) -> frozenset[str]:
    """Every id that now resolves to the same surviving entity as `entity_id`.

    The set a merged entity's rows are gathered by. It has to be a set of ids rather than the
    survivor alone, because a merge rewrites nothing: an alias written before the merge still
    names the entity it was written against, and looking only for rows naming the survivor
    would lose every row belonging to the entity that was merged away. See
    `A_MERGE_MOVES_ONE_POINTER_AND_NOTHING_ELSE`.

    Linear in the size of the graph, which is the cost of the pointer not having been
    compressed. `er.resolved_alias` does the same walk in one recursive pass in the database;
    this is the in-memory equivalent and is called with whatever set of entities the caller
    loaded.
    """
    survivor = current_id(entity_id, entities, max_depth=max_depth)
    return frozenset(
        candidate
        for candidate in entities
        if _survivor(candidate, entities, max_depth=max_depth) == survivor
    )


# ---------------------------------------------------------------- what a reader is handed
@dataclass(frozen=True)
class ResolvedView:
    """One entity as one reader may see it: their own rows, and nothing about anybody else's.

    Every field is either the reader's own reachable rows or one of the two facts off the
    canonical row that are safe to carry. There is no count, no total and no truncation flag;
    see `A_VIEW_CARRIES_NO_COUNT_OF_WHAT_IT_WITHHELD`. There is no confidence; see
    `A_SCORE_IS_EVIDENCE_AND_NEVER_A_PERMISSION`. There is no provenance; see
    `THE_VIEW_CARRIES_NOTHING_OFF_THE_CANONICAL_ROW_BUT_ITS_ID_AND_ITS_TYPE`.
    """

    #: The surviving id, which is what an id issued before a merge now resolves to.
    entity_id: str
    entity_type: EntityType
    #: The source records this reader reaches. `SourceRef` rather than `Link`, so the score
    #: does not travel.
    members: tuple[SourceRef, ...]
    aliases: tuple[Alias, ...] = ()
    identifiers: tuple[Identifier, ...] = ()


def resolved_view(
    entity_id: str,
    *,
    entities: Mapping[str, CanonicalEntity],
    links: Sequence[Link],
    aliases: Sequence[Alias] = (),
    identifiers: Sequence[Identifier] = (),
    reaches: MemberReach,
    max_depth: int = MAX_FORWARD_DEPTH,
) -> ResolvedView | None:
    """What one reader may see of one entity, after any merges (M14.1.5, M14.1.6).

    None means "nothing to show you", and it is returned for an id that was never issued, an
    id whose chain is corrupt and an id whose every member is out of reach. Those cases are
    deliberately indistinguishable: a caller able to tell them apart could enumerate which
    entities exist by asking about ids until the answer changed shape.

    The order is deliberate. The graph is resolved first, over every entity supplied, so the
    surviving id is the same for everybody. The reader's reach is applied second, to the rows,
    one source record at a time. Doing it the other way round would make the survivor a
    function of who is asking.

    `reaches` is called for every candidate row unconditionally, and its answer is the only
    thing consulted. Nothing here reads a confidence, tests a threshold or admits a row
    because a sibling was admitted.
    """
    if entity_id not in entities:
        return None
    survivor = _survivor(entity_id, entities, max_depth=max_depth)
    if survivor is None:
        return None
    family = family_of(entity_id, entities, max_depth=max_depth)

    members = sorted(
        {link.member for link in links if link.entity_id in family and reaches(link.member)},
        key=SourceRef.sort_key,
    )
    if not members:
        # Nothing to show, said the same way an id that does not exist is said. Not an empty
        # view: an empty view with a real entity id in it is the statement "this exists and
        # you may not see it", which is the disclosure this whole module refuses.
        return None

    kept_aliases = sorted(
        (one for one in aliases if one.entity_id in family and reaches(one.source)),
        key=lambda one: (one.name, one.source.sort_key(), one.first_seen_at),
    )
    kept_identifiers = sorted(
        (one for one in identifiers if one.entity_id in family and reaches(one.source)),
        key=lambda one: (one.kind.value, one.key_hash, one.source.sort_key()),
    )
    return ResolvedView(
        entity_id=survivor,
        entity_type=entities[survivor].entity_type,
        members=tuple(members),
        aliases=tuple(kept_aliases),
        identifiers=tuple(kept_identifiers),
    )


# ------------------------------------------------------- the resolved alias view (M14.1.6)
@dataclass(frozen=True)
class ResolvedAlias:
    """One observed name, and the entity it now belongs to after any merges.

    The Python half of `er.resolved_alias`. Two halves rather than one because the database
    needs the join to answer a query and this module needs it to answer without a database,
    and `tests/unit/test_resolution_tables.py` holds the view's SQL against the properties
    this function has.
    """

    alias: Alias
    #: The surviving entity. Not necessarily `alias.entity_id`, which is the entity the name
    #: was observed against and is never rewritten.
    entity_id: str


def resolved_aliases(
    *,
    entities: Mapping[str, CanonicalEntity],
    aliases: Sequence[Alias],
    reaches: MemberReach,
    max_depth: int = MAX_FORWARD_DEPTH,
) -> tuple[ResolvedAlias, ...]:
    """Every alias this reader may see, against the entity that survives for it.

    Filtered by the same predicate `resolved_view` uses and on the same terms: a name observed
    on a record the reader cannot see is a fact about that record. A listing is where this
    leaks most quietly, because the natural implementation resolves first and filters never.

    An alias whose entity has no surviving entity - a cycle, a chain that is too long, a
    pointer into nothing - is omitted rather than raised on, matching the database view, whose
    anchor is the set of entities that are not merged and which therefore cannot reach a cycle
    at all. Omission tells the reader nothing, which is the only property that matters here:
    the count is not reported, so nothing can be subtracted from it.
    """
    resolved: list[ResolvedAlias] = []
    for one in aliases:
        if not reaches(one.source):
            continue
        survivor = _survivor(one.entity_id, entities, max_depth=max_depth)
        if survivor is None:
            continue
        resolved.append(ResolvedAlias(alias=one, entity_id=survivor))
    return tuple(
        sorted(
            resolved, key=lambda row: (row.entity_id, row.alias.name, row.alias.source.sort_key())
        )
    )
