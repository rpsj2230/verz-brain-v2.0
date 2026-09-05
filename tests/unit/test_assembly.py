"""Result assembly: what may be added to what came back, and what may be taken away.

Two failures, and they run in opposite directions.

**Widening fails silently and looks like better recall.** A document result that fetched the
neighbouring passage, or offered a link to the file, or said how many passages it did not
show, reads in a diff as a more helpful answer. Nothing in the ordinary suite notices, because
the extra material is correct: it is a real passage of a real document. What is wrong is that
nothing decided the reader may have it, and the decision that would have was the reach
predicate inside a query that ran two layers ago.

**Deduplication fails silently and looks like a shorter list.** A merge that drops the wrong
member removes a fact the reader was entitled to, and the removal leaves nothing behind: no
error, no count, no gap. Nobody files a bug against a result that merely never appeared. The
worst version of it drops a visible row because an invisible document covered the same fact,
so the reader loses something for a reason they may not know exists.

The tests below are therefore mostly about shapes and signatures rather than about behaviour.
A field that does not exist cannot be filled in during an incident, and a function with one
parameter cannot be told about a document the caller may not see.

Task ids: M15.3.2, M15.3.3
"""

from __future__ import annotations

import dataclasses
import inspect

import pytest

from brain.knowledge.assembly import (
    A_DOCUMENT_RESULT_IS_A_FUNCTION_OF_THE_PASSAGES_RETURNED,
    A_DUPLICATE_ONLY_LOSES_ITS_PLACE_TO_ONE_THE_CALLER_CAN_SEE,
    AssemblyError,
    Candidate,
    Claim,
    DocumentResult,
    DuplicateGroup,
    Plane,
    RetrievedChunk,
    by_chunk,
    by_document,
    deduplicate,
)

CONTRACT = Claim(entity="client", record_id="42", field="contract_value", value="120000")


def chunk(
    chunk_id: str, document_id: str = "doc1", ordinal: int = 0, *, title: str = "", text: str = "x"
) -> RetrievedChunk:
    """A retrieved passage with the fields a test cares about and defaults for the rest."""
    return RetrievedChunk(
        chunk_id=chunk_id, document_id=document_id, ordinal=ordinal, text=text, title=title
    )


def bodies_of(*chunks: RetrievedChunk) -> dict[str, RetrievedChunk]:
    return {c.chunk_id: c for c in chunks}


# --------------------------------------------------- the chunk level (M15.3.2)
def test_a_retrieved_chunk_has_nowhere_to_carry_a_permission() -> None:
    """Stated against the shape rather than against behaviour, in the same form
    `test_a_ranking_has_nowhere_to_put_a_score` uses in `test_fusion.py`.

    A `scope` or a `visibility` field would be added helpfully, by somebody writing a renderer
    who wanted to show a badge, and the next change would read it "just to double-check". At
    that point there are two answers to who may see this passage: the predicate that was
    inside the query, and a copy of the document's permissions frozen at indexing time. The
    copy is the one the renderer uses and it is the stale one.

    Delete this and the second permission decision arrives as a convenience.
    """
    names = {f.name for f in dataclasses.fields(RetrievedChunk)}
    assert names == {"chunk_id", "document_id", "ordinal", "text", "title", "section", "page"}
    forbidden = {
        "scope",
        "permissions",
        "owner",
        "owner_id",
        "visibility",
        "state",
        "department",
        "deleted_at",
    }
    surface = names | {n for n in dir(RetrievedChunk) if not n.startswith("_")}
    assert not (surface & forbidden), f"a retrieved chunk carries permissions: {surface}"


def test_the_retrieval_order_decides_membership_and_the_mapping_only_supplies_text() -> None:
    """**The safety property of the chunk level.** The two arrangements read identically in a
    diff: iterate the order and look bodies up, or iterate the bodies and look the order up.
    The second admits everything the mapping happens to hold, and in a real deployment the
    mapping is a page cache or a batch read that was not narrowed by anybody's reach.

    Delete this and the loop can be turned round by somebody tidying, and the answer quietly
    grows to whatever was in the cache.
    """
    wanted = chunk("c1")
    unasked = chunk("c9")
    got = by_chunk(["c1"], bodies_of(wanted, unasked))
    assert got == (wanted,)


def test_a_reference_with_no_body_is_skipped_rather_than_refused() -> None:
    """The order and the bodies are fetched separately, so a body that has not arrived is an
    ordinary race rather than a fault. Raising would turn a slightly shorter answer into no
    answer at all, which is the failure mode this whole package spends its docstrings avoiding.

    Delete this and somebody makes the missing body an error, and one slow read takes the
    whole answer down.
    """
    present = chunk("c1")
    assert by_chunk(["c1", "c2"], bodies_of(present)) == (present,)


def test_a_passage_listed_twice_in_the_order_is_refused() -> None:
    """The same refusal `Ranking.__post_init__` makes, one layer later. It arrives from a
    query that joined and forgot to be distinct, so it looks like nothing in a diff, and it
    puts one passage in the answer twice and makes one document look like two matches.

    Delete this and a joined query silently doubles a document's apparent support.
    """
    with pytest.raises(AssemblyError, match="more than once"):
        by_chunk(["c1", "c1"], bodies_of(chunk("c1")))


def test_by_chunk_returns_the_passages_in_the_order_retrieval_gave_them() -> None:
    """The sibling of the three refusals above: the ordinary path still works, and it
    preserves the order rather than sorting by anything of its own.

    Delete this and a function that refused every input would satisfy the rest of this
    section.
    """
    first, second = chunk("c2", ordinal=7), chunk("c1", ordinal=1)
    assert by_chunk(["c2", "c1"], bodies_of(first, second)) == (first, second)


# ------------------------------------------------ the document level (M15.3.2)
def test_a_document_result_has_nowhere_to_put_a_link_or_a_total() -> None:
    """**The widening the leaf names, expressed as a field set.** Each absent field is a
    different disclosure. A `body` is the whole document, which two visible passages do not
    entitle anybody to. A `url` is a claim that this reader may open the file, decided here
    against permissions copied onto chunks at indexing time rather than by whatever serves
    files against the permissions the document has now. A `passage_total` is a count of hidden
    things, and an `omitted` is the same count arrived at by subtraction.

    Delete this and the first person writing a citation footer adds the total, because a
    footer reading "2 passages" looks incomplete without it.
    """
    names = {f.name for f in dataclasses.fields(DocumentResult)}
    assert names == {"document_id", "title", "passages", "position"}
    forbidden = {
        "body",
        "content",
        "full_text",
        "url",
        "link",
        "href",
        "passage_total",
        "total",
        "omitted",
        "withheld",
        "chunk_count",
        "available",
    }
    surface = names | {n for n in dir(DocumentResult) if not n.startswith("_")}
    assert not (surface & forbidden), f"a document result asserts more than its passages: {surface}"


def test_by_document_has_no_parameter_a_corpus_could_arrive_through() -> None:
    """The structural half of `A_DOCUMENT_RESULT_IS_A_FUNCTION_OF_THE_PASSAGES_RETURNED`. A
    store, a session or a chunk fetcher is one parameter, it reads as an improvement in
    recall, and it is how neighbour expansion and a passage total both arrive. With one
    parameter there is nothing to fetch from and nothing to count against.

    Delete this and the second parameter is added by whoever wants the passage before the one
    that matched, and it will be a retired chunk the reach predicate deliberately excluded.
    """
    assert list(inspect.signature(by_document).parameters) == ["chunks"]
    assert "passages retrieval returned" in A_DOCUMENT_RESULT_IS_A_FUNCTION_OF_THE_PASSAGES_RETURNED


def test_a_document_is_placed_by_its_best_passage_and_not_by_how_many_it_has() -> None:
    """Summing a document's passages is the standard failure of document-level aggregation: a
    long document with many mediocre passages outranks a short one with the answer in it, and
    from the outside that looks like better recall because more text came back.

    Delete this and the aggregation is changed to a sum during a week when the ordering looks
    thin, and long documents win every question afterwards.
    """
    passages = (
        chunk("a1", document_id="short", ordinal=0),
        chunk("b1", document_id="long", ordinal=0),
        chunk("b2", document_id="long", ordinal=1),
        chunk("b3", document_id="long", ordinal=2),
    )
    assert [d.document_id for d in by_document(passages)] == ["short", "long"]


def test_passages_within_a_document_are_in_reading_order_rather_than_relevance_order() -> None:
    """A person quoting two passages of one document wants them the way round the document has
    them. Relevance order inside a document produces a quotation that reads as though the
    author wrote the conclusion first.

    Delete this and the passages follow the fused order, and every multi-passage citation
    reads backwards for the half of them where the later passage matched better.
    """
    later, earlier = chunk("c9", ordinal=9), chunk("c2", ordinal=2)
    assembled = by_document((later, earlier))
    assert [p.ordinal for p in assembled[0].passages] == [2, 9]


def test_the_title_comes_from_the_earliest_passage_that_carries_one() -> None:
    """Deterministic, and both alternatives are worse. Refusing when two passages disagree
    turns an indexing race into a failed answer; taking the best-ranked passage's title makes
    the document's name a function of the question, so the same document is called two things
    in two answers.

    Delete this and the title becomes whichever passage the grouping loop reached first, which
    is stable until somebody changes the sort.
    """
    assembled = by_document(
        (
            chunk("c9", ordinal=9, title="Late heading"),
            chunk("c2", ordinal=2, title="Maintenance SOP"),
            chunk("c1", ordinal=1, title=""),
        )
    )
    assert assembled[0].title == "Maintenance SOP"


def test_a_document_result_cannot_be_built_from_another_documents_passages() -> None:
    """The constructor guard behind the grouping, in the two-refusal shape
    `brain.ops.denial_alerts` uses: the grouping is the check a refactor drops and the
    constructor is the one a hand-built result goes around. A citation whose passages belong
    to a different document is a citation nobody can check.

    Delete this and a hand-assembled result can attribute one document's text to another,
    which is the single most damaging thing a citation can do.
    """
    with pytest.raises(AssemblyError, match="another document"):
        DocumentResult(
            document_id="doc1",
            title="",
            passages=(chunk("c1", document_id="doc2"),),
            position=1,
        )


def test_a_document_result_with_no_passages_cannot_be_built() -> None:
    """An empty document result asserts that the document exists and says nothing else, which
    is precisely the assertion a result assembled from passages is not entitled to make. It is
    also how a "we found this document but cannot show you any of it" line reaches a reader,
    which is DENIED and ABSENT told apart in one sentence.

    Delete this and a filtering step upstream that empties a document leaves the shell behind.
    """
    with pytest.raises(AssemblyError, match="no passages"):
        DocumentResult(document_id="doc1", title="", passages=(), position=1)


def test_by_document_returns_every_passage_it_was_given() -> None:
    """The sibling of the refusals above. Grouping must not be a filter: every passage that
    went in comes out, under exactly one document.

    Delete this and a grouping that dropped passages on some condition would still satisfy
    every ordering test in this section.
    """
    passages = (chunk("a1", document_id="x", ordinal=1), chunk("b1", document_id="y", ordinal=0))
    assembled = by_document(passages)
    assert sorted(p.chunk_id for d in assembled for p in d.passages) == ["a1", "b1"]


# ------------------------------------------- deduplication across planes (M15.3.3)
def test_deduplication_has_nowhere_to_be_told_about_a_result_the_caller_cannot_see() -> None:
    """**The disclosure rule, expressed as a signature.** A second parameter carrying a shared
    duplicate index, a corpus, a cache or another caller's results is how a thing outside this
    caller's list gets a vote on what is in it. With one parameter the group is drawn from the
    argument and the representative is drawn from the group, so the reason a result is not
    shown first is always another result the reader is holding.

    Delete this and the fact index is added at ingest, because computing the relation once is
    obviously cheaper than computing it per caller.
    """
    assert list(inspect.signature(deduplicate).parameters) == ["candidates"]
    assert "without a caller" in A_DUPLICATE_ONLY_LOSES_ITS_PLACE_TO_ONE_THE_CALLER_CAN_SEE


def test_a_document_the_caller_cannot_see_cannot_remove_a_row_they_can() -> None:
    """The scenario the rule exists for, run the way retrieval actually produces it. A caller
    entitled to the row and not to the document gets a candidate list with the row in it and
    the document nowhere, because the reach predicate was inside the query. The row survives,
    and there was no argument through which the document could have been mentioned.

    Delete this and the concrete case goes untested, leaving only the abstract signature
    assertion above, which somebody can satisfy while reintroducing the behaviour through a
    module-level index.
    """
    row = Candidate(plane=Plane.ROW, ref="rec42", position=1, claim=CONTRACT)
    groups = deduplicate([row])
    assert [g.kept for g in groups] == [row]
    assert groups[0].also == ()


def test_a_row_and_a_passage_stating_one_fact_become_one_group_that_keeps_both() -> None:
    """The other half of the same rule, and the reason deduplication groups rather than drops.
    The harm being prevented is one fact read as two sources agreeing, not a list that is too
    long, so the passage stays in the group where a renderer that can cite it still can.

    Delete this and the merge is changed to a drop, and every answer loses the citation for
    facts the row plane also carries.
    """
    row = Candidate(plane=Plane.ROW, ref="rec42", position=3, claim=CONTRACT)
    passage = Candidate(plane=Plane.DOCUMENT, ref="doc7.c2", position=1, claim=CONTRACT)
    (group,) = deduplicate([row, passage])
    assert group.members == (row, passage)
    assert group.planes == ("document", "row")
    assert group.corroborated


def test_every_result_the_caller_held_is_still_in_exactly_one_group() -> None:
    """The partition property, which is what "nothing is lost" means when it is checkable.
    Stated over a mixed list rather than over a pair, because the failure that matters is a
    grouping loop that forgets the candidates it did not group.

    Delete this and a rewrite that iterates the claim keys instead of the candidates drops
    every claimless result, silently, and those are the passages nothing has linked to a
    record: most of the corpus.
    """
    held = [
        Candidate(plane=Plane.ROW, ref="rec42", position=1, claim=CONTRACT),
        Candidate(plane=Plane.DOCUMENT, ref="doc7.c2", position=1, claim=CONTRACT),
        Candidate(plane=Plane.DOCUMENT, ref="doc9.c1", position=2),
        Candidate(plane=Plane.ROW, ref="rec43", position=2),
    ]
    members = [m for group in deduplicate(held) for m in group.members]
    assert sorted(m.ref for m in members) == sorted(c.ref for c in held)
    assert len(members) == len(held)


def test_the_representative_is_always_a_member_of_its_own_group() -> None:
    """The sentence the whole leaf turns on, asserted rather than argued. A representative
    chosen from anywhere else is a result the caller may not be holding, and then the group is
    shown as something they cannot see or is dropped entirely.

    Delete this and a later change can pick the representative from a lookup table of
    canonical records, which is exactly the shape the module docstring rejects.
    """
    held = [
        Candidate(plane=Plane.DOCUMENT, ref="doc7.c2", position=1, claim=CONTRACT),
        Candidate(plane=Plane.ROW, ref="rec42", position=9, claim=CONTRACT),
    ]
    for group in deduplicate(held):
        assert group.kept in group.members
        assert group.kept in held


def test_the_row_plane_represents_a_group_it_is_in() -> None:
    """A row is the record; a passage is prose about the record, true when it was written. The
    row plane's projection is also the finer-grained of the two permission decisions, compiled
    per column from capabilities, so preferring it never puts a value in front of a reader
    through the coarser of the two gates that govern it. Asserted with the row placed *worse*
    in its own plane, so the test fails if the rule degrades to "whichever ranked higher".

    Delete this and the preference silently becomes position-based, which compares a place in
    the row plane against a place in the document plane: two numbers on different scales.
    """
    row = Candidate(plane=Plane.ROW, ref="rec42", position=9, claim=CONTRACT)
    passage = Candidate(plane=Plane.DOCUMENT, ref="doc7.c2", position=1, claim=CONTRACT)
    (group,) = deduplicate([passage, row])
    assert group.kept is row


def test_a_candidate_stating_no_comparable_claim_is_never_merged() -> None:
    """`UNDER_DEDUPLICATION_IS_THE_SAFE_FAILURE` in the ordinary case. Most passages have
    nothing linking them to a record, and the choice is between showing one fact twice, which
    a reader sees and discounts, and merging two facts that were not the same, which removes
    one of them without trace.

    Delete this and a claimless candidate becomes mergeable on something looser, a title or a
    shared document, and two different statements collapse into one.
    """
    a = Candidate(plane=Plane.DOCUMENT, ref="doc7.c2", position=1)
    b = Candidate(plane=Plane.DOCUMENT, ref="doc7.c3", position=2)
    groups = deduplicate([a, b])
    assert [g.kept for g in groups] == [a, b]
    assert all(g.also == () for g in groups)


def test_two_statements_of_one_fact_are_one_fact_whatever_the_spacing_and_case() -> None:
    """The sibling that stops the normalisation being satisfied by a function that never
    matches anything. Whitespace and case are the two differences that carry no meaning
    between a database column and a sentence quoting it.

    Delete this and the key can be tightened to exact equality, deduplication stops firing,
    and the only symptom is answers that cite the same figure twice.
    """
    row = Candidate(plane=Plane.ROW, ref="rec42", position=1, claim=CONTRACT)
    passage = Candidate(
        plane=Plane.DOCUMENT,
        ref="doc7.c2",
        position=1,
        claim=Claim(entity="Client", record_id="42", field="Contract_Value", value="  120000  "),
    )
    (group,) = deduplicate([row, passage])
    assert group.corroborated


def test_two_figures_written_differently_stay_two_facts() -> None:
    """The other side of the same line. A normaliser clever enough to read "120,000" and
    "120000" as one figure is clever enough to read "120,000" and "1,20,000" as one too, and
    the merged one is gone without trace. Formatting is left alone deliberately.

    Delete this and somebody adds digit-group stripping to make a demo look tidy, and the day
    two genuinely different values normalise together nothing reports it.
    """
    row = Candidate(plane=Plane.ROW, ref="rec42", position=1, claim=CONTRACT)
    passage = Candidate(
        plane=Plane.DOCUMENT,
        ref="doc7.c2",
        position=1,
        claim=Claim(entity="client", record_id="42", field="contract_value", value="120,000"),
    )
    assert len(deduplicate([row, passage])) == 2


def test_deduplication_preserves_the_order_it_was_given_and_invents_none() -> None:
    """A place in the row plane and a place in the document plane are not on one scale, which
    is `brain.knowledge.fusion`'s argument one level up. So groups come back in the order of
    their earliest member and this function never sorts by plane, by position or by anything
    else it could compare across the two.

    Delete this and the groups are sorted by position "for tidiness", which silently ranks a
    projected row against a fused passage.
    """
    first = Candidate(plane=Plane.DOCUMENT, ref="doc9.c1", position=7)
    second = Candidate(plane=Plane.ROW, ref="rec43", position=1)
    assert [g.kept for g in deduplicate([first, second])] == [first, second]


def test_a_result_passed_twice_is_refused_rather_than_counted_twice() -> None:
    """One result listed twice would corroborate itself, which is the fake agreement `Ranking`
    refuses inside a single list. It arrives the same way it does there: a caller that built
    one of the two candidate lists twice.

    The refusal is matched on the wording `deduplicate` uses rather than on the phrase both
    guards share. A mutation loosening this check was caught by `DuplicateGroup` instead and
    survived the looser assertion, which is a test proving *a* refusal happened rather than
    the one it names. The second pair below is the case only this guard can catch: two
    candidates under one reference stating different things land in different groups, so the
    constructor never sees them together and the reference is counted twice.

    Delete this and a duplicated candidate makes an uncorroborated fact read as corroborated,
    which is the one thing `corroborated` is relied on for.
    """
    row = Candidate(plane=Plane.ROW, ref="rec42", position=1, claim=CONTRACT)
    with pytest.raises(AssemblyError, match="the candidate list carries"):
        deduplicate([row, row])
    restated = Candidate(
        plane=Plane.ROW,
        ref="rec42",
        position=2,
        claim=Claim(entity="client", record_id="42", field="contract_value", value="90000"),
    )
    with pytest.raises(AssemblyError, match="the candidate list carries"):
        deduplicate([row, restated])


def test_a_group_cannot_be_hand_built_around_results_stating_different_things() -> None:
    """The constructor half of the two-refusal pattern. `deduplicate` groups by key, so this
    can only be reached by building a group directly, which is what a renderer or a test
    fixture does. A group is the assertion that its members say one thing, and a wrong one
    merges two facts and loses the second.

    Delete this and a hand-built group becomes a way to make two different figures look like
    one corroborated one.
    """
    row = Candidate(plane=Plane.ROW, ref="rec42", position=1, claim=CONTRACT)
    other = Candidate(
        plane=Plane.DOCUMENT,
        ref="doc7.c2",
        position=1,
        claim=Claim(entity="client", record_id="42", field="contract_value", value="90000"),
    )
    with pytest.raises(AssemblyError, match="stating something else"):
        DuplicateGroup(kept=row, also=(other,))


def test_a_group_cannot_be_hand_built_around_a_result_with_no_claim() -> None:
    """The same guard from the other side: a candidate stating nothing comparable cannot be
    merged, so it cannot be placed in a group with anything either. Without this the claimless
    case is refused by `deduplicate` and permitted by the constructor, and the constructor is
    the one a renderer reaches.

    Delete this and the safe failure direction holds only on the path that happens to be
    tested.
    """
    row = Candidate(plane=Plane.ROW, ref="rec42", position=1, claim=CONTRACT)
    silent = Candidate(plane=Plane.DOCUMENT, ref="doc7.c2", position=1)
    with pytest.raises(AssemblyError, match="no comparable claim"):
        DuplicateGroup(kept=row, also=(silent,))


def test_a_group_of_one_is_ordinary_and_asserts_nothing_about_sameness() -> None:
    """The sibling of the two constructor refusals: the common case still builds. A guard
    tested only by its refusals is satisfied by a constructor that refuses everything, and
    most groups have one member.

    Delete this and the sameness check can be tightened until singletons stop constructing,
    which would take out every claimless passage in every answer.
    """
    silent = Candidate(plane=Plane.DOCUMENT, ref="doc7.c2", position=1)
    group = DuplicateGroup(kept=silent)
    assert group.members == (silent,)
    assert not group.corroborated


def test_corroboration_counts_planes_rather_than_members() -> None:
    """Two passages of one document stating one fact are one source, not two. Counting members
    would make a document that repeats itself look like agreement between independent
    retrievals, which is the misreading the whole of `fusion` is arranged against.

    Delete this and a repetitive document corroborates itself.
    """
    first = Candidate(plane=Plane.DOCUMENT, ref="doc7.c2", position=1, claim=CONTRACT)
    second = Candidate(plane=Plane.DOCUMENT, ref="doc7.c5", position=4, claim=CONTRACT)
    (group,) = deduplicate([first, second])
    assert len(group.members) == 2
    assert group.planes == ("document",)
    assert not group.corroborated


def test_a_claim_with_an_empty_part_is_refused() -> None:
    """Two claims with an empty field would collide on their emptiness and be merged, which is
    the loss-of-a-fact failure arriving through a blank rather than through a bug in the key.

    Delete this and an extraction step that returns "" for the field it could not read merges
    every one of its results into one group.
    """
    with pytest.raises(AssemblyError, match="empty"):
        Claim(entity="client", record_id="42", field="", value="120000")


# ------------------------------------------------ what a reference and a place are
def test_a_reference_no_citation_could_resolve_is_refused() -> None:
    """Everything assembled here ends up inside a citation, and a citation nothing can resolve
    is a citation nobody checks. The grammar is `brain.knowledge.item`'s own, so a reference
    that passes here resolves against the ids that package issues rather than against a second
    alphabet invented in this file.

    Delete this and a reference carrying a space, a slash or a quote reaches a renderer, where
    it becomes either a broken link or an injection depending on the renderer.
    """
    with pytest.raises(AssemblyError, match="not a reference"):
        RetrievedChunk(chunk_id="c 1", document_id="doc1", ordinal=0, text="x")
    with pytest.raises(AssemblyError, match="not a reference"):
        RetrievedChunk(chunk_id="c1", document_id="doc 1", ordinal=0, text="x")
    with pytest.raises(AssemblyError, match="not a reference"):
        Candidate(plane=Plane.ROW, ref="rec 42", position=1)


def test_a_place_that_is_not_a_place_in_a_list_is_refused() -> None:
    """Positions are one-based here and ordinals are nought-based, which is exactly the sort of
    difference that gets copied wrongly between two modules. A nought position would order
    ahead of every real result, and a negative ordinal would put a passage before the start of
    its own document.

    Delete this and an off-by-one in whoever builds these becomes a silent reordering rather
    than a refusal, and reordering is the failure this package has the least defence against.
    """
    with pytest.raises(AssemblyError, match="not a position"):
        RetrievedChunk(chunk_id="c1", document_id="doc1", ordinal=-1, text="x")
    with pytest.raises(AssemblyError, match="not a one-based place"):
        Candidate(plane=Plane.ROW, ref="rec42", position=0)
    with pytest.raises(AssemblyError, match="not a one-based place"):
        DocumentResult(document_id="doc1", title="", passages=(chunk("c1"),), position=0)
