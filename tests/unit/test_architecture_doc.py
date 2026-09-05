"""The architecture document, checked where it states a fact that changes.

`docs/architecture.html` is a document of record: it is served in the console and it is what
somebody reads to learn the current position. Most of it is reasoning, which does not go
stale. A handful of sentences are counts and decisions, and those do.

The one this file exists for said "Six questions are open" long after four had been answered,
and named two of them specifically: whether production deploys on every push, and who holds
the vault's unseal pieces. Both were decided, one of them the same day the sentence was last
read. A stale number in a document of record is worse than no number, because a reader takes
it for the current position rather than for something nobody updated.

Prose cannot be tested and should not be. What can be tested is a number that also exists
somewhere machine-readable, so the count is carried in a `data-` attribute and held against
`docs/needs-rupash.md`, which is the file both the count and the console read from.

Task ids: M0.6.4
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from brain.docs_routes import _needs_count

ARCHITECTURE = Path(__file__).resolve().parents[2] / "docs" / "architecture.html"

#: Words for the small numbers this sentence can plausibly carry. A count is written out in
#: prose here rather than as a digit, so the test has to know both forms.
WORDS = {
    0: "none",
    1: "one",
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
}


def _stated_open_questions() -> str:
    """The word inside the marked span, or a failure naming what is missing."""
    html = ARCHITECTURE.read_text(encoding="utf-8")
    found = re.search(r'<span data-open-questions="[^"]*">([^<]+)</span>', html)
    if found is None:
        pytest.fail(
            "the architecture no longer marks its open-question count, so nothing can "
            "check it against the tracker"
        )
    return found.group(1).strip().lower()


def test_the_architecture_agrees_with_the_tracker_about_how_many_questions_are_open() -> None:
    """The sentence said six while three were open, and it named two decided ones as though
    they were live.

    Compared against `docs/needs-rupash.md` through the same function the console uses, so
    there is one source and not two opinions about it. Delete this and the number drifts
    again, silently, and the person who reads it is the client."""
    actual = _needs_count()

    assert _stated_open_questions() == WORDS[actual], (
        f"the architecture says {_stated_open_questions()!r} open questions and there are {actual}"
    )


def test_the_count_is_carried_somewhere_a_test_can_read() -> None:
    """The guard on the guard. A future edit that rewrites the sentence and drops the span
    leaves the test above with nothing to compare, and a test that quietly stops checking is
    the failure this whole file is about.

    `pytest.fail` inside the helper rather than a skip, for the same reason."""
    html = ARCHITECTURE.read_text(encoding="utf-8")

    assert 'data-open-questions=""' in html


@pytest.mark.parametrize(
    ("decided", "phrase"),
    [
        ("every push", "deploys on every push rather than from a tag"),
        ("five unseal pieces", "the vault split is five pieces, any three"),
    ],
)
def test_a_settled_decision_is_not_still_described_as_open(decided: str, phrase: str) -> None:
    """Two decisions this section used to list as open questions. Naming them here is not
    decoration: they are the two the document called out by name, so a rewrite that restores
    the old paragraph would put them back in the open list, and the count test alone would
    not notice because the count could still be right.

    Asserted as presence of the settled wording rather than absence of the old, because
    absence passes for a section somebody deleted entirely."""
    html = ARCHITECTURE.read_text(encoding="utf-8").lower()

    assert decided in html, f"the architecture no longer records that {phrase}"
