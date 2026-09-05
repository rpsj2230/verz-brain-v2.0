"""Cutting a document into retrievable passages without cutting it loose from its permissions.

A chunk carries the permissions of the document it came from. That is the one sentence this
whole package exists to make true, and this is the file where it is enforced, because
chunking is the only place a chunk is ever created.

**What breaks without it.** Retrieval answers from a paragraph nobody was allowed to read,
and the answer looks exactly like a correct one. There is no error, no empty result and no
lock icon: a fluent, cited, confident paragraph out of a contract the asker has never been
granted. Nobody files a bug against an answer that reads well, which is why the guarantee
has to be structural rather than a rule people remember. `Chunk` cannot be constructed
outside `chunk_document`, the same mechanism `brain.gate.catalogue.ProjectedCatalogue` uses
and for the same reason: a guarantee that depends on every caller doing the right thing is a
convention, and a convention is not a guarantee.

Permissions are copied, not narrowed and not recomputed. A chunk narrower than its document
is not a safe error, it is a silent retrieval hole: the document is readable, the passage is
not found, and the answer is thin with nothing saying why. `chunk_document` checks equality
before returning, which cannot fail today for the same reason `brain.core.department.compose`
re-checks its own narrowing, and is here so a future edit that computes a chunk scope from
anything other than the document's cannot land quietly.

**A table is never split.** Half a table retrieves as an answer: a header with no rows, or
rows whose columns are unlabelled, both of which read as facts. A table block is emitted
whole even when it exceeds the size bound, because a chunk slightly over budget is a cost
and half a price list is a wrong answer.

**Blocks are chunked independently.** Two consecutive prose blocks are not merged into one
run, and the rejected alternative is worth recording: merging means inventing the characters
the parser dropped between them, so a chunk would contain text that is not in the document
and its citation span would point somewhere else. Block granularity is the parser's decision
and this module does not second-guess it.

Nothing here embeds, stores, or reads a clock. Chunking is arithmetic over text, and keeping
it that way is what makes the invariant testable at its own boundary.

Task ids: M7.2.2, M7.3.1, M7.3.2
"""

from __future__ import annotations

import enum
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from brain.core.scope import Scope
from brain.knowledge.item import KnowledgeItem, KnowledgeState
from brain.knowledge.visibility import Visibility

#: A chunk id has to survive into a citation, so it is held to the reference grammar
#: `brain.gate.provenance` accepts. Restated rather than imported, exactly as that module
#: restates it, so this guarantee does not move when somebody widens an unrelated one. Note
#: what it excludes: `#`, the separator everybody reaches for first, which would produce ids
#: no anchor can hold.
_REFERENCE_RE: Final = re.compile(r"^[A-Za-z0-9_.@-]{1,128}$")

#: Where a chunk is cut when the size bound arrives mid-sentence, in order of preference. A
#: paragraph break is a real boundary; a space is a last resort that at least avoids cutting
#: a word in half. Cutting mid-word costs retrieval quality twice, once in the embedding and
#: once in the passage a person is shown.
_SEPARATORS: Final[tuple[str, ...]] = ("\n\n", "\n", ". ", " ")


class ChunkingError(Exception):
    """A chunk or a chunking configuration that would be unsafe or unusable.

    Outside the `brain.core.errors` taxonomy, like the other refusals in this package: those
    five outcomes describe an answer to a person, and this describes a refusal to write a
    row.
    """


class BlockKind(enum.StrEnum):
    """What a parsed block is, as far as chunking is concerned.

    Two kinds, not a taxonomy of every layout element a parser can name. The only question
    chunking asks is whether a block may be cut, and headings, lists and paragraphs all
    answer it the same way. A third kind would have to change that answer to earn its place.
    """

    PROSE = "prose"
    #: Atomic. Emitted whole however long it is, because half a table reads as an answer.
    TABLE = "table"


@dataclass(frozen=True)
class Block:
    """One piece of a parsed document, and where it sits in the parsed text.

    `start` is a character offset into the document as parsed, which is what a citation span
    resolves against. It is not an offset into the original PDF, and the distinction matters
    the day somebody re-parses: the spans move, the chunk ids move with them, and a citation
    that named only a page would have survived while one that named only characters would
    not. That is why `brain.gate.provenance.Anchor` carries both.
    """

    kind: BlockKind
    text: str
    start: int
    page: int | None = None
    section: str = ""

    def __post_init__(self) -> None:
        if self.start < 0:
            msg = f"block start {self.start} is not an offset"
            raise ChunkingError(msg)
        if not self.text:
            # An empty block would produce no chunk and a zero-width span, and a zero-width
            # span renders as a citation pointing at nothing.
            msg = "an empty block has no passage in it and would cite a zero-width span"
            raise ChunkingError(msg)

    @property
    def end(self) -> int:
        return self.start + len(self.text)


@dataclass(frozen=True)
class ChunkBounds:
    """How long a chunk may be, how short, and how much of the last one it repeats (M7.3.1).

    Four numbers, and the relationship between two of them is load-bearing:
    `overlap < minimum`. Overlap exists so a sentence spanning a cut is retrievable from
    either side; minimum exists so a fragment is never indexed alone. If the overlap could
    reach or exceed the minimum, the next chunk could start at or before the current one,
    and the loop would either repeat itself forever or emit the same passage twice under two
    ids. Requiring the relationship at construction is what makes the arithmetic in
    `chunk_text` safe rather than watchful.

    `lookback` is how far back a cut may move to find a sentence or paragraph boundary. It is
    separate from `minimum` because they answer different questions: how much of a chunk may
    be given up for a better cut, against how short a chunk may be at all.
    """

    size: int = 1200
    overlap: int = 150
    minimum: int = 200
    lookback: int = 300

    def __post_init__(self) -> None:
        if self.size <= 0:
            msg = f"size {self.size} is not a length"
            raise ChunkingError(msg)
        if self.overlap < 0:
            msg = f"overlap {self.overlap} is not a length"
            raise ChunkingError(msg)
        if self.lookback < 0:
            msg = f"lookback {self.lookback} is not a length"
            raise ChunkingError(msg)
        if self.minimum > self.size:
            msg = (
                f"minimum {self.minimum} is longer than size {self.size}; "
                "no chunk could satisfy both bounds"
            )
            raise ChunkingError(msg)
        if self.overlap >= self.minimum:
            msg = (
                f"overlap {self.overlap} is not shorter than minimum {self.minimum}; "
                "a chunk could then start at or before the one before it, and the same "
                "passage would be indexed twice under two ids"
            )
            raise ChunkingError(msg)


@dataclass(frozen=True)
class DocumentPermissions:
    """Who may read the document, held as one value so it can be copied as one value.

    Three loose fields would be three chances to copy two of them. This is the thing that
    travels from a document to every chunk of it, and it is compared for equality at the end
    of `chunk_document`, so it has to be a single comparable object.
    """

    scope: Scope
    owner_id: str
    visibility: Visibility


@dataclass(frozen=True)
class Passage:
    """A cut, before it has any permissions attached.

    Separate from `Chunk` on purpose. Everything about where to cut can be worked out, read
    and tested without a document's permissions anywhere near it, and keeping the two apart
    means the cutting logic has no way to invent a scope even by accident: there is no field
    for one.
    """

    text: str
    start: int
    end: int
    kind: BlockKind
    page: int | None = None
    section: str = ""


#: Only `chunk_document` holds this. See `Chunk` for why that is the enforcement mechanism.
_CHUNK_TOKEN: Final = object()


@dataclass(frozen=True)
class Chunk:
    """A passage, its position, and the permissions of the document it came from (M7.3.2).

    **This type cannot be constructed outside `chunk_document`.** The alternatives were
    considered and none of them holds. A comment saying "always copy the document's scope" is
    obeyed until the day somebody writes a re-indexing script at four in the afternoon. A
    check at retrieval time is a check in the wrong place: by then the chunk exists, is in the
    index, and any other reader of that index is unprotected. A default scope on the field is
    the worst of the three, because `Scope()` is the unrestricted scope, so a forgotten
    argument publishes the passage to everybody.

    `ordinal` is the position of the chunk in its document, which is the "position" the task
    asks for and is not the same as the character span. The span locates the text; the
    ordinal orders the chunks, survives a re-parse changing every offset, and is what makes
    "the passage after this one" answerable.
    """

    chunk_id: str
    document_id: str
    ordinal: int
    text: str
    start: int
    end: int
    kind: BlockKind
    permissions: DocumentPermissions
    page: int | None = None
    section: str = ""
    #: Not data. The constructor guard, and the reason a chunk has one origin.
    token: object = None

    def __post_init__(self) -> None:
        if self.token is not _CHUNK_TOKEN:
            msg = (
                "a chunk may only be built by brain.knowledge.chunking.chunk_document; "
                "a chunk built anywhere else is a passage with permissions somebody chose"
            )
            raise ChunkingError(msg)
        if not _REFERENCE_RE.match(self.chunk_id):
            msg = f"chunk id {self.chunk_id!r} is not a reference a citation can hold"
            raise ChunkingError(msg)
        if self.end <= self.start:
            msg = f"chunk span {self.start}:{self.end} is empty or inverted"
            raise ChunkingError(msg)

    @property
    def scope(self) -> Scope:
        """The document's predicate. Read through the permissions, never stored separately."""
        return self.permissions.scope

    @property
    def owner_id(self) -> str:
        return self.permissions.owner_id

    @property
    def visibility(self) -> Visibility:
        return self.permissions.visibility


# ------------------------------------------------------------- cutting (M7.3.1)


def _snap(text: str, start: int, hard_end: int, bounds: ChunkBounds) -> int:
    """Move a cut backwards to the nearest real boundary, or leave it where it is.

    The floor is the later of "the chunk is still at least `minimum` long" and "we have not
    looked back further than `lookback`". Both matter: without the first, a boundary early in
    the window produces a fragment; without the second, a document with one paragraph break
    at character three would put every cut there.
    """
    floor = max(start + bounds.minimum, hard_end - bounds.lookback)
    if floor >= hard_end:
        return hard_end
    window = text[floor:hard_end]
    for separator in _SEPARATORS:
        cut = window.rfind(separator)
        if cut != -1:
            return floor + cut + len(separator)
    return hard_end


def chunk_text(text: str, *, bounds: ChunkBounds, offset: int = 0) -> tuple[Passage, ...]:
    """Cut one run of prose into overlapping passages (M7.3.1).

    Three properties hold over the result and are worth stating, because they are what the
    invariant suite asserts rather than the loop:

    Coverage. The first passage starts at the beginning and the last ends at the end, and no
    two consecutive passages leave a gap. A gap is text that is in the document and in no
    chunk, so it is unfindable while looking indexed.

    Progress. Each passage starts strictly after the one before it, guaranteed by
    `overlap < minimum` in `ChunkBounds` rather than watched for here.

    Length. Every passage is at most `size`, and every passage except a whole document shorter
    than `minimum` is at least `minimum`. A short tail is widened backwards rather than merged
    into the passage before it, because merging forwards produces a chunk over the size bound
    and the size bound is the one the embedding model actually enforces.
    """
    if not text:
        return ()

    spans: list[tuple[int, int]] = []
    length = len(text)
    start = 0
    while start < length:
        hard_end = min(start + bounds.size, length)
        end = hard_end if hard_end >= length else _snap(text, start, hard_end, bounds)
        spans.append((start, end))
        if end >= length:
            break
        following = end - bounds.overlap
        if following <= start:
            # Unreachable while `overlap < minimum` holds, because `_snap` floors a cut at
            # `start + minimum`. Kept as a raise rather than an assert for the reason
            # `brain.core.department.compose` keeps its own re-check: it costs nothing, and
            # a future edit to the snapping rule that reintroduced a short chunk would
            # otherwise loop forever or emit one passage twice under two ids.
            msg = f"chunking made no progress at {start}; the bounds would repeat a passage"
            raise ChunkingError(msg)
        start = following

    first_start, last_end = spans[0][0], spans[-1][1]
    if len(spans) > 1 and last_end - spans[-1][0] < bounds.minimum:
        spans[-1] = (max(first_start, last_end - bounds.minimum), last_end)

    return tuple(
        Passage(
            text=text[begin:finish], start=offset + begin, end=offset + finish, kind=BlockKind.PROSE
        )
        for begin, finish in spans
    )


def chunk_blocks(blocks: Sequence[Block], *, bounds: ChunkBounds) -> tuple[Passage, ...]:
    """Cut a parsed document into passages, leaving tables whole (M7.2.2).

    The blocks must be in document order and must not overlap. That is checked rather than
    assumed, because out-of-order blocks produce passages whose spans point at other blocks'
    text, and the resulting citation resolves to a passage that does not contain the claim.
    It is the worst kind of wrong: followable, specific, and about the wrong paragraph.
    """
    previous_end = -1
    for block in blocks:
        if block.start < previous_end:
            msg = (
                f"block at {block.start} overlaps or precedes the block ending at "
                f"{previous_end}; blocks must be in document order, or a citation span "
                "resolves to the wrong passage"
            )
            raise ChunkingError(msg)
        previous_end = block.end

    passages: list[Passage] = []
    for block in blocks:
        if block.kind is BlockKind.TABLE:
            # Whole, whatever its length. A table cut in half is a header with no rows or
            # rows with no header, and both read as an answer rather than as a fragment.
            passages.append(
                Passage(
                    text=block.text,
                    start=block.start,
                    end=block.end,
                    kind=BlockKind.TABLE,
                    page=block.page,
                    section=block.section,
                )
            )
            continue
        for passage in chunk_text(block.text, bounds=bounds, offset=block.start):
            passages.append(
                Passage(
                    text=passage.text,
                    start=passage.start,
                    end=passage.end,
                    kind=BlockKind.PROSE,
                    page=block.page,
                    section=block.section,
                )
            )
    return tuple(passages)


# ------------------------------------------------- the invariant (M7.3.2)


def permissions_of(item: KnowledgeItem) -> DocumentPermissions:
    """The three facts a chunk inherits, read off the document in one place."""
    return DocumentPermissions(
        scope=item.scope,
        owner_id=item.owner_id,
        visibility=item.visibility.level,
    )


def chunk_document(
    item: KnowledgeItem, blocks: Sequence[Block], *, bounds: ChunkBounds
) -> tuple[Chunk, ...]:
    """Cut a document into chunks that carry its permissions. The only source of a `Chunk`.

    A superseded or archived document is refused. Its chunks would enter the index the moment
    they were written and would be retrievable from it, so the replaced version would keep
    answering questions beside the one that replaced it, and the answer would carry a badge
    saying it was verified.

    The equality check before returning cannot fail today. It is here so that a future edit
    computing a chunk's scope from anything other than the document's is a test failure rather
    than a quiet widening, which is the shape of the one bug this package exists to prevent.
    """
    if not item.is_retrievable:
        msg = (
            f"{item.item_id!r} is {item.state} and must not be chunked; its passages would "
            "be indexed and would answer questions beside the version that replaced it"
        )
        raise ChunkingError(msg)

    permissions = permissions_of(item)
    chunks: list[Chunk] = []
    for ordinal, passage in enumerate(chunk_blocks(blocks, bounds=bounds)):
        chunks.append(
            Chunk(
                chunk_id=f"{item.item_id}.{ordinal:04d}",
                document_id=item.item_id,
                ordinal=ordinal,
                text=passage.text,
                start=passage.start,
                end=passage.end,
                kind=passage.kind,
                permissions=permissions,
                page=passage.page,
                section=passage.section,
                token=_CHUNK_TOKEN,
            )
        )

    for chunk in chunks:
        if chunk.permissions != permissions:
            msg = (
                f"chunk {chunk.chunk_id!r} does not carry the permissions of "
                f"{item.item_id!r}; a chunk is only ever a copy of its document's reach"
            )
            raise ChunkingError(msg)
    return tuple(chunks)


def rechunk(
    item: KnowledgeItem, blocks: Sequence[Block], *, bounds: ChunkBounds
) -> tuple[Chunk, ...]:
    """Re-cut a document after a re-parse or a bounds change.

    A separate name rather than a flag, because the two calls mean different things to
    whoever reads the index afterwards: the ids are the same, the spans are not, and every
    citation issued against the old spans now points a few characters off. Naming the
    operation is the only warning available at this layer.
    """
    return chunk_document(item, blocks, bounds=bounds)


def states_that_must_not_be_indexed() -> frozenset[KnowledgeState]:
    """The states `chunk_document` refuses, named so a retrieval sweep can assert the same set.

    Returned rather than exported as a constant so that a caller cannot add to it in place,
    which would widen what may be indexed from somewhere that is not this file.
    """
    return frozenset(KnowledgeState) - frozenset({KnowledgeState.DRAFT, KnowledgeState.PUBLISHED})
