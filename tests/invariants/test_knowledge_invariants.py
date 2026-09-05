"""Rules the knowledge layer must never break. A failure here blocks deploy.

One of these matters more than the rest. A chunk carries the permissions of the document it
came from, and retrieval that forgot it would answer from a paragraph nobody was allowed to
read, in a fluent, cited, confident sentence that looks exactly like a correct answer. Nobody
files a bug against an answer that reads well, so the only defence is that the mistake cannot
be made.

The rest defend the same boundary at other points: widening is never a side effect of an
upload or of a new version, a withheld column cannot be reconstructed from visible ones, and
nothing in the package can report how much it withheld.

Task ids: M7.1.1, M7.1.3, M7.2.2, M7.3.1, M7.3.2, M7.4.3, M7.4.4, M7.4.5, M7.5.1, M7.5.2
"""

from __future__ import annotations

import dataclasses
import importlib
import pkgutil
import re
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel

import brain.knowledge
from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.scope import Scope
from brain.knowledge.chunking import (
    Block,
    BlockKind,
    Chunk,
    ChunkBounds,
    ChunkingError,
    chunk_document,
    permissions_of,
)
from brain.knowledge.columns import PRICE_LIST, project_row
from brain.knowledge.ingest import (
    IngestRefused,
    MediaType,
    ScanResult,
    ScanVerdict,
    admit_upload,
    assert_clean,
)
from brain.knowledge.item import KnowledgeItem, KnowledgeState, supersede
from brain.knowledge.visibility import (
    PROMOTION_CAPABILITY,
    KnowledgeVisibility,
    Visibility,
    VisibilityError,
    approve_promotion,
    propose_promotion,
)

pytestmark = pytest.mark.invariant

NOW = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
BOUNDS = ChunkBounds(size=120, overlap=20, minimum=40, lookback=40)
BODY = (
    "The maintenance retainer for SNM covers twelve hours a month. Unused hours do not "
    "roll over, and the block is invoiced in advance on the first working day."
)


def _ents(principal: str, *caps: str) -> EntitlementSet:
    return EntitlementSet(
        principal_id=principal,
        grants=tuple(Grant(capability=Capability(value=c), scope=Scope()) for c in caps),
    )


def _item(
    item_id: str = "k_retainer_terms",
    *,
    visibility: KnowledgeVisibility | None = None,
    state: KnowledgeState = KnowledgeState.PUBLISHED,
) -> KnowledgeItem:
    return KnowledgeItem(
        item_id=item_id,
        content=BODY,
        title="Retainer terms",
        visibility=visibility or KnowledgeVisibility.of_department("web"),
        owner_id="p_wei_ling",
        state=state,
    )


def _chunks(item: KnowledgeItem) -> tuple[Chunk, ...]:
    return chunk_document(item, [Block(kind=BlockKind.PROSE, text=BODY, start=0)], bounds=BOUNDS)


# ------------------------------------------------------- the one that matters
def test_a_chunk_carries_its_document_s_permissions() -> None:
    """The invariant of the whole milestone. A chunk whose scope, owner or level differs from
    its document's is a passage retrieval can reach on terms nobody set, and the answer drawn
    from it is indistinguishable from a correct one: fluent, cited, and about a paragraph the
    asker was never granted.

    Compared against the document's own fields rather than against `permissions_of`, so that
    breaking the copy cannot be hidden by the helper that performs it also being wrong.
    """
    item = _item()
    chunks = _chunks(item)
    assert chunks
    for chunk in chunks:
        assert chunk.scope == item.scope
        assert chunk.owner_id == item.owner_id
        assert chunk.visibility is item.visibility.level


def test_a_chunk_of_a_personal_document_admits_nobody_else() -> None:
    """The behavioural half of the rule above. Equal scopes are not the point; the point is
    that the predicate on the chunk still refuses the same rows. A chunk that carried a scope
    object which happened to compare equal while admitting everybody would satisfy the
    structural check and leak anyway."""
    item = _item(visibility=KnowledgeVisibility.personal("p_wei_ling"))
    for chunk in _chunks(item):
        assert chunk.scope.matches({"owner_id": "p_wei_ling"})
        assert not chunk.scope.matches({"owner_id": "p_priya"})
        assert not chunk.scope.is_unrestricted()


def test_a_chunk_cannot_be_built_outside_the_chunker() -> None:
    """The mechanism behind the two above. If a `Chunk` could be constructed anywhere, the
    guarantee would rest on every caller copying the document's permissions correctly, which is
    a convention rather than a guarantee. A re-indexing script is how that convention is
    broken, and it is broken quietly."""
    with pytest.raises(ChunkingError, match="may only be built by"):
        Chunk(
            chunk_id="k_retainer_terms.0000",
            document_id="k_retainer_terms",
            ordinal=0,
            text=BODY[:40],
            start=0,
            end=40,
            kind=BlockKind.PROSE,
            permissions=permissions_of(_item()),
        )


def test_a_replaced_document_is_never_chunked_into_the_index() -> None:
    """Its passages would answer questions beside the version that replaced them, carrying the
    older document's verification badge. Nothing links an index entry to a state it acquired
    after the entry was written, so the only place to refuse is here."""
    with pytest.raises(ChunkingError, match="must not be chunked"):
        _chunks(_item(state=KnowledgeState.SUPERSEDED))


# ------------------------------------------------- widening is never a side effect
def test_uploading_never_widens_the_scope_it_lands_in() -> None:
    """Without this the upload form is the promotion path: one participant, no approver, no
    review date. The architecture's rule is that widening is a deliberate act, and an upload is
    not a deliberate act about visibility."""
    from brain.knowledge.visibility import admit_upload as admit_visibility

    with pytest.raises(VisibilityError):
        admit_visibility(Visibility.COMPANY, uploader_department="web")
    assert admit_visibility(None, uploader_department="web") is Visibility.DEPARTMENT


def test_a_new_version_never_reaches_further_than_the_one_it_replaces() -> None:
    """Supersession is the ordinary weekly mechanism, so if it could widen, every control in
    `brain.knowledge.visibility` would be optional: upload version two of the department SOP at
    company visibility and it is published to 126 people.

    The successor is a draft, because that is the shape the attempt actually takes: an
    unverified company-scoped item cannot be published directly, so the way round the gate is
    to draft one and let supersession carry it."""
    predecessor = _item("k_sop_v1")
    successor = _item(
        "k_sop_v2",
        visibility=KnowledgeVisibility.company(owner_id="p_wei_ling"),
        state=KnowledgeState.DRAFT,
    )
    with pytest.raises(VisibilityError, match="not the promotion path"):
        supersede(predecessor, successor)


def test_a_promotion_needs_a_second_person_who_holds_the_capability() -> None:
    """Both halves are the gate. Self-approval leaves an audit trail that reads correctly and
    has one participant; an approver without the grant makes the step a formality any account
    can perform. There is no deny list anywhere here: the refusal is the absence of a grant."""
    proposal = propose_promotion(
        item_id="k_retainer_terms",
        from_level=Visibility.DEPARTMENT,
        to_level=Visibility.COMPANY,
        proposer_id="p_wei_ling",
        owner_id="p_wei_ling",
        review_by=NOW + timedelta(days=90),
        reason="every team quotes from this",
        now=NOW,
    )
    with pytest.raises(VisibilityError):
        approve_promotion(
            proposal,
            approver_id="p_wei_ling",
            entitlement=_ents("p_wei_ling", PROMOTION_CAPABILITY.value),
            now=NOW,
        )
    with pytest.raises(VisibilityError):
        approve_promotion(
            proposal,
            approver_id="p_priya",
            entitlement=_ents("p_priya", "read:client.name"),
            now=NOW,
        )


# ------------------------------------------------------ field level and subtraction
def test_a_withheld_column_cannot_be_reconstructed_from_visible_ones() -> None:
    """The subtraction rule, applied to columns. Classifying the cost while leaving the sell
    price and the margin visible hides nothing at all, and the person doing the arithmetic has
    done nothing wrong because both numbers were shown to them."""
    row = {"sku": "PKG-1", "sell_price": 1200, "cost": 400, "margin": 800}
    view = project_row(
        PRICE_LIST,
        row,
        entitlement=_ents(
            "p_wei_ling",
            "read:price_list.sku",
            "read:price_list.sell_price",
            "read:price_list.margin",
        ),
    )
    assert "cost" not in view.values
    assert "margin" not in view.values
    assert view.values["sell_price"] == 1200


def test_a_column_nobody_classified_is_withheld() -> None:
    """Default-deny, inherited from the redactor and asserted here because this is a second
    door onto it. A column that is over-returned looks exactly like a column meant to be
    public, and the only way to notice is for the wrong person to read it."""
    view = project_row(
        PRICE_LIST,
        {"sku": "PKG-1", "internal_note": "renewal at risk"},
        entitlement=_ents("p_wei_ling", "read:price_list.sku"),
    )
    assert "internal_note" not in view.values


# ------------------------------------------------------- absence has no shape
#: Field names that would report how much was withheld. Matched against every field of every
#: dataclass and model in the package, because a count of hidden items is the one thing none of
#: these types may ever carry, and it would arrive as a helpful addition rather than as a leak.
_COUNTING_NAMES = re.compile(
    r"(?:^|_)(count|total|hidden|omitted|excluded|withheld|remaining|suppressed)(?:$|_)"
)


def _package_modules() -> list[str]:
    return [
        f"brain.knowledge.{name}"
        for _finder, name, _pkg in pkgutil.iter_modules(brain.knowledge.__path__)
    ]


def test_nothing_in_the_package_can_report_how_much_was_withheld() -> None:
    """A count of hidden items hands over the hidden set by subtraction: "12 of 40 results" is
    "28 you may not see", and the asker has learnt the size of what they were refused. This is
    structural rather than a review habit, because the field always arrives as a helpful
    addition to a console screen and never as a leak."""
    offenders: list[str] = []
    for module_name in _package_modules():
        module = importlib.import_module(module_name)
        for attribute in vars(module).values():
            if not isinstance(attribute, type):
                continue
            if getattr(attribute, "__module__", "") != module_name:
                continue
            names: list[str] = []
            if dataclasses.is_dataclass(attribute):
                names = [f.name for f in dataclasses.fields(attribute)]
            elif issubclass(attribute, BaseModel):
                names = list(attribute.model_fields)
            offenders.extend(
                f"{module_name}.{attribute.__name__}.{name}"
                for name in names
                if _COUNTING_NAMES.search(name)
            )
    assert offenders == [], f"these fields could carry a count of hidden items: {offenders}"


def test_every_module_in_the_package_says_which_tasks_it_carries() -> None:
    """A module with no task ids cannot be traced back to the decision that asked for it, and
    the traceability sweep reads these lines. A file that nothing claims is a file nobody
    reviews against a requirement."""
    for module_name in _package_modules():
        module = importlib.import_module(module_name)
        doc = module.__doc__ or ""
        assert doc.strip(), f"{module_name} has no docstring"
        assert "Task ids:" in doc, f"{module_name} names no task ids"


# ------------------------------------------------------- the ingestion ordering
def test_an_unscannable_file_is_never_treated_as_clean() -> None:
    """Failing open here means every file crafted to defeat a scanner is also a file that
    skips it, and the parser is exactly what such a file is aimed at. "We could not scan it"
    is not a verdict and must not be read as one."""
    upload = admit_upload(
        filename="sop.pdf", declared_type=MediaType.PDF.value, content=b"%PDF-1.7\ncontent"
    )
    with pytest.raises(IngestRefused):
        assert_clean(
            upload,
            ScanResult(digest=upload.digest, verdict=ScanVerdict.UNSCANNABLE, scanner="clamd"),
        )


def test_a_scan_verdict_is_bound_to_the_bytes_it_was_reached_about() -> None:
    """A verdict recorded against a filename or an upload id survives the content behind it
    changing, so a clean scan of version one becomes a clean scan of version two. The digest is
    the only thing that cannot be reused."""
    upload = admit_upload(
        filename="sop.pdf", declared_type=MediaType.PDF.value, content=b"%PDF-1.7\ncontent"
    )
    with pytest.raises(IngestRefused, match="not a verdict about these"):
        assert_clean(
            upload,
            ScanResult(digest="0" * 64, verdict=ScanVerdict.CLEAN, scanner="clamd"),
        )
