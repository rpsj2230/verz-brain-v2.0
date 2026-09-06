"""The cassettes, checked for the properties that make a connector correct.

A cassette set with only happy paths produces connectors that have never been compiled
against failure. These assert the corpus keeps its teeth.

Task ids: M0.6.5, M38.4.1.1
"""

from __future__ import annotations

import pytest

from tests.fixtures.cassettes import (
    CASSETTES,
    Source,
    failures,
    for_source,
    limit_for,
)

pytestmark = pytest.mark.invariant


def test_every_source_has_at_least_one_recording() -> None:
    for s in Source:
        assert for_source(s), f"{s} has no cassette"


def test_failures_outnumber_nothing_and_cover_every_kind() -> None:
    """Rate limit, auth, server error, and the 200-carrying-an-error case. A set of only
    happy paths produces a connector that has never seen a failure."""
    f = failures()
    assert len(f) >= 5
    statuses = {c.status for c in f}
    assert 429 in statuses
    assert 401 in statuses
    assert 500 in statuses
    # the nastiest kind: HTTP success carrying an application error
    assert any(c.status == 200 for c in f)


def test_a_two_hundred_can_still_be_a_failure() -> None:
    """Lark returns code 0 inside a 200 for success and non-zero inside a 200 for
    failure. A connector checking only the HTTP status records an error as an empty
    result, and an empty result reads as fact."""
    lark = [c for c in for_source(Source.LARK_BASE) if c.status == 200]
    assert any(isinstance(c.body, dict) and c.body.get("code") not in (0, None) for c in lark)


def test_every_cassette_says_what_it_is_for() -> None:
    for c in CASSETTES:
        assert c.why.strip(), f"{c.cid} has no stated purpose"


def test_the_rate_limits_record_whether_they_can_be_raised() -> None:
    """The difference between a starting tier and a design constraint. Lark Base at
    100/minute cannot be raised, so it shapes the architecture rather than the invoice.

    Xero is the same kind of constraint and this file recorded the opposite until
    2026-09-06. Its 5,000 a day sits on the client's tenant, shared with every other
    integration they run, so there is no plan we can buy that moves it. `brain.ops.limits`
    had that right and this had it wrong, which is why the assertion below now runs against
    both records rather than only this one."""
    assert limit_for(Source.LARK_BASE).raisable is False
    assert limit_for(Source.XERO).raisable is False


#: Sources whose cassette `RateLimit` records something other than a rate, and are therefore
#: not comparable with `brain.ops.limits.SOURCE_CEILINGS` by raisability.
#:
#: Freshdesk is the only one and it is worth the exception. Its cassette entry is the
#: 300-record search ceiling, which is a bound on a *result set* and genuinely cannot be
#: raised; `ops.limits` records its 100-a-minute *rate*, which the vendor raises by plan to
#: 400 and 700. Both records are correct and they describe different quantities that happen
#: to share a field name.
#:
#: Listed rather than inferred, so that the day a real contradiction appears for Freshdesk it
#: is a deliberate edit here and not a silent pass.
NOT_A_RATE: frozenset[Source] = frozenset({Source.FRESHDESK})


def test_the_recordings_and_the_operational_ceilings_agree_about_what_can_be_raised() -> None:
    """**Two records of the same fact, and until this test nothing compared them.**

    `tests/fixtures/cassettes.py` is what connectors are built against;
    `brain.ops.limits.SOURCE_CEILINGS` is what the admission controller sizes budgets from.
    They disagreed about Xero for as long as both existed, and it surfaced only because one
    connector was written against both at once and its author noticed.

    A connector sized against a ceiling it believes is raisable will tell an operator to ask
    for an upgrade they cannot buy.

    **The first version of this test then found a second disagreement that was not one.**
    Freshdesk's cassette entry records a result-set ceiling and `ops.limits` records a rate,
    and forcing those two to agree would have been making a correct record wrong to satisfy a
    test. `NOT_A_RATE` names the exception with the reason, which is the honest shape: the
    comparison is only meaningful where both records describe the same quantity.

    Delete this and the two drift again, quietly, in whichever direction somebody edits."""
    from brain.ops.limits import SOURCE_CEILINGS

    operational = {ceiling.name: ceiling.raisable for ceiling in SOURCE_CEILINGS}

    disagreements = {
        source.value: (limit_for(source).raisable, operational[source.value])
        for source in Source
        if source not in NOT_A_RATE
        and source.value in operational
        and limit_for(source).raisable != operational[source.value]
    }

    assert not disagreements, (
        f"the recordings and the operational ceilings disagree (source: cassette, limits): "
        f"{disagreements}"
    )
    assert operational, "no operational ceilings were read, so this compared nothing"


def test_the_source_excluded_from_that_comparison_really_is_measuring_something_else() -> None:
    """The guard on the exception. An exclusion list is how a disagreement gets hidden, so
    the reason Freshdesk is in it has to be checked rather than asserted in a comment.

    Its cassette entry must be a count of records rather than a rate over a window, and its
    operational ceiling must be a rate. If either stops being true, the exemption is no
    longer justified and this fails rather than the comparison silently skipping a real
    contradiction."""
    from brain.ops.limits import SOURCE_CEILINGS

    recorded = limit_for(Source.FRESHDESK)
    ceiling = next(c for c in SOURCE_CEILINGS if c.name == "freshdesk")

    assert "record" in recorded.per, f"the cassette now records {recorded.per!r}, not a result set"
    assert ceiling.per_minute, "the operational ceiling is no longer a rate per minute"


def test_the_freshdesk_ceiling_is_recorded_as_a_ceiling() -> None:
    """300 is not a page size. The search API will not return a 301st record however you
    page, so a connector that assumes it can enumerate silently under-reports."""
    fresh = limit_for(Source.FRESHDESK)
    assert fresh.calls == 300
    assert "page size" in fresh.note.lower()


def test_a_retry_after_is_present_on_every_rate_limit_response() -> None:
    """Backing off without one means guessing, and guessing low burns the remaining
    budget faster."""
    for c in CASSETTES:
        if c.status == 429:
            assert "Retry-After" in c.headers, f"{c.cid} is a 429 with no Retry-After"


def test_absent_is_distinguishable_from_refused_and_unreachable() -> None:
    """Three outcomes a naive connector collapses into one empty list."""
    empty_ok = [c for c in CASSETTES if c.status == 200 and c.body == {"total": 0, "results": []}]
    assert empty_ok, "no cassette records a genuine absence"
    assert any(c.status >= 500 for c in CASSETTES), "no cassette records unreachable"
    assert any(isinstance(c.body, dict) and c.body.get("code") == 91403 for c in CASSETTES), (
        "no cassette records refused"
    )


def test_the_awkward_date_format_is_captured() -> None:
    """Xero returns .NET epoch strings, not ISO. A connector assuming ISO parses garbage
    without erroring, which is worse than failing."""
    xero = for_source(Source.XERO)
    assert any("/Date(" in str(c.body) for c in xero)
