"""Recorded connector responses, so connectors can be built before credentials exist.

This is what makes the thirty-day plan possible. Every connector is written and tested
against these, and wired to real credentials at go-live — otherwise the whole build waits
on someone finding a Xero API key.

A cassette is not a convenience mock. It records three things a hand-written mock always
gets wrong, and each has caused a real outage somewhere:

**The failure responses, not just the happy one.** Xero returns 429 with a Retry-After
header, and the correct behaviour is to say "I could not reach Xero" rather than answer
from memory. A mock that only knows success produces a connector that has never once been
compiled against failure.

**The real limits, verified rather than assumed.** Freshdesk search returns at most 300
records *ever* — not per page, not per request, but as a hard ceiling on the result set.
A connector written against a mock that pages forever will silently under-report and look
correct doing it.

**The pagination shape.** Every one of these APIs pages differently, and the difference is
where "we only ever saw the first 100 clients" comes from.

Task ids: M0.6.5, M38.4.1.1
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class Source(enum.StrEnum):
    XERO = "xero"
    LARK_BASE = "lark_base"
    FRESHDESK = "freshdesk"
    LARAVEL = "laravel"
    HUBSPOT = "hubspot"


@dataclass(frozen=True)
class RateLimit:
    """Verified against the vendor's documentation, not guessed.

    These numbers are why the architecture federates instead of syncing: a realistic day
    touches 5-20 records per question, and syncing everything would need 15,000-60,000
    Xero calls against a ceiling of 5,000.
    """

    source: Source
    calls: int
    per: str
    note: str
    raisable: bool


RATE_LIMITS: tuple[RateLimit, ...] = (
    RateLimit(Source.XERO, 5_000, "day per tenant", "Also 60/minute. Resets 00:00 NZT.", True),
    RateLimit(
        Source.LARK_BASE,
        100,
        "minute",
        "Permanently uncapped - Lark does not raise this on request, so it is a design "
        "constraint rather than a starting tier.",
        False,
    ),
    RateLimit(
        Source.FRESHDESK,
        300,
        "records per search, ever",
        "Not a page size. The search API will not return a 301st record however you "
        "page, so any connector that assumes it can enumerate is wrong.",
        False,
    ),
    RateLimit(Source.HUBSPOT, 10_000, "day", "Per app, per account.", True),
    RateLimit(Source.LARAVEL, 0, "no ceiling", "Our own system.", True),
)


@dataclass(frozen=True)
class Cassette:
    """One recorded exchange."""

    cid: str
    source: Source
    request: str
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None
    why: str = ""


CASSETTES: tuple[Cassette, ...] = (
    # ---- the happy paths ------------------------------------------------
    Cassette(
        cid="XERO-200-invoices",
        source=Source.XERO,
        request='GET /api.xro/2.0/Invoices?where=Contact.Name=="SNM Construction Pte Ltd"',
        status=200,
        headers={"X-DayLimit-Remaining": "4139", "X-MinLimit-Remaining": "58"},
        body={
            "Invoices": [
                {
                    "InvoiceID": "b1f2-0447",
                    "InvoiceNumber": "INV-2291",
                    "Contact": {"ContactID": "c-0447", "Name": "SNM Construction Pte Ltd"},
                    "AmountDue": "CANARY-INVOICE-Z9KRT",
                    "DueDate": "/Date(1794700800000+0000)/",
                    "Status": "AUTHORISED",
                }
            ]
        },
        why="Note the date format. Xero returns .NET epoch strings, not ISO, and a "
        "connector that assumes ISO parses garbage without erroring.",
    ),
    Cassette(
        cid="LARK-200-records",
        source=Source.LARK_BASE,
        request="GET /open-apis/bitable/v1/apps/{app}/tables/{tbl}/records?page_size=100",
        status=200,
        body={
            "code": 0,
            "data": {
                "has_more": True,
                "page_token": "eyJvZmZzZXQiOjEwMH0",
                "total": 412,
                "items": [
                    {
                        "record_id": "recSNM0447",
                        "fields": {
                            "Client": "SNM Construction Pte Ltd",
                            "Hours Remaining": 12,
                            "Contract Value": "CANARY-CONTRACT-7Q4XZ",
                            "Renewal": 1794700800000,
                        },
                    }
                ],
            },
        },
        why="Lark returns code 0 inside a 200 for success and a non-zero code inside a "
        "200 for failure. A connector checking only the HTTP status treats every error "
        "as a successful empty result.",
    ),
    Cassette(
        cid="FRESH-200-search",
        source=Source.FRESHDESK,
        request='GET /api/v2/search/tickets?query="company_id:447"',
        status=200,
        headers={"X-Search-Results-Count": "300", "X-RateLimit-Remaining": "1841"},
        body={
            "total": 300,
            "results": [
                {
                    "id": 88213,
                    "subject": "SSL renewal",
                    "status": 2,
                    "custom_fields": {"internal_note": "CANARY-TICKET-B6YHF"},
                }
            ],
        },
        why="total is 300 because 300 is the ceiling, not because there are 300. The "
        "true count is unknowable through this endpoint, and an answer that says '300 "
        "tickets' is wrong in a way nobody notices.",
    ),
    # ---- the failures, which are the point ------------------------------
    Cassette(
        cid="XERO-429",
        source=Source.XERO,
        request="GET /api.xro/2.0/Invoices",
        status=429,
        headers={"Retry-After": "1847", "X-DayLimit-Remaining": "0"},
        body={
            "Type": "https://developer.xero.com/documentation/api/errors",
            "Title": "Rate limit exceeded",
        },
        why="The daily ceiling, half an hour before reset. Correct behaviour is DEGRADED "
        "- say the source is unreachable - and never a cached figure presented as "
        "current.",
    ),
    Cassette(
        cid="XERO-401-expired",
        source=Source.XERO,
        request="GET /api.xro/2.0/Invoices",
        status=401,
        body={"Title": "Unauthorized", "Detail": "TokenExpired"},
        why="Distinct from 429 and must not be retried the same way. Retrying an expired "
        "token burns the rate limit and never succeeds.",
    ),
    Cassette(
        cid="LARK-200-code-permission",
        source=Source.LARK_BASE,
        request="GET /open-apis/bitable/v1/apps/{app}/tables/{tbl}/records",
        status=200,
        body={"code": 91403, "msg": "Forbidden", "data": {}},
        why="HTTP 200 carrying a permission failure. The bot's own token is scoped to "
        "base:record:read; anything wider comes back like this, and a connector reading "
        "only the status code records an empty table as fact.",
    ),
    Cassette(
        cid="FRESH-429",
        source=Source.FRESHDESK,
        request="GET /api/v2/tickets",
        status=429,
        headers={"Retry-After": "60"},
        body={"description": "Rate limit exceeded"},
        why="Freshdesk gives Retry-After in seconds; Xero gives it in seconds too but "
        "against a daily window. Same header, very different waits.",
    ),
    Cassette(
        cid="LARAVEL-500",
        source=Source.LARAVEL,
        request="GET /internal/clients/4471",
        status=500,
        body={"message": "Server Error"},
        why="Our own system failing. Still DEGRADED, still no substituted value - being "
        "in-house is not a reason to trust an error response.",
    ),
    Cassette(
        cid="HUBSPOT-200-empty",
        source=Source.HUBSPOT,
        request="GET /crm/v3/objects/companies/search",
        status=200,
        body={"total": 0, "results": []},
        why="Genuinely absent, as distinct from refused or unreachable. Three different "
        "outcomes that a naive connector collapses into one empty list.",
    ),
)


def for_source(source: Source) -> tuple[Cassette, ...]:
    return tuple(c for c in CASSETTES if c.source is source)


def failures() -> tuple[Cassette, ...]:
    """Everything that is not a plain success, including the 200s that carry errors."""
    return tuple(
        c
        for c in CASSETTES
        if c.status >= 400 or (isinstance(c.body, dict) and c.body.get("code", 0) not in (0, None))
    )


def limit_for(source: Source) -> RateLimit:
    return next(r for r in RATE_LIMITS if r.source is source)
