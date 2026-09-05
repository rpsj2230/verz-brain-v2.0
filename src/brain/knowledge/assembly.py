"""Turning what retrieval returned into what a person is shown, and adding nothing to it.

Two jobs here and they run in one direction. **A chunk is what was retrieved; a document is
what a person cites.** The first half of this module arranges retrieved chunks into the two
shapes an answer needs, and the second half decides when two results from two different
planes are one fact. Both are pure functions over results the caller has already been
admitted to see, and neither can fetch anything: there is no session, no `RowSource` and no
parameter through which a corpus could arrive.

---

**A document-level result is an arrangement of the passages that came back, and it asserts
nothing else about the document.** This is the widening the leaf names, and it arrives in
three disguises.

*Neighbour expansion.* Having found chunk seven, show six and eight for context. Both are
chunks of the same document, so the permissions look identical, and they are: `chunk_document`
copies one `DocumentPermissions` onto every chunk of a document and compares them at the end.
What differs is retirement. Re-chunking a document retires its previous chunks, and
`reach_predicate` excludes `deleted_at IS NOT NULL` for exactly that reason. Reaching for a
neighbour by ordinal, outside the query that carried the predicate, is how a retired passage
comes back with text the document no longer contains. It is the post-filter of
`brain.knowledge.search`, moved one layer later and dressed as a feature.

*The document itself.* A citation that opens the file is a claim that the reader may have the
whole document. Reaching a passage is evidence about the permissions copied onto that chunk
at the last indexing run, not a decision anybody made about the document as it stands now: a
visibility narrowed after indexing leaves the old permissions on the chunks until they are
rebuilt. So the assembled result carries the reference and never a link, and whether the
reader may open the file is a fresh question for whatever serves files.

*A total.* "Showing two of nine passages" is a count of hidden things, and so is any figure
it could be subtracted from. `DocumentResult` therefore has no field a total could live in,
which is the same enforcement `brain.knowledge.fusion.Ranking` uses against a score: not a
rule somebody has to remember, a shape with nowhere to put the mistake.

Rejected: making `by_document` take the store, so that a document with only one retrieved
passage could be filled out for context. It is one parameter, it reads as an improvement in
recall, and it moves the permission decision from the query into this file.

---

**Deduplication across planes is the one with a disclosure in it, and the rule is that a
result only ever loses its place to another result the same caller is already holding.**

The row plane and the document plane can return the same underlying fact: a client's contract
value as a projected field, and the same figure inside a passage. The danger is not that the
list is too long. It is that a reader shown both counts them as two sources agreeing, which is
the false corroboration `Ranking` refuses when one retriever votes twice for one reference.

The cheap design is a fact index: decide once, at ingest, that this row and this passage state
the same thing, store a canonical group, and at query time keep the group's preferred
representative. **It is computed without a caller, so the representative can be a thing this
caller cannot see, and then the whole group disappears for them.** They lose a fact they were
entitled to, and the cause is an object they may not know exists. The same failure arrives more
quietly through a cache keyed on the question and not on the entitlement, which is why
`EntitlementSet.ent_hash` exists.

So the operation here takes one sequence, the caller's own admitted results, and returns
groups over exactly that sequence. There is no second parameter, and therefore nowhere an
invisible thing could be passed. Three properties follow and all three are asserted:

- every group member came from the input, so nothing is invented;
- every input appears in exactly one group, so nothing is lost;
- the representative is a member of its own group, so the reason a result is not shown first
  is always another result the reader is holding.

**Nothing is dropped.** A group keeps every member and reports which planes found it. Dropping
is what loses a fact: a row carrying three fields and a passage mentioning one of them are not
interchangeable, and a function that chose between them would be a field policy with no field
policy in it. A renderer that can show only one thing per result shows `kept`; one that can
show more, can.

**A candidate that states no comparable claim is its own group and is never merged.** Failing
to notice a duplicate shows one fact twice, which a reader sees and discounts. Merging two
facts that were not the same removes one of them silently, and nobody files a bug against a
result that merely never appeared.

**The two halves compose in one direction: assemble first, deduplicate second.** Deduplicating
chunk-level results would compare a passage against a record before the passage's own document
has been made whole, so one document would be compared several times and could keep one of its
passages while dropping another.

**This module does not rank and has no way to.** Groups come back in the order of their
earliest member in the input. A position in the row plane and a position in the document plane
are not on one scale, and `brain.knowledge.fusion` is the argument: any function that compared
them would be comparing a length against a temperature. The one place a plane is preferred is
the choice of representative, and that is a preference between two things the caller holds
rather than an ordering of the list.

Nothing here reads a clock, opens a connection or evaluates a permission. It rearranges lists.

Task ids: M15.3.2, M15.3.3
"""

from __future__ import annotations

import enum
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, assert_never

from brain.knowledge.item import ITEM_ID_PATTERN


class AssemblyError(Exception):
    """A result set that would be assembled wrongly rather than answered wrongly.

    Outside the `brain.core.errors` taxonomy, like every other refusal in this package:
    those five outcomes describe an answer given to a person, and this describes a refusal
    to build one.
    """


# ------------------------------------------------------------- named reasons


#: The rule the deduplication half is arranged around. A constant rather than a comment, for
#: the reason `BOTH_LIMITS_APPLY` is one: the sentence is what survives the person who wrote
#: it, and this is the sentence a reviewer has to be able to point at.
A_DUPLICATE_ONLY_LOSES_ITS_PLACE_TO_ONE_THE_CALLER_CAN_SEE: Final = (
    "deduplication is a function of one caller's own admitted results and of nothing else, so "
    "a result loses its place only to another result in that same list and the reason it is "
    "not first is always something the reader is holding; a duplicate relation computed "
    "without a caller, at index time or in a cache keyed on the question, can nominate a "
    "representative this caller may not see, and then the group disappears for them, which "
    "removes a fact they were entitled to for a reason they may not know exists"
)

#: Why a group keeps everything it contains.
DEDUPLICATION_NEVER_WITHHOLDS: Final = (
    "a group keeps every member and nothing leaves, because the harm deduplication exists to "
    "prevent is one fact read as two sources agreeing rather than a list that is too long; "
    "dropping is what loses a fact, since a row carrying three fields and a passage "
    "mentioning one of them are not interchangeable, and a function that chose between them "
    "would be a field policy with no field policy in it"
)

#: Which way the merge is allowed to be wrong.
UNDER_DEDUPLICATION_IS_THE_SAFE_FAILURE: Final = (
    "a candidate stating no comparable claim is its own group and is never merged; failing to "
    "notice a duplicate shows one fact twice, which a reader sees and discounts, while merging "
    "two facts that were not the same removes one of them silently, and nobody files a bug "
    "against a result that merely never appeared"
)

#: The widening the document half is written against.
TWO_PASSAGES_ARE_NOT_A_DOCUMENT: Final = (
    "a document-level result is an arrangement of the passages that came back and asserts "
    "nothing else: no body, no link and no total; reaching a passage is evidence about the "
    "permissions copied onto that chunk at the last indexing run rather than a decision about "
    "the document as it stands now, so whether the reader may open the file is a fresh "
    "question for whatever serves files"
)

#: Why this module fetches nothing, stated so that adding a source parameter has to argue
#: with a sentence rather than with a habit.
A_DOCUMENT_RESULT_IS_A_FUNCTION_OF_THE_PASSAGES_RETURNED: Final = (
    "everything on a document-level result is computed from the passages retrieval returned "
    "and nothing on it is computed from the passages it did not, which is why there is no "
    "parameter here through which a corpus could arrive; 'showing two of nine passages' is a "
    "count of hidden things, and so is any total it could be subtracted from"
)


# ------------------------------------------------------------- the chunk level


#: The reference grammar, compiled once. Imported rather than restated so that a citation
#: this module builds resolves against the same alphabet `brain.knowledge.item` issues.
_REFERENCE_RE: Final = re.compile(ITEM_ID_PATTERN)


@dataclass(frozen=True)
class RetrievedChunk:
    """One passage as retrieval returned it: the chunk level (M15.3.2).

    **There is no scope, no owner, no visibility and no state on this type, and no room for
    one.** By the time a chunk reaches assembly the reach predicate has already run inside
    the query, so a permission field here would be a second copy of a decision that was made
    correctly somewhere else, and a second copy is a second thing to evaluate. The type that
    does carry permissions is `brain.knowledge.chunking.Chunk`, which cannot be built outside
    `chunk_document`; this one is what came back, which is a different object with a different
    life.

    `title` is the document's title as the chunk carries it, copied at indexing time. It is
    read here rather than fetched, which is the whole reason the column exists on `know.chunk`.
    """

    chunk_id: str
    document_id: str
    ordinal: int
    text: str
    title: str = ""
    section: str = ""
    page: int | None = None

    def __post_init__(self) -> None:
        for name in ("chunk_id", "document_id"):
            value = str(getattr(self, name))
            if not _REFERENCE_RE.match(value):
                msg = (
                    f"{name} {value!r} is not a reference a citation can hold; an "
                    "unresolvable citation is a citation nobody checks"
                )
                raise AssemblyError(msg)
        if self.ordinal < 0:
            msg = f"ordinal {self.ordinal} is not a position in a document"
            raise AssemblyError(msg)


def by_chunk(
    order: Sequence[str], bodies: Mapping[str, RetrievedChunk]
) -> tuple[RetrievedChunk, ...]:
    """The chunk-level result: the references retrieval returned, in the order it returned
    them, with their text attached (M15.3.2).

    **The order decides membership and the mapping only supplies text.** That asymmetry is
    the safety property and it is worth the extra argument: a caller who passes a wider
    mapping than the order, a whole page cache say, gets nothing extra back, because a
    reference absent from the order was never returned to this caller by a query carrying
    their reach predicate. The reverse arrangement, iterating the mapping and looking each
    entry up in the order, reads identically in a diff and admits everything the mapping
    happens to hold.

    A reference with no body is skipped rather than refused. The two are fetched separately
    in any real deployment, so a body that has not arrived is an ordinary race, and turning
    it into an exception would turn a slightly shorter answer into no answer at all.

    A repeated reference is refused, for the reason `Ranking.__post_init__` refuses one: it
    arrives from a query that joined and forgot to be distinct, it looks like nothing in a
    diff, and here it would put one passage in the answer twice and make one document look
    like two matches.
    """
    repeated = sorted({ref for ref in order if order.count(ref) > 1})
    if repeated:
        msg = (
            f"the retrieval order carries {repeated} more than once; one passage listed twice "
            "is one document that looks like two matches"
        )
        raise AssemblyError(msg)
    return tuple(bodies[ref] for ref in order if ref in bodies)


# ---------------------------------------------------------- the document level


@dataclass(frozen=True)
class DocumentResult:
    """One document, as an arrangement of the passages that came back (M15.3.2).

    **Read the absences.** There is no `body`, because assembling one would mean fetching
    what retrieval did not return. There is no `url` or `link`, because whether this reader
    may open the file is decided against the document's current permissions by whatever
    serves files, and this result is built from permissions copied onto chunks at indexing
    time. There is no `passage_total` and no `omitted`, because both are counts of hidden
    things and the second is the first by subtraction. See `TWO_PASSAGES_ARE_NOT_A_DOCUMENT`.

    `position` is where this document's best passage sat in the chunk-level list, one-based.
    Best rather than summed: summing a document's passages rewards a long document with many
    mediocre ones, which is the standard failure of document-level aggregation and it looks
    like better recall from the outside.
    """

    document_id: str
    title: str
    passages: tuple[RetrievedChunk, ...]
    position: int

    def __post_init__(self) -> None:
        if not self.passages:
            msg = (
                f"{self.document_id!r} has no passages; a document result with nothing in it "
                "is an assertion that the document exists, which is the one thing a result "
                "assembled from passages is not entitled to make"
            )
            raise AssemblyError(msg)
        if self.position < 1:
            msg = f"position {self.position} is not a one-based place in the result list"
            raise AssemblyError(msg)
        foreign = sorted({p.document_id for p in self.passages} - {self.document_id})
        if foreign:
            msg = (
                f"{self.document_id!r} was assembled with passages from {foreign}; a document "
                "result citing another document's text is a citation that cannot be checked"
            )
            raise AssemblyError(msg)


def by_document(chunks: Sequence[RetrievedChunk]) -> tuple[DocumentResult, ...]:
    """The document-level result: the same passages, grouped into what a person cites
    (M15.3.2).

    One parameter, and that is the enforcement rather than an accident of the signature. A
    second one carrying a store or a corpus is how neighbour expansion and a passage total
    both arrive, and both are widenings the module docstring argues against at length. See
    `A_DOCUMENT_RESULT_IS_A_FUNCTION_OF_THE_PASSAGES_RETURNED`.

    Documents come back in the order of their best passage, so the ordering retrieval decided
    survives the grouping. Passages inside a document are in ordinal order instead, because
    that is reading order and a person quoting two passages of one document wants them the way
    round the document has them.

    The title is taken from the earliest passage that carries one. Deterministic, and the
    alternative is worse in both directions: refusing when two passages disagree turns an
    indexing race into a failed answer, and taking the best-ranked passage's title makes the
    document's name a function of the question.
    """
    ordered: dict[str, list[RetrievedChunk]] = {}
    position_of: dict[str, int] = {}
    for place, chunk in enumerate(chunks, start=1):
        ordered.setdefault(chunk.document_id, []).append(chunk)
        position_of.setdefault(chunk.document_id, place)
    results: list[DocumentResult] = []
    for document_id, passages in ordered.items():
        in_reading_order = tuple(sorted(passages, key=lambda c: (c.ordinal, c.chunk_id)))
        titled = next((c.title for c in in_reading_order if c.title), "")
        results.append(
            DocumentResult(
                document_id=document_id,
                title=titled,
                passages=in_reading_order,
                position=position_of[document_id],
            )
        )
    return tuple(sorted(results, key=lambda d: d.position))


# ------------------------------------------------ deduplication across planes


class Plane(enum.StrEnum):
    """Where a result came from.

    Two members, and a third would be a type error at `_plane_preference` rather than a
    silent tie-break, which is the treatment `brain.knowledge.search._level_branch` gives
    `Visibility` and for the same reason.
    """

    #: `brain.knowledge.rows`: a projected business record, read through a typed tool.
    ROW = "row"
    #: `brain.knowledge.search`: a passage, or a document assembled from passages.
    DOCUMENT = "document"


@dataclass(frozen=True)
class Claim:
    """What a result states, in the only form two planes can be compared in.

    **A claim is derived from the result the caller received and never looked up.** For a row
    it is the projected field and its value, which the caller is holding. For a passage it is
    whatever extraction read out of text the caller is also holding. Neither is a lookup in a
    shared fact index, and that is the whole disclosure argument: an index is computed without
    a caller, so it can speak for a document this caller cannot see. See
    `A_DUPLICATE_ONLY_LOSES_ITS_PLACE_TO_ONE_THE_CALLER_CAN_SEE`.

    The key is a tuple rather than a joined string. A separator is a character that eventually
    appears inside a value, and the day it does, two claims that are not the same collide on
    it and one of them is quietly merged away.
    """

    entity: str
    record_id: str
    field: str
    value: str

    def __post_init__(self) -> None:
        for name in ("entity", "record_id", "field", "value"):
            if not str(getattr(self, name)).strip():
                msg = (
                    f"a claim with an empty {name} is not a statement about anything, and two "
                    "such claims would collide on their emptiness and be merged"
                )
                raise AssemblyError(msg)

    @property
    def key(self) -> tuple[str, str, str, str]:
        """The comparable form: whitespace collapsed and case folded, nothing else.

        Deliberately not a semantic comparison. "120000" and "120,000" stay different claims,
        and that is `UNDER_DEDUPLICATION_IS_THE_SAFE_FAILURE` chosen on purpose: a normaliser
        clever enough to see through formatting is clever enough to merge two figures that
        differ, and the merged one is gone without trace.
        """
        return (
            _folded(self.entity),
            _folded(self.record_id),
            _folded(self.field),
            _folded(self.value),
        )


def _folded(text: str) -> str:
    return " ".join(text.split()).casefold()


@dataclass(frozen=True)
class Candidate:
    """One result the caller has already been admitted to see, ready to be grouped.

    It carries what deduplication needs in order to decide and to point back, and nothing it
    would need in order to render. A candidate holding the text would be a candidate a future
    change could withhold, and this module has no business withholding anything: the
    projection and the reach predicate are where that decision was made, correctly, before
    any of these existed.

    `claim` is optional and None is the ordinary case for a passage nothing has linked to a
    record. A candidate with no claim is its own group for ever.
    """

    plane: Plane
    ref: str
    position: int
    claim: Claim | None = None

    def __post_init__(self) -> None:
        if not _REFERENCE_RE.match(self.ref):
            msg = f"candidate reference {self.ref!r} is not a reference a citation can hold"
            raise AssemblyError(msg)
        if self.position < 1:
            msg = f"position {self.position} is not a one-based place in its own plane's list"
            raise AssemblyError(msg)


@dataclass(frozen=True)
class DuplicateGroup:
    """Results the caller is holding that state one fact (M15.3.3).

    `kept` is what a renderer able to show one thing per result shows, and `also` is
    everything else the group contains. Nothing is discarded: see
    `DEDUPLICATION_NEVER_WITHHOLDS`. A group of one is the ordinary case and carries an empty
    `also`.

    The constructor checks what `deduplicate` guarantees, because the routing check is the one
    a refactor drops and the constructor is the one a hand-built group goes around. That is
    the two-refusal pattern `brain.ops.denial_alerts` uses for its subject rule.
    """

    kept: Candidate
    also: tuple[Candidate, ...] = ()

    def __post_init__(self) -> None:
        refs = [(m.plane, m.ref) for m in self.members]
        if len(set(refs)) != len(refs):
            msg = (
                f"{self.kept.ref!r} appears in its own group more than once; one result "
                "counted twice is corroboration that never happened"
            )
            raise AssemblyError(msg)
        if not self.also:
            # A group of one asserts nothing about sameness, so there is nothing to check.
            return
        if any(m.claim is None for m in self.members):
            msg = (
                f"{self.kept.ref!r} was grouped with a result stating no comparable claim; "
                f"{UNDER_DEDUPLICATION_IS_THE_SAFE_FAILURE}"
            )
            raise AssemblyError(msg)
        keys = {m.claim.key for m in self.members if m.claim is not None}
        if len(keys) > 1:
            msg = (
                f"{self.kept.ref!r} was grouped with results stating something else; a group "
                "is the assertion that its members say one thing, and a wrong one merges two "
                "facts into one and loses the second without trace"
            )
            raise AssemblyError(msg)

    @property
    def members(self) -> tuple[Candidate, ...]:
        """Everything in the group, the representative first."""
        return (self.kept, *self.also)

    @property
    def planes(self) -> tuple[str, ...]:
        """Which planes found this fact, in name order.

        A diagnostic about our own retrieval rather than a fact about the corpus, exactly as
        `Ranking.retriever` is: it names which of our own queries matched, for results the
        caller was already entitled to hold.
        """
        return tuple(sorted({m.plane.value for m in self.members}))

    @property
    def corroborated(self) -> bool:
        """True when more than one plane found it, which is the thing a reader may rely on."""
        return len(self.planes) > 1


def _plane_preference(plane: Plane) -> int:
    """Lower is preferred. The row plane represents a group whenever it is in one.

    Two reasons and the second is the load-bearing one. A projected row is the record; a
    passage is prose about the record, written at some point in the past and true then. And
    the row plane's projection is the finer-grained of the two permission decisions, since it
    is compiled per column from capabilities, so preferring it never puts a value in front of
    a reader through the coarser of the two gates that govern it.

    `assert_never` on the tail, so a third plane is a type error rather than a plane that
    silently sorts last.
    """
    match plane:
        case Plane.ROW:
            return 0
        case Plane.DOCUMENT:
            return 1
    assert_never(plane)


def deduplicate(candidates: Sequence[Candidate]) -> tuple[DuplicateGroup, ...]:
    """Group one caller's own results by the fact they state (M15.3.3).

    **One parameter, and that is the guarantee.** There is nowhere to pass a corpus, a shared
    duplicate index, another caller's results or a cache, so there is nothing outside this
    list that can decide what is in it. Every representative is a member of its own group and
    every group is drawn from the argument, which is
    `A_DUPLICATE_ONLY_LOSES_ITS_PLACE_TO_ONE_THE_CALLER_CAN_SEE` expressed as a signature
    rather than as a rule somebody has to keep.

    Groups come back in the order of their earliest member. This function does not rank and
    must not: a position in the row plane and a position in the document plane are not on one
    scale, and `brain.knowledge.fusion` is the argument for why combining them by value is
    comparing a length against a temperature.

    Within a group the representative is the row-plane member if there is one, then the
    earlier position, then the reference. The position comparison only ever runs inside one
    plane, because the plane preference is settled first.

    A repeated `(plane, ref)` is refused. One result listed twice would become its own
    corroboration, which is the same fake agreement `Ranking` refuses inside one list.
    """
    listed = [(c.plane.value, c.ref) for c in candidates]
    repeated = sorted(
        f"{plane}:{ref}" for plane, ref in set(listed) if listed.count((plane, ref)) > 1
    )
    if repeated:
        msg = (
            f"the candidate list carries {repeated} more than once; one result counted twice "
            "corroborates itself"
        )
        raise AssemblyError(msg)

    grouped: dict[tuple[str, str, str, str], list[Candidate]] = {}
    singletons: list[tuple[int, Candidate]] = []
    first_seen: dict[tuple[str, str, str, str], int] = {}
    for place, candidate in enumerate(candidates):
        if candidate.claim is None:
            singletons.append((place, candidate))
            continue
        key = candidate.claim.key
        grouped.setdefault(key, []).append(candidate)
        first_seen.setdefault(key, place)

    built: list[tuple[int, DuplicateGroup]] = [
        (place, DuplicateGroup(kept=candidate)) for place, candidate in singletons
    ]
    for key, members in grouped.items():
        ranked = sorted(members, key=lambda c: (_plane_preference(c.plane), c.position, c.ref))
        built.append((first_seen[key], DuplicateGroup(kept=ranked[0], also=tuple(ranked[1:]))))
    return tuple(group for _place, group in sorted(built, key=lambda pair: pair[0]))
