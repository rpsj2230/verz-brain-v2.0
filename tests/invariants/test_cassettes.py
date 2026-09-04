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
    100/minute cannot be raised, so it shapes the architecture rather than the invoice."""
    assert limit_for(Source.LARK_BASE).raisable is False
    assert limit_for(Source.XERO).raisable is True


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
