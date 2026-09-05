"""Where a document is cut, what is never cut, and what every cut carries with it.

Task ids: M7.2.2, M7.3.1, M7.3.2
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from brain.gate.provenance import Anchor
from brain.knowledge.chunking import (
    Block,
    BlockKind,
    Chunk,
    ChunkBounds,
    ChunkingError,
    chunk_blocks,
    chunk_document,
    chunk_text,
    permissions_of,
    rechunk,
)
from brain.knowledge.item import KnowledgeItem, KnowledgeState
from brain.knowledge.visibility import KnowledgeVisibility, Visibility

BOUNDS = ChunkBounds(size=120, overlap=20, minimum=40, lookback=40)

#: Long enough to need several cuts under BOUNDS, with paragraph and sentence boundaries in
#: it so the snapping rule has something to find.
PROSE = (
    "Deployments go out on a Tuesday morning. Never on a Friday, and never in the "
    "last week of a quarter.\n\n"
    "Before a release, take a database snapshot and record the migration id in the "
    "change log. The snapshot is what makes a rollback a decision rather than an "
    "argument.\n\n"
    "If the smoke tests fail twice, roll back and open a ticket. Do not try a third "
    "time on a hunch."
)


def _item(
    *,
    level: Visibility = Visibility.DEPARTMENT,
    state: KnowledgeState = KnowledgeState.PUBLISHED,
) -> KnowledgeItem:
    visibility = (
        KnowledgeVisibility.of_department("web")
        if level is Visibility.DEPARTMENT
        else KnowledgeVisibility(level=level, owner_id="p_wei_ling", department="web")
    )
    return KnowledgeItem(
        item_id="k_deployment_sop",
        content=PROSE,
        title="Web deployment SOP",
        visibility=visibility,
        owner_id="p_wei_ling",
        state=state,
    )


# --------------------------------------------------------- the bounds (M7.3.1)
def test_an_overlap_that_reaches_the_minimum_is_refused() -> None:
    """The relationship the cutting arithmetic rests on. If the overlap could reach the
    minimum, the next chunk could start at or before the current one, so the loop would either
    repeat forever or emit the same passage twice under two ids, and the index would carry
    duplicate evidence for the same claim."""
    with pytest.raises(ChunkingError, match="not shorter than minimum"):
        ChunkBounds(size=100, overlap=40, minimum=40)


def test_a_minimum_longer_than_the_size_is_refused() -> None:
    """No chunk can satisfy both bounds, so every cut would violate one of them and the
    violation would be silent. It is a configuration mistake and it belongs at construction."""
    with pytest.raises(ChunkingError, match="longer than size"):
        ChunkBounds(size=100, overlap=10, minimum=200)


def test_a_size_of_zero_is_refused() -> None:
    """A zero size cuts nothing and loops forever. Deleting this turns a typo in a console
    field into a hung ingestion worker."""
    with pytest.raises(ChunkingError, match="not a length"):
        ChunkBounds(size=0, overlap=0, minimum=0)


# --------------------------------------------------------- the cutting (M7.3.1)
def test_the_chunks_cover_the_whole_text_with_no_gap() -> None:
    """A gap is text that is in the document and in no chunk, so it is unfindable while
    looking indexed. Nobody reports it, because the answer that omits it is merely thin."""
    passages = chunk_text(PROSE, bounds=BOUNDS)
    assert passages[0].start == 0
    assert passages[-1].end == len(PROSE)
    for earlier, later in pairwise(passages):
        assert later.start <= earlier.end


def test_each_chunk_starts_after_the_one_before_it() -> None:
    """Progress. Without it the cutting loop repeats a passage, and the index holds two ids
    for the same characters, which double-counts a claim in every retrieval that finds both."""
    passages = chunk_text(PROSE, bounds=BOUNDS)
    starts = [passage.start for passage in passages]
    assert starts == sorted(set(starts))


def test_no_chunk_is_longer_than_the_size_bound() -> None:
    """The size bound is the one the embedding model actually enforces. A chunk over it is
    truncated by the model rather than by us, so the tail is embedded as though it were not
    there and the passage is retrievable by half its content."""
    for passage in chunk_text(PROSE, bounds=BOUNDS):
        assert len(passage.text) <= BOUNDS.size


def test_consecutive_chunks_repeat_the_end_of_the_one_before() -> None:
    """Overlap is the whole reason a sentence spanning a cut is still retrievable. Deleting
    this lets the overlap fall to zero without any test noticing, and the symptom is a
    question that is answerable from the document going unanswered."""
    passages = chunk_text(PROSE, bounds=BOUNDS)
    assert len(passages) > 2
    for earlier, later in pairwise(passages):
        assert later.start < earlier.end


def test_a_cut_prefers_a_real_boundary_over_the_size_bound() -> None:
    """A cut through the middle of a word costs retrieval twice: once in the embedding and
    once in the passage a person is shown. The snapping rule is what makes the chunks
    readable, and nothing else would notice it being removed."""
    text = "alpha beta gamma. " * 40
    passages = chunk_text(text, bounds=BOUNDS)
    for passage in passages[:-1]:
        assert passage.text.endswith((". ", " ", "\n"))


def test_a_document_shorter_than_one_chunk_is_one_chunk() -> None:
    """The common case for a meeting note. If the short path produced nothing, small documents
    would upload cleanly and never be retrievable, which reads as a search problem rather than
    an ingestion one."""
    passages = chunk_text("A short note about nothing much.", bounds=BOUNDS)
    assert len(passages) == 1
    assert passages[0].start == 0


def test_a_short_tail_is_widened_backwards_rather_than_indexed_as_a_fragment() -> None:
    """A twelve-character chunk embeds as noise and retrieves against everything. Widening it
    backwards keeps it inside the size bound, which merging it forwards into the previous
    chunk would not."""
    # Chosen so the final window leaves a handful of characters over.
    text = "x" * (BOUNDS.size + 5)
    passages = chunk_text(text, bounds=BOUNDS)
    assert len(passages) > 1
    assert len(passages[-1].text) >= BOUNDS.minimum
    assert passages[-1].end == len(text)


def test_an_empty_text_produces_no_chunks() -> None:
    """A zero-width chunk renders as a citation pointing at nothing. Returning one would put
    an unfollowable citation into an answer that otherwise looks correct."""
    assert chunk_text("", bounds=BOUNDS) == ()


def test_the_offset_places_a_passage_in_the_document_rather_than_in_its_block() -> None:
    """Spans are document offsets, because that is what a citation resolves against. Without
    the offset every block's passages would claim to start at zero and every citation after
    the first block would point at the wrong text."""
    passages = chunk_text("A short note.", bounds=BOUNDS, offset=500)
    assert passages[0].start == 500
    assert passages[0].end == 513


# ----------------------------------------------------------- tables (M7.2.2)
def test_a_table_is_never_split_however_long_it_is() -> None:
    """Half a table is a header with no rows or rows with no header, and both read as an
    answer rather than as a fragment. This is the one case where exceeding the size bound is
    the correct outcome."""
    table = "| sku | sell | cost |\n" + "".join(f"| a{n} | 10 | 4 |\n" for n in range(60))
    assert len(table) > BOUNDS.size
    passages = chunk_blocks([Block(kind=BlockKind.TABLE, text=table, start=0)], bounds=BOUNDS)
    assert len(passages) == 1
    assert passages[0].text == table
    assert passages[0].kind is BlockKind.TABLE


def test_prose_around_a_table_is_still_cut_normally() -> None:
    """Otherwise the presence of one table in a contract would make the whole document
    atomic, and a forty-page PDF would arrive at the embedding model as a single chunk."""
    blocks = [
        Block(kind=BlockKind.PROSE, text=PROSE, start=0),
        Block(kind=BlockKind.TABLE, text="| a | b |\n| 1 | 2 |", start=len(PROSE)),
    ]
    passages = chunk_blocks(blocks, bounds=BOUNDS)
    assert sum(1 for p in passages if p.kind is BlockKind.PROSE) > 1
    assert sum(1 for p in passages if p.kind is BlockKind.TABLE) == 1


def test_blocks_out_of_document_order_are_refused() -> None:
    """Out-of-order blocks produce passages whose spans point at other blocks' text, so the
    citation resolves to a passage that does not contain the claim. That is the worst kind of
    wrong: followable, specific, and about the wrong paragraph."""
    blocks = [
        Block(kind=BlockKind.PROSE, text="second", start=100),
        Block(kind=BlockKind.PROSE, text="first", start=0),
    ]
    with pytest.raises(ChunkingError, match="document order"):
        chunk_blocks(blocks, bounds=BOUNDS)


def test_an_empty_block_is_refused() -> None:
    """It would produce a zero-width span, which renders as a citation pointing at nothing,
    and a parser that emits empty blocks for page breaks is entirely plausible."""
    with pytest.raises(ChunkingError, match="empty block"):
        Block(kind=BlockKind.PROSE, text="", start=0)


# -------------------------------------------------- the chunk itself (M7.3.2)
def test_a_chunk_carries_its_document_s_permissions() -> None:
    """The sentence this package exists to make true. A chunk with anything other than its
    document's scope, owner and level is a passage retrieval can reach on terms nobody set,
    and the answer drawn from it looks exactly like a correct one."""
    item = _item()
    chunks = chunk_document(item, [Block(kind=BlockKind.PROSE, text=PROSE, start=0)], bounds=BOUNDS)
    assert chunks
    for chunk in chunks:
        assert chunk.permissions == permissions_of(item)
        assert chunk.scope == item.scope
        assert chunk.owner_id == item.owner_id
        assert chunk.visibility is item.visibility.level


def test_a_chunk_knows_its_position_and_its_document() -> None:
    """The metadata M7.3.2 asks for. Without the ordinal there is no "the passage after this
    one", and without the document id a retrieved chunk cannot be traced back to the thing
    whose permissions it is claiming to carry."""
    chunks = chunk_document(
        _item(), [Block(kind=BlockKind.PROSE, text=PROSE, start=0)], bounds=BOUNDS
    )
    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))
    assert {chunk.document_id for chunk in chunks} == {"k_deployment_sop"}


def test_a_chunk_id_is_a_reference_a_citation_can_hold() -> None:
    """Chunk ids end up inside `brain.gate.provenance.Anchor`, whose grammar excludes the
    separator everybody reaches for first. An id an anchor refuses is a passage that can be
    retrieved and never cited, which shows up as an answer with no evidence behind it."""
    chunks = chunk_document(
        _item(), [Block(kind=BlockKind.PROSE, text=PROSE, start=0)], bounds=BOUNDS
    )
    for chunk in chunks:
        anchor = Anchor(chunk_id=chunk.chunk_id, start=chunk.start, end=chunk.end)
        assert anchor.chunk_id == chunk.chunk_id


def test_a_chunk_cannot_be_built_outside_the_chunker() -> None:
    """The guarantee is structural rather than remembered. A re-indexing script written in a
    hurry is the realistic way a chunk acquires a scope somebody chose, and a comment saying
    "always copy the document's" is obeyed until that afternoon."""
    with pytest.raises(ChunkingError, match="may only be built by"):
        Chunk(
            chunk_id="k_x.0000",
            document_id="k_x",
            ordinal=0,
            text="anything at all",
            start=0,
            end=15,
            kind=BlockKind.PROSE,
            permissions=permissions_of(_item()),
        )


def test_a_superseded_document_is_not_chunked() -> None:
    """Its passages would enter the index and answer questions beside the version that
    replaced it, carrying a badge saying the content was verified. The replacement would not
    displace them, because nothing links an index entry to a state it no longer has."""
    with pytest.raises(ChunkingError, match="must not be chunked"):
        chunk_document(
            _item(state=KnowledgeState.SUPERSEDED),
            [Block(kind=BlockKind.PROSE, text=PROSE, start=0)],
            bounds=BOUNDS,
        )


def test_rechunking_a_document_produces_the_same_ids() -> None:
    """Ids are positional, so a re-parse reuses them while the spans move. That is the
    documented cost of re-parsing, and a test is the only place it is written down where
    somebody will meet it."""
    item = _item()
    blocks = [Block(kind=BlockKind.PROSE, text=PROSE, start=0)]
    first = chunk_document(item, blocks, bounds=BOUNDS)
    again = rechunk(item, blocks, bounds=BOUNDS)
    assert [chunk.chunk_id for chunk in first] == [chunk.chunk_id for chunk in again]
