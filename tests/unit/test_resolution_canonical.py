"""The canonical model, held to the one failure entity resolution invites.

That failure is a merge widening somebody's reach. Two records that were separately
permissioned become one entity, and a view assembled the obvious way hands the readers of the
open record the fields of the restricted one. So almost every test here is about a reader who
reaches one member and not the other, and about what they are and are not told.

**The reach predicate is a real function over a real set of members, never a stub that says
yes.** A fake reach that admitted everything would make every filtering test pass against a
`resolved_view` with no filter in it at all, which is the shape of the defect CLAUDE.md names
as constant-compared-against-itself: the test would be satisfied by the thing it exists to
refuse.

**The two members in the merge tests differ in every part.** Different sources, different
source ids, different observed names, different identifier digests. A test where the two
records share a name cannot tell "A's alias was filtered out" from "A's alias looked like B's",
and it would pass against a resolver that returned the wrong one.

Task ids: M14.1.1, M14.1.2, M14.1.3, M14.1.4, M14.1.5, M14.1.6
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields
from datetime import UTC, datetime, timedelta

import pytest

from brain.resolution.canonical import (
    MAX_ALIAS_CHARS,
    MAX_FORWARD_DEPTH,
    Alias,
    CanonicalEntity,
    EntityType,
    Identifier,
    IdentifierKind,
    Link,
    MemberReach,
    ResolutionError,
    ResolvedView,
    SourceRef,
    current_id,
    family_of,
    identifier_hash,
    resolved_aliases,
    resolved_view,
)

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
PEPPER = "a-deployment-secret"

#: The two source records the merge tests are built from. Different in every part, so a
#: filtering failure cannot be mistaken for a lookup that returned the other one.
OPEN_RECORD = SourceRef(source="freshdesk", entity="company", source_id="42")
RESTRICTED_RECORD = SourceRef(source="xero", entity="contact", source_id="CON-99")


def entity(
    entity_id: str,
    *,
    merged_into: str = "",
    merged_at: datetime | None = None,
    kind: EntityType = EntityType.COMPANY,
    created_from: SourceRef = OPEN_RECORD,
) -> CanonicalEntity:
    return CanonicalEntity(
        entity_id=entity_id,
        entity_type=kind,
        created_at=NOW,
        created_by="job:backfill",
        created_from=created_from,
        merged_into=merged_into,
        merged_at=merged_at,
    )


def graph(*entities: CanonicalEntity) -> dict[str, CanonicalEntity]:
    return {one.entity_id: one for one in entities}


def link(entity_id: str, member: SourceRef, confidence: float = 0.9) -> Link:
    return Link(entity_id=entity_id, source=member, confidence=confidence, linked_at=NOW)


def alias(entity_id: str, name: str, member: SourceRef) -> Alias:
    return Alias(entity_id=entity_id, name=name, source=member, first_seen_at=NOW)


def identifier(entity_id: str, value: str, member: SourceRef) -> Identifier:
    return Identifier(
        entity_id=entity_id,
        kind=IdentifierKind.DOMAIN,
        key_hash=identifier_hash(IdentifierKind.DOMAIN, value, pepper=PEPPER),
        source=member,
        first_seen_at=NOW,
    )


def reaching(*members: SourceRef) -> MemberReach:
    """A reach predicate that admits exactly the members named and refuses the rest.

    A real function over a real set rather than a stub returning True, for the reason the
    module docstring gives: a predicate that admits everything makes every filtering test
    here pass against a resolver with no filter in it.
    """
    admitted = frozenset(members)

    def reaches(member: SourceRef) -> bool:
        return member in admitted

    return reaches


# ------------------------------------------------------------------- a merged graph
#: One entity merged into another: the restricted record's entity forwards to the open
#: record's. Built as a function rather than a constant so each test gets its own rows and
#: nothing can be mutated across tests.
def merged_pair() -> tuple[dict[str, CanonicalEntity], list[Link], list[Alias], list[Identifier]]:
    entities = graph(
        entity("ent_open", created_from=OPEN_RECORD),
        entity(
            "ent_restricted",
            created_from=RESTRICTED_RECORD,
            merged_into="ent_open",
            merged_at=NOW,
        ),
    )
    links = [
        link("ent_open", OPEN_RECORD, confidence=0.55),
        link("ent_restricted", RESTRICTED_RECORD, confidence=1.0),
    ]
    aliases = [
        alias("ent_open", "Acme Support", OPEN_RECORD),
        alias("ent_restricted", "Acme Holdings Pte Ltd", RESTRICTED_RECORD),
    ]
    identifiers = [
        identifier("ent_open", "acme-support.example", OPEN_RECORD),
        identifier("ent_restricted", "acme-holdings.example", RESTRICTED_RECORD),
    ]
    return entities, links, aliases, identifiers


# ============================================================ merging two permission surfaces
def test_a_reader_who_reaches_one_member_of_a_merged_entity_is_told_nothing_about_the_other() -> (
    None
):
    """The property the whole module exists for.

    Delete this and a merge becomes a way to widen reach: the resolver could gather every
    member of the entity, and a reader entitled to the open Freshdesk record would receive the
    restricted Xero record's source id, its observed name and its identifier, none of which
    they may see. Nothing else in this file asserts that the *other* member's rows are absent
    rather than merely that the reader's own are present.
    """
    entities, links, aliases, identifiers = merged_pair()

    view = resolved_view(
        "ent_open",
        entities=entities,
        links=links,
        aliases=aliases,
        identifiers=identifiers,
        reaches=reaching(OPEN_RECORD),
    )

    assert view is not None
    assert view.members == (OPEN_RECORD,)
    assert [one.name for one in view.aliases] == ["Acme Support"]
    assert [one.source for one in view.identifiers] == [OPEN_RECORD]
    # And named the other way round, so a resolver that returned the restricted rows under
    # the open record's label could not pass.
    assert RESTRICTED_RECORD not in view.members
    assert all(one.source != RESTRICTED_RECORD for one in view.aliases)
    assert all(one.source != RESTRICTED_RECORD for one in view.identifiers)


def test_a_reader_who_reaches_both_members_of_a_merged_entity_is_told_about_both() -> None:
    """The positive sibling of the filter, without which every test above is satisfied by a
    `resolved_view` that returns nothing to anybody.

    It is also the statement of what a merge is *for*: a reader entitled to both records sees
    one entity rather than two, which is the product feature the permission rule has to survive
    rather than defeat.
    """
    entities, links, aliases, identifiers = merged_pair()

    view = resolved_view(
        "ent_restricted",
        entities=entities,
        links=links,
        aliases=aliases,
        identifiers=identifiers,
        reaches=reaching(OPEN_RECORD, RESTRICTED_RECORD),
    )

    assert view is not None
    assert set(view.members) == {OPEN_RECORD, RESTRICTED_RECORD}
    assert {one.name for one in view.aliases} == {"Acme Support", "Acme Holdings Pte Ltd"}
    assert len(view.identifiers) == 2


def test_an_alias_observed_on_a_record_the_reader_cannot_see_is_not_in_the_view() -> None:
    """An entity's name is its aliases, and a name is data about the record it was read off.

    Delete this and the natural implementation reappears: filter the members, then attach every
    alias the entity has, because aliases "belong to the entity". A reader entitled to the
    Freshdesk record would then be told the company's registered Xero name, which is precisely
    the disclosure a merge must not perform.
    """
    entities, _links, aliases, _identifiers = merged_pair()
    links = [link("ent_open", OPEN_RECORD), link("ent_restricted", RESTRICTED_RECORD)]

    view = resolved_view(
        "ent_open",
        entities=entities,
        links=links,
        aliases=aliases,
        reaches=reaching(OPEN_RECORD),
    )

    assert view is not None
    assert "Acme Holdings Pte Ltd" not in [one.name for one in view.aliases]


def test_an_identifier_asserted_by_a_record_the_reader_cannot_see_is_not_in_the_view() -> None:
    """The same rule for join keys, which are contact details even as digests.

    Delete this and a reader who reaches one member receives the peppered digest of the other
    member's domain. That digest is an oracle: anybody holding the pepper, or able to ask this
    system to hash a guess, can confirm which domain the restricted record carries.
    """
    entities, links, _aliases, identifiers = merged_pair()
    restricted_digest = identifier_hash(
        IdentifierKind.DOMAIN, "acme-holdings.example", pepper=PEPPER
    )

    view = resolved_view(
        "ent_open",
        entities=entities,
        links=links,
        identifiers=identifiers,
        reaches=reaching(OPEN_RECORD),
    )

    assert view is not None
    assert restricted_digest not in [one.key_hash for one in view.identifiers]


# ================================================ a match is a probability, a permission is not
def test_the_reach_predicate_is_never_shown_a_match_score() -> None:
    """`reaches` is called with the source record and never with the link that carries the
    confidence.

    Delete this and `resolved_view` may pass the whole `Link`, which is one line and reads as
    a convenience. The threshold arrives later, when somebody wants a review queue shorter, and
    at that point a permission decision is being made from a similarity score with nothing
    anywhere reporting it.
    """
    entities, links, aliases, identifiers = merged_pair()
    seen: list[object] = []

    def recording_reach(member: SourceRef) -> bool:
        seen.append(member)
        return member == OPEN_RECORD

    resolved_view(
        "ent_open",
        entities=entities,
        links=links,
        aliases=aliases,
        identifiers=identifiers,
        reaches=recording_reach,
    )

    assert seen, "the reach predicate was never consulted at all"
    assert all(isinstance(one, SourceRef) for one in seen)
    assert not any(hasattr(one, "confidence") for one in seen)


def test_a_confidently_matched_member_the_reader_cannot_reach_is_still_withheld() -> None:
    """Reach is decided by the predicate alone and never by the score.

    The data discriminates deliberately: the member the reader may not see is linked at 1.0 and
    the member they may see is linked at 0.55, so an implementation that admitted the
    best-scoring member, or anything above a threshold, would return the wrong one rather than
    the same one. A test built the other way round would pass against both.
    """
    entities, links, _aliases, _identifiers = merged_pair()

    view = resolved_view(
        "ent_open",
        entities=entities,
        links=links,
        reaches=reaching(OPEN_RECORD),
    )

    assert view is not None
    assert view.members == (OPEN_RECORD,)


def test_the_view_handed_to_a_reader_carries_no_match_score() -> None:
    """`ResolvedView` has no confidence on it, so nothing downstream can widen on one.

    Delete this and a `confidence` field can be added to the view for a console that wanted to
    show it. The field is then in every payload, and the next thing that reads it is something
    deciding what to display, which is one refactor away from something deciding what to
    return.
    """
    names = {field.name for field in dataclass_fields(ResolvedView)}
    assert "confidence" not in names
    assert not any("confiden" in name or "score" in name for name in names)


# =============================================================== denied and absent are one
def test_an_entity_the_reader_reaches_nothing_of_answers_as_one_that_was_never_issued() -> None:
    """The two answers are the same value, so no caller can tell them apart.

    Delete this and an empty `ResolvedView` becomes the natural return for "you reach none of
    it": it carries the real entity id and an empty member list, which is the sentence "this
    entity exists and you may see none of it". That is the existence disclosure the whole
    system is built to refuse, arriving as a tidier return type.
    """
    entities, links, aliases, identifiers = merged_pair()

    reaches_nothing = resolved_view(
        "ent_open",
        entities=entities,
        links=links,
        aliases=aliases,
        identifiers=identifiers,
        reaches=reaching(),
    )
    never_issued = resolved_view(
        "ent_that_was_never_minted",
        entities=entities,
        links=links,
        aliases=aliases,
        identifiers=identifiers,
        reaches=reaching(OPEN_RECORD, RESTRICTED_RECORD),
    )

    assert reaches_nothing is None
    assert never_issued is None
    assert reaches_nothing is never_issued


def test_the_view_has_no_field_that_could_carry_a_count_of_what_was_withheld() -> None:
    """No total, no member count, no truncation flag: nothing to subtract from.

    The expected set is written out here rather than read off the class, because a set read off
    the class is the class compared against itself and would admit any field anybody added.

    Delete this and "showing 2 of 5" becomes available, either directly or as a `total` a
    console renders. Five minus two is three facts about clients the reader may not see, and
    repeating the question with different filters turns it into a search interface over them.
    """
    names = {field.name for field in dataclass_fields(ResolvedView)}
    assert names == {"entity_id", "entity_type", "members", "aliases", "identifiers"}
    assert not any(field.type is int for field in dataclass_fields(ResolvedView))


def test_the_view_carries_none_of_the_canonical_rows_provenance() -> None:
    """`created_from` names a source record, and a reader who reaches a different member has no
    claim on it.

    Delete this and the provenance is the obvious thing to include, because it is on the row
    already and looks like metadata rather than like data. It names the record that first
    evidenced the entity, which after a merge is very often the one the reader cannot see.
    """
    names = {field.name for field in dataclass_fields(ResolvedView)}
    assert "created_from" not in names
    assert "created_by" not in names
    assert not any(name.startswith("created") for name in names)


def test_a_forwarding_chain_with_no_survivor_is_an_absence_to_a_reader() -> None:
    """A corrupt graph tells a reader nothing, and tells an operator asking directly everything.

    `resolved_view` returns None, exactly as it does for an id nobody minted, so a caller
    cannot probe for corruption. `current_id` raises, because whoever called it asked about one
    id and there is no true answer.

    Delete this and the two behaviours drift: either the view starts raising, which lets a
    caller distinguish a real entity in a broken chain from an id that does not exist, or
    `current_id` starts returning something, which hands out a non-current id that looks
    current.
    """
    entities = graph(
        entity("ent_a", merged_into="ent_b", merged_at=NOW),
        entity("ent_b", merged_into="ent_a", merged_at=NOW),
    )
    links = [link("ent_a", OPEN_RECORD)]

    assert (
        resolved_view("ent_a", entities=entities, links=links, reaches=reaching(OPEN_RECORD))
        is None
    )
    with pytest.raises(ResolutionError, match="cycle"):
        current_id("ent_a", entities)


# ================================================= the forwarding pointer resolves forever
def test_an_id_issued_before_a_merge_still_resolves_to_the_surviving_entity() -> None:
    """M14.1.5, stated as the promise it makes to whoever holds an old id.

    Delete this and the pointer can stop being followed: a caller holding `ent_restricted` gets
    a view labelled `ent_restricted`, which is an id that is no longer current, and the two
    halves of the estate then disagree about what one company is called.
    """
    entities, links, _aliases, _identifiers = merged_pair()

    view = resolved_view(
        "ent_restricted",
        entities=entities,
        links=links,
        reaches=reaching(RESTRICTED_RECORD),
    )

    assert view is not None
    assert view.entity_id == "ent_open"
    assert current_id("ent_restricted", entities) == "ent_open"


def test_a_chain_of_merges_resolves_to_the_last_surviving_entity() -> None:
    """A survivor merged again forwards twice, and the first id has to follow both hops.

    Delete this and a single-hop implementation passes every other test in this file, because
    every other graph here is one hop deep. The two-hop case is the one that arrives in
    production, the second time somebody merges.
    """
    entities = graph(
        entity("ent_1", merged_into="ent_2", merged_at=NOW),
        entity("ent_2", merged_into="ent_3", merged_at=NOW + timedelta(days=1)),
        entity("ent_3"),
    )

    assert current_id("ent_1", entities) == "ent_3"
    assert current_id("ent_2", entities) == "ent_3"
    assert current_id("ent_3", entities) == "ent_3"


def test_every_id_in_a_merge_chain_gathers_the_same_family() -> None:
    """The family is what a merged entity's rows are gathered by, and it cannot depend on which
    id the caller happened to hold.

    Delete this and `family_of` may return only the survivor, which loses every alias, link and
    identifier written against the entity that was merged away. Nothing else here would notice:
    a one-entity graph has one family whatever the implementation does.
    """
    entities = graph(
        entity("ent_1", merged_into="ent_2", merged_at=NOW),
        entity("ent_2", merged_into="ent_3", merged_at=NOW + timedelta(days=1)),
        entity("ent_3"),
        entity("ent_unrelated"),
    )

    assert family_of("ent_1", entities) == {"ent_1", "ent_2", "ent_3"}
    assert family_of("ent_3", entities) == {"ent_1", "ent_2", "ent_3"}
    assert family_of("ent_unrelated", entities) == {"ent_unrelated"}


def test_resolution_runs_over_the_whole_graph_so_two_readers_reach_one_surviving_id() -> None:
    """The survivor is a fact about the graph and not about who is asking.

    Two readers with disjoint reach resolve the same old id to the same current one, and see
    entirely different rows underneath it. Delete this and the tempting optimisation appears:
    resolve over only what the reader can see, which is fewer rows to walk, and which gives one
    id two meanings depending on who is holding it.
    """
    entities, links, aliases, identifiers = merged_pair()

    open_reader = resolved_view(
        "ent_restricted",
        entities=entities,
        links=links,
        aliases=aliases,
        identifiers=identifiers,
        reaches=reaching(OPEN_RECORD),
    )
    restricted_reader = resolved_view(
        "ent_restricted",
        entities=entities,
        links=links,
        aliases=aliases,
        identifiers=identifiers,
        reaches=reaching(RESTRICTED_RECORD),
    )

    assert open_reader is not None
    assert restricted_reader is not None
    assert open_reader.entity_id == restricted_reader.entity_id == "ent_open"
    assert open_reader.members != restricted_reader.members


def test_a_forwarding_cycle_is_refused_rather_than_followed_forever() -> None:
    """Delete this and a corrupt pair of rows hangs whichever process resolves them.

    A cycle cannot be refused by a check constraint, because no single row can see it, so this
    is the only place the two-hop case is caught at all.
    """
    entities = graph(
        entity("ent_a", merged_into="ent_b", merged_at=NOW),
        entity("ent_b", merged_into="ent_a", merged_at=NOW),
    )

    with pytest.raises(ResolutionError, match="cycle"):
        current_id("ent_a", entities)


def test_a_chain_longer_than_the_bound_is_refused_rather_than_truncated() -> None:
    """The bound reports corruption; it does not silently return the deepest id reached.

    Delete this and the loop can return whatever it had in hand when it ran out of hops. That
    id is not current, it looks current, the caller stores it, and the next merge makes it
    wrong again with nothing reporting either step.
    """
    length = MAX_FORWARD_DEPTH + 2
    chain = [entity(f"ent_{i}", merged_into=f"ent_{i + 1}", merged_at=NOW) for i in range(length)]
    entities = graph(*chain, entity(f"ent_{length}"))

    with pytest.raises(ResolutionError, match="longer than"):
        current_id("ent_0", entities)
    # And the boundary from the other side: a chain exactly at the bound still resolves, so
    # the guard is a bound rather than an off-by-one that refuses legitimate histories.
    short = [
        entity(f"s_{i}", merged_into=f"s_{i + 1}", merged_at=NOW) for i in range(MAX_FORWARD_DEPTH)
    ]
    assert (
        current_id("s_0", graph(*short, entity(f"s_{MAX_FORWARD_DEPTH}")))
        == f"s_{MAX_FORWARD_DEPTH}"
    )


def test_a_pointer_into_an_entity_that_is_not_in_the_graph_is_refused() -> None:
    """A dangling pointer is corruption and is reported as such.

    `er.canonical`'s self-referencing foreign key makes it impossible to store one, so meeting
    one means the row arrived another way. Delete this and the loop returns the last entity it
    could see, which is a stub presented as a survivor.
    """
    entities = graph(entity("ent_a", merged_into="ent_gone", merged_at=NOW))

    with pytest.raises(ResolutionError, match="not an entity in this graph"):
        current_id("ent_a", entities)


def test_an_entity_merged_into_itself_cannot_be_constructed() -> None:
    """The one cycle a single row can see, refused where a check constraint can also refuse it.

    Delete this and the constructor admits a row that resolves to nothing: following the
    pointer from it never reaches a surviving entity, and every id issued for it stops working.
    """
    with pytest.raises(ResolutionError, match="merged into itself"):
        entity("ent_a", merged_into="ent_a", merged_at=NOW)


def test_the_forwarding_pointer_and_its_timestamp_cannot_disagree() -> None:
    """Both halves, because either alone is a row two parts of the system read differently.

    An entity merged at no time reads as current to anything filtering on the timestamp; one
    merged into nothing at a time reads as merged to anything filtering on the pointer. Delete
    this and which entities are current depends on which column the query happened to use.
    """
    with pytest.raises(ResolutionError, match="have to agree"):
        CanonicalEntity(
            entity_id="ent_a",
            entity_type=EntityType.COMPANY,
            created_at=NOW,
            created_by="job:backfill",
            created_from=OPEN_RECORD,
            merged_into="ent_b",
        )
    with pytest.raises(ResolutionError, match="have to agree"):
        CanonicalEntity(
            entity_id="ent_a",
            entity_type=EntityType.COMPANY,
            created_at=NOW,
            created_by="job:backfill",
            created_from=OPEN_RECORD,
            merged_at=NOW,
        )


# ===================================================== a merge is reversible, and not audited
def test_clearing_the_forwarding_pointer_restores_both_entities_exactly() -> None:
    """A merge writes one pointer, so undoing it is clearing one pointer, and the evidence is
    that both views come back byte for byte.

    Delete this and the rejected design becomes available: rewrite every alias, identifier and
    link to name the survivor, which reads faster and destroys the only record of which entity
    each row was observed against. An unmerge then has nothing to restore from.
    """
    entities, links, aliases, identifiers = merged_pair()
    both = reaching(OPEN_RECORD, RESTRICTED_RECORD)

    unmerged = graph(entity("ent_open"), entity("ent_restricted", created_from=RESTRICTED_RECORD))
    before_open = resolved_view(
        "ent_open",
        entities=unmerged,
        links=links,
        aliases=aliases,
        identifiers=identifiers,
        reaches=both,
    )
    before_restricted = resolved_view(
        "ent_restricted",
        entities=unmerged,
        links=links,
        aliases=aliases,
        identifiers=identifiers,
        reaches=both,
    )

    merged = resolved_view(
        "ent_open",
        entities=entities,
        links=links,
        aliases=aliases,
        identifiers=identifiers,
        reaches=both,
    )
    assert merged is not None
    assert len(merged.members) == 2

    # The unmerge: the same rows, with the pointer cleared and nothing else touched.
    restored = graph(entity("ent_open"), entity("ent_restricted", created_from=RESTRICTED_RECORD))
    after_open = resolved_view(
        "ent_open",
        entities=restored,
        links=links,
        aliases=aliases,
        identifiers=identifiers,
        reaches=both,
    )
    after_restricted = resolved_view(
        "ent_restricted",
        entities=restored,
        links=links,
        aliases=aliases,
        identifiers=identifiers,
        reaches=both,
    )

    assert after_open == before_open
    assert after_restricted == before_restricted


def test_nothing_in_the_canonical_model_records_who_merged_or_on_what_evidence() -> None:
    """An honest assertion of a gap rather than a guard on a feature.

    `merged_at` says when. There is no approver, no evidence and no pre-image anywhere in this
    model, so a merge cannot be explained after the fact. That is M14.5.1 and M14.5.4 and it is
    not built. This test exists so that the gap is a failing assertion the day somebody adds
    half of it, and so that nobody reading `merged_at` concludes the merge is audited.
    """
    names = {field.name for field in dataclass_fields(CanonicalEntity)}
    assert "merged_at" in names
    assert "merged_by" not in names
    assert "merge_evidence" not in names
    assert not any("approv" in name or "evidence" in name for name in names)


def test_a_canonical_entity_has_nowhere_to_put_a_field_value() -> None:
    """The structural half of "merging two records merges two permission surfaces".

    The expected set is a literal here, not read off the class, so that a field added later
    fails rather than being absorbed.

    Delete this and a `name` column arrives on the canonical row, sourced from whichever member
    is most trusted. It has no permission surface of its own, so everybody who reaches any
    member reaches it, and a merge widens the whole estate's reach one entity at a time.
    """
    names = {field.name for field in dataclass_fields(CanonicalEntity)}
    assert names == {
        "entity_id",
        "entity_type",
        "created_at",
        "created_by",
        "created_from",
        "merged_into",
        "merged_at",
    }


# ============================================================== the hashed join key (M14.1.3)
def test_the_same_value_under_two_peppers_gives_two_different_join_keys() -> None:
    """Proof that the pepper is actually in the digest rather than merely required.

    A determinism test alone passes against a plain sha256 with the pepper checked and thrown
    away, which is a table of reversible digests behind an argument that says otherwise. This
    is the test that discriminates.
    """
    one = identifier_hash(IdentifierKind.EMAIL, "someone@example.com", pepper="pepper-one")
    two = identifier_hash(IdentifierKind.EMAIL, "someone@example.com", pepper="pepper-two")

    assert one != two


def test_two_kinds_of_join_key_with_the_same_value_do_not_join() -> None:
    """The kind is hashed with the value, not merely stored beside it.

    Delete this and the kind can be dropped from the material, which is one line and reads as
    simplification. A phone number and a tax id that happen to be the same digits then produce
    one digest, and stage one of the cascade merges two companies on a coincidence.
    """
    digits = "201912345"
    as_phone = identifier_hash(IdentifierKind.PHONE, digits, pepper=PEPPER)
    as_tax_id = identifier_hash(IdentifierKind.TAX_ID, digits, pepper=PEPPER)

    assert as_phone != as_tax_id


def test_the_same_kind_value_and_pepper_give_the_same_join_key() -> None:
    """The positive case, without which every test above is satisfied by a random digest.

    A join key that is not stable joins nothing, and the failure is silent: the cascade simply
    never matches on identifiers and falls through to the weaker stages.
    """
    first = identifier_hash(IdentifierKind.UEN, "201912345K", pepper=PEPPER)
    second = identifier_hash(IdentifierKind.UEN, "201912345K", pepper=PEPPER)

    assert first == second
    assert len(first) == 64


def test_a_join_key_cannot_be_computed_without_a_pepper() -> None:
    """An empty pepper turns the HMAC into a plain digest, and a plain digest of an email is
    reversible by anybody with a list of emails.

    Delete this and a deployment that forgot to configure the secret produces a working,
    silently reversible identifier table. Nothing else in the system would report it: the
    digests are the right length and they join correctly.
    """
    with pytest.raises(ResolutionError, match="needs a pepper"):
        identifier_hash(IdentifierKind.EMAIL, "someone@example.com", pepper="")


def test_an_identifier_cannot_be_built_from_anything_that_is_not_a_digest() -> None:
    """The privacy rule for join keys, as a shape rather than as a convention.

    Delete this and `key_hash` accepts whatever it is given, which one day is the address
    itself, passed by a caller who had it in hand and had not read the docstring. The column
    carries the same check, so the row would still be refused, but not until it reached a
    database, and not in any test.
    """
    with pytest.raises(ResolutionError, match="not a sha256 digest"):
        Identifier(
            entity_id="ent_a",
            kind=IdentifierKind.EMAIL,
            key_hash="someone@example.com",
            source=OPEN_RECORD,
            first_seen_at=NOW,
        )
    # An uppercase digest is refused too: one digest has one spelling, or two rows for one join
    # key differ by case and neither finds the other.
    with pytest.raises(ResolutionError, match="not a sha256 digest"):
        Identifier(
            entity_id="ent_a",
            kind=IdentifierKind.EMAIL,
            key_hash=identifier_hash(IdentifierKind.EMAIL, "a@b.example", pepper=PEPPER).upper(),
            source=OPEN_RECORD,
            first_seen_at=NOW,
        )


def test_an_identifier_has_nowhere_to_put_the_value_it_identifies() -> None:
    """There is no `value` field and there is not going to be one.

    Delete this and one arrives for a review queue that wanted to show a human what matched.
    `er.identifier` then holds every email address and phone number in the estate, in a table
    designed on the assumption that it holds none.
    """
    names = {field.name for field in dataclass_fields(Identifier)}
    assert names == {"entity_id", "kind", "key_hash", "source", "first_seen_at"}


# =================================================================== the observed name (M14.1.2)
def test_an_alias_keeps_the_form_that_was_observed_rather_than_a_normalised_one() -> None:
    """M14.1.2 asks for every observed name form, which means the punctuation and the case too.

    Delete this and the constructor may fold the name on the way in, because that is what makes
    the alias table joinable. A table holding only normalised forms cannot be re-normalised when
    M14.2's rules change, and the evidence a human reviewer is shown stops being what the source
    actually said.
    """
    observed = "  ACME  Pte. Ltd.  "
    kept = alias("ent_a", observed, OPEN_RECORD)

    assert kept.name == observed


def test_an_alias_longer_than_the_column_can_key_on_is_refused_before_it_reaches_one() -> None:
    """The observed name is part of `er.alias`'s primary key, so its length is an index-tuple
    bound rather than a tidiness one.

    Delete this and the refusal happens at the database, during a backfill, at whatever hour the
    longest name in the estate arrives, with the row lost and the job's error naming a column
    rather than a rule.
    """
    with pytest.raises(ResolutionError, match="longer than"):
        alias("ent_a", "x" * (MAX_ALIAS_CHARS + 1), OPEN_RECORD)
    # And the boundary from the other side, so the guard is a bound rather than an off-by-one.
    assert alias("ent_a", "x" * MAX_ALIAS_CHARS, OPEN_RECORD).name


# ================================================================= the membership (M14.1.4)
def test_a_link_confidence_outside_nought_to_one_is_refused() -> None:
    """A match score is a probability, and a figure outside the unit interval is not one.

    Delete this and a weight table exported from calibration can write raw log-odds into the
    confidence column. Everything downstream that compares it against a threshold then compares
    against a number on a different scale, and the comparison is silently always true.
    """
    for bad in (1.5, -0.1):
        with pytest.raises(ResolutionError, match="not a share"):
            link("ent_a", OPEN_RECORD, confidence=bad)
    assert link("ent_a", OPEN_RECORD, confidence=0.0).confidence == 0.0
    assert link("ent_a", OPEN_RECORD, confidence=1.0).confidence == 1.0


def test_a_member_is_the_whole_triple_so_two_records_differing_in_any_part_are_two_members() -> (
    None
):
    """Freshdesk company 42 and Xero contact 42 are different companies, and Freshdesk ticket 42
    and Freshdesk company 42 both exist.

    Delete this and the identity of a member can be narrowed to the source id, which reads as
    simplification and joins two unrelated records by a coincidence of integers. A reader who
    reaches one would then be handed the other's rows.
    """
    base = SourceRef(source="freshdesk", entity="company", source_id="42")
    assert base != SourceRef(source="xero", entity="company", source_id="42")
    assert base != SourceRef(source="freshdesk", entity="ticket", source_id="42")
    assert base != SourceRef(source="freshdesk", entity="company", source_id="43")
    assert base == SourceRef(source="freshdesk", entity="company", source_id="42")


def test_a_member_with_a_blank_part_is_refused() -> None:
    """Two members with a blank source id compare equal and become one.

    Delete this and a source that returns an empty id for a record it could not identify
    produces members that all collide, so a reader reaching any one of them reaches every other
    unidentified record in that source.
    """
    with pytest.raises(ResolutionError, match="no source id"):
        SourceRef(source="freshdesk", entity="company", source_id="  ")
    with pytest.raises(ResolutionError, match="not a connector name"):
        SourceRef(source="", entity="company", source_id="42")


# ============================================================ the resolved alias view (M14.1.6)
def test_a_name_observed_before_a_merge_resolves_to_the_surviving_entity() -> None:
    """M14.1.6, and the reason it has to exist at all: a merge rewrites no alias row.

    Delete this and the forwarding is done by rewriting `er.alias.entity_id` instead, which
    loses the only record of which entity each name was observed against and makes an unmerge
    unperformable.
    """
    entities, _links, aliases, _identifiers = merged_pair()

    rows = resolved_aliases(
        entities=entities,
        aliases=aliases,
        reaches=reaching(OPEN_RECORD, RESTRICTED_RECORD),
    )

    assert {row.entity_id for row in rows} == {"ent_open"}
    assert {row.alias.entity_id for row in rows} == {"ent_open", "ent_restricted"}
    assert {row.alias.name for row in rows} == {"Acme Support", "Acme Holdings Pte Ltd"}


def test_the_resolved_alias_listing_omits_names_observed_on_records_the_reader_cannot_see() -> None:
    """A listing is where the merge leak is quietest, because the natural implementation
    resolves and never filters.

    Delete this and the forwarding view becomes the way round the member filter: ask for every
    alias rather than for one entity's view, and the restricted record's name comes back with
    the survivor's id attached to it.
    """
    entities, _links, aliases, _identifiers = merged_pair()

    rows = resolved_aliases(entities=entities, aliases=aliases, reaches=reaching(OPEN_RECORD))

    assert [row.alias.name for row in rows] == ["Acme Support"]


def test_an_alias_whose_entity_has_no_survivor_is_omitted_rather_than_raised_on() -> None:
    """The listing and the lookup disagree on a corrupt chain, deliberately.

    A view that raised would take out every healthy entity's query because one pair of rows is
    corrupt, and `er.resolved_alias` cannot raise at all: its anchor is the set of unmerged
    entities, so a cycle is unreachable from it. Delete this and the Python half starts raising
    where the SQL half omits, and the two stop describing one behaviour.
    """
    entities = graph(
        entity("ent_a", merged_into="ent_b", merged_at=NOW),
        entity("ent_b", merged_into="ent_a", merged_at=NOW),
        entity("ent_healthy"),
    )
    aliases = [
        alias("ent_a", "In A Cycle", OPEN_RECORD),
        alias("ent_healthy", "Perfectly Fine", RESTRICTED_RECORD),
    ]

    rows = resolved_aliases(
        entities=entities, aliases=aliases, reaches=reaching(OPEN_RECORD, RESTRICTED_RECORD)
    )

    assert [row.alias.name for row in rows] == ["Perfectly Fine"]
