"""The client-visible audit view: the ledger, filtered to one reader, with nothing left over.

**What breaks without it.** The audit trail exists and no client or auditor can read it. The
export (M24.1.6) is a contiguous window of everything, refused if filtered, because a filtered
run of entries is not a chain; it is the right artefact for a regulator and the wrong one to
put in front of a person who may see some of the ledger and not the rest. Without a filtered
view, the only way to answer "who gave her access to that" is to hand somebody the whole
ledger, which is the permission map for the entire company.

This is where the ledger meets a person, so the rules that apply everywhere else apply here
with the volume turned up.

**An entry the reader may not see is absent, not refused, and never counted.** It does not
appear, nothing is raised, no placeholder is emitted and no number anywhere says how many
there were. `brain.core.errors` collapses DENIED into ABSENT on the message side and
`brain.core.redaction` drops the record on the data side; this module does the same by
skipping, and the invariant beside it asserts the strong form: the view of a ledger
containing entries the reader may not see is byte-identical to the view of a ledger where
those entries were never written.

**The view must not become a way to enumerate.** That is a harder rule than it sounds,
because a ledger is numbered. A row carrying `seq` hands the reader the gaps directly:
seeing 100, 103 and 107 is being told that four entries exist that they may not read, which
is the hidden count the whole design refuses to emit. So the row carries no `seq`, no
`entry_hash` and no `prev_hash`; the chain positions stay on the auditor's side of the wall,
in the export. Pages are filled to the limit from the entries the reader may see, so a short
page means there are no more visible entries and never that some were removed from it, and
the cursor is a position in the visible order rather than a row number.

**Filters are exact matches on closed vocabularies.** Action, subject kind, actor, and a date
range. There is no free-text filter and there is no substring match on an actor id, because a
search box over a ledger is a search engine over a permission map: run "salary" against
everyone's entries and the shape of what you cannot see is the answer.

**One capability may be shown and a list may not.** Rupash decided that on 5 September, and
`redact_details` already implements exactly it: `read:client.name` survives, a list of
capabilities becomes `<redacted>`. Nothing here loosens that, and nothing here aggregates
either: there is no method that collects the capabilities across rows, because assembling
them is rebuilding the map the single-capability rule exists to prevent.

Scope: domain logic, like `brain.audit.ledger`. Nothing here opens a connection. It takes the
entries it is given, which means whatever loads them still owes this view a window; see the
report for what that costs.

Task ids: M24.1.5
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from brain.audit.ledger import (
    IDENTIFIER,
    SUBJECT_KINDS,
    AuditAction,
    AuditEntry,
    redact_details,
)
from brain.core.entitlement import Capability, EntitlementSet

#: The noun the audit capabilities are written against: `read:audit.principal`,
#: `read:audit.grant`, and `read:audit.*` for somebody who may read all of it.
AUDIT_NOUN: Final = "audit"

#: The ledger's own identifier grammar, compiled once. Imported rather than restated, for
#: the reason `ledger._is_capability` gives about importing the capability grammar: two
#: definitions of one shape drift, and the drift is silent in the permissive direction.
_IDENTIFIER_RE: Final = re.compile(IDENTIFIER)

#: One capability per subject kind, built once. Built from `SUBJECT_KINDS` rather than
#: listed, so a new subject kind cannot arrive with no capability governing it and quietly
#: become readable by whoever holds the nearest wildcard.
CAPABILITY_BY_KIND: Final[Mapping[str, Capability]] = MappingProxyType(
    {kind: Capability(value=f"read:{AUDIT_NOUN}.{kind}") for kind in sorted(SUBJECT_KINDS)}
)

#: How many rows one page may carry. A ceiling rather than a suggestion: a page of fifty
#: thousand is a way of asking for the whole ledger through a screen built to show a
#: fortnight of it.
MAX_PAGE_SIZE: Final = 200
DEFAULT_PAGE_SIZE: Final = 50


class AuditFilter(BaseModel):
    """What a reader may narrow the view by. Closed vocabularies and a date range.

    `extra="forbid"` is load-bearing rather than tidy. It is what stops a free-text field
    being added to this model later by somebody making a screen more useful, which is how a
    ledger acquires a search box.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Empty means every action, not no action. The same reading as an empty scope.
    actions: frozenset[AuditAction] = frozenset()
    subject_kinds: frozenset[str] = frozenset()
    #: Exact actor references. Never a substring: `u_wei` matching `u_weiling` turns the
    #: filter into a prefix search over principal ids, and a prefix search over a ledger
    #: enumerates the people in it.
    actors: frozenset[str] = frozenset()
    #: Half-open: `since` is included, `until` is not. Two consecutive days therefore cover
    #: each entry once, which an inclusive upper bound does not.
    since: datetime | None = None
    until: datetime | None = None

    @field_validator("subject_kinds")
    @classmethod
    def _known_kinds(cls, v: frozenset[str]) -> frozenset[str]:
        unknown = sorted(v - SUBJECT_KINDS)
        if unknown:
            msg = f"unknown subject kind(s) {unknown}; known kinds are {sorted(SUBJECT_KINDS)}"
            raise ValueError(msg)
        return v

    @field_validator("actors")
    @classmethod
    def _actor_shape(cls, v: frozenset[str]) -> frozenset[str]:
        """Refuse anything that is not an identifier.

        This is the rule that keeps the filter from becoming a query language. An actor
        filter of `%wei%` or `u_wei*` is a search; an actor filter of `u_weiling` is a
        reference to somebody the reader already knows the name of.
        """
        bad = sorted(a for a in v if not _IDENTIFIER_RE.match(a))
        if bad:
            msg = (
                f"actor filter(s) {bad} are not identifiers; a filter is a reference, not a search"
            )
            raise ValueError(msg)
        return v

    @field_validator("since", "until")
    @classmethod
    def _tz_aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            msg = "a filter bound must be timezone-aware; a naive one is a silent bug"
            raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.since is not None and self.until is not None and self.until <= self.since:
            msg = "until must be after since; an empty range is a filter nobody meant to write"
            raise ValueError(msg)
        return self

    def matches(self, entry: AuditEntry) -> bool:
        """Whether the entry falls inside this filter. Says nothing about entitlement."""
        if self.actions and entry.action not in self.actions:
            return False
        if self.subject_kinds and entry.subject.partition(":")[0] not in self.subject_kinds:
            return False
        if self.actors and entry.actor_id not in self.actors:
            return False
        if self.since is not None and entry.at < self.since:
            return False
        return not (self.until is not None and entry.at >= self.until)


class AuditRow(BaseModel):
    """One entry, as a person reads it.

    The field list is the whole security argument of this module, so it is worth saying what
    is missing and why.

    No `seq`, `entry_hash` or `prev_hash`. Those are the chain, and the chain is a numbering:
    a reader who sees the numbers of the entries they may read has been told how many lie
    between them. `entry_hash` and `prev_hash` are worse than `seq`, because two of them
    together say whether two visible entries are adjacent, which is the same disclosure
    arrived at sideways.

    No `ent_hash`. It is a digest of the actor's whole reach, carried so a verifier can tell
    two entitlements apart without either being written down. To a reader it is a grouping
    key: every entry sharing a hash was written under one reach, which is the permission map
    with the labels rubbed off. It stays in the ledger and in the export, where the audience
    is a verifier rather than an audience.

    `details` is what the ledger already holds, run through `redact_details` a second time.
    `AuditEntry` refuses to load a row carrying a value, so this is the second of two locks;
    it costs one dict comprehension and the failure it would otherwise permit is permanent.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    at: datetime
    action: AuditAction
    actor_id: str
    subject_kind: str
    subject_id: str
    details: dict[str, str] = Field(default_factory=dict)


class AuditPage(BaseModel):
    """A page of rows, and where to continue.

    There is no `total`, and its absence is the design rather than an omission.
    `brain.api.Page` carries an optional one and says a count behind a permission predicate
    costs a full scan; here it costs more than that. A total counts the entries matching the
    filter, the rows count the ones the reader may see, and the subtraction is precisely the
    hidden count that no part of this system may emit. `extra="forbid"` means one cannot be
    attached later by a screen that wanted to show "showing 12 of 40".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: tuple[AuditRow, ...] = ()
    #: None when there are no further rows this reader may see. Never a signal about rows
    #: they may not.
    next_cursor: str | None = None


class AuditView:
    """The ledger as one reader may read it.

    Takes a sequence of entries rather than an `AuditChain`, deliberately. A filtered view is
    not a chain and must not be mistaken for one: `brain.audit.export` refuses to export a
    filtered selection precisely because the links no longer meet. Accepting a chain here
    would invite somebody to hand the result of a view to the exporter.

    Rejected: verifying the chain before rendering, and refusing to show a ledger that does
    not verify. A break would then hide the audit trail from every client at exactly the
    moment somebody needed to read it, and the view is not where a break is reported.
    `brain.audit.verify` is.
    """

    def __init__(
        self,
        entries: Sequence[AuditEntry],
        *,
        reader: EntitlementSet,
        now: datetime,
    ) -> None:
        # Sorted by (at, entry_hash). Time is the order a person reads a ledger in, and the
        # digest is the tie-break: entries can share a timestamp, and a tie broken by
        # position would put a chain sequence number back into the ordering the cursor
        # encodes. The digest is unique by construction; `uq_audit_entry_entry_hash` says so
        # in the table.
        self._entries: tuple[AuditEntry, ...] = tuple(sorted(entries, key=_order))
        self._reader = reader
        # No default of `datetime.now(UTC)`. An expired principal holds nothing, and the
        # check that enforces that takes the moment as an argument; defaulting it here would
        # be the bug the contractor fixtures exist to catch.
        self._now = now

    def page(
        self,
        criteria: AuditFilter | None = None,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
    ) -> AuditPage:
        """One page of the entries this reader may see, oldest first.

        The page is filled from visible rows, so its length says nothing about what was
        withheld: a page shorter than `limit` means the reader has reached the end of what
        they may see. `next_cursor` is present exactly when at least one further visible row
        exists, which is a fact about their own view and not about anybody else's.
        """
        if not 1 <= limit <= MAX_PAGE_SIZE:
            # A caller error, raised identically whatever the ledger contains, so it cannot
            # be used to learn anything about an entry.
            msg = f"limit must be between 1 and {MAX_PAGE_SIZE}, not {limit}"
            raise ValueError(msg)

        after = _decode_cursor(cursor) if cursor is not None else None
        rows: list[AuditRow] = []
        last_key: tuple[datetime, str] | None = None
        more = False

        for entry in self._visible(criteria or AuditFilter()):
            if after is not None and _order(entry) <= after:
                continue
            if len(rows) == limit:
                # One visible row beyond the page. Found rather than counted: the loop stops
                # here, so nothing anywhere holds a number of remaining rows that could be
                # returned by accident.
                more = True
                break
            rows.append(_row(entry))
            last_key = _order(entry)

        next_cursor = _encode_cursor(last_key) if more and last_key is not None else None
        return AuditPage(rows=tuple(rows), next_cursor=next_cursor)

    def _visible(self, criteria: AuditFilter) -> Iterator[AuditEntry]:
        """Every entry this reader may see, in view order.

        One pass, one predicate per entry, and no branch that does extra work for an entry
        the reader may not see. That is not an optimisation. An entry the reader may not see
        has to be indistinguishable from an entry that was never written, and a second lookup
        taken only on the denied path is a distinction that survives every test asserting the
        output is identical.
        """
        for entry in self._entries:
            if not criteria.matches(entry):
                continue
            if not self._may_see(entry):
                continue
            yield entry

    def _may_see(self, entry: AuditEntry) -> bool:
        """Whether this reader is entitled to this entry.

        Two ways in, and no third.

        **The entry is about the reader.** A person may read their own audit trail: it is
        what a subject access request asks for, and every fact in it is a fact about them.
        This is a decision rather than a mechanism falling out of the grant model, and it can
        be removed by deleting the first branch if it turns out a client should see nothing
        without an explicit grant.

        **A grant covers the entry's subject kind.** `read:audit.principal`, and
        `read:audit.*` for a reader who may see all of it, evaluated in the scope the grant
        carries. The scope is matched against the entry's own closed fields and nothing else,
        because the ledger deliberately holds no business attributes: a grant scoped to
        `department = maintenance` therefore matches no audit entry at all, since a clause
        over a field the row does not carry admits nothing. That fails closed, which is the
        right direction, and it means audit grants have to be scoped on what a ledger row
        actually has. Rejected: passing per-entry attributes in from outside so a
        departmental scope could be evaluated. That makes the visibility of an audit entry
        depend on a mapping the view cannot check, which is a permission decision taken by
        the caller.
        """
        if entry.subject == f"principal:{self._reader.principal_id}":
            return True
        capability = CAPABILITY_BY_KIND.get(entry.subject.partition(":")[0])
        if capability is None:
            # A subject kind with no capability governing it. Unreachable while
            # CAPABILITY_BY_KIND is built from SUBJECT_KINDS and the entry model validates
            # its subject against the same set; kept because the honest answer to a shape
            # nobody has decided about is no.
            return False
        scope = self._reader.scope_for(capability, self._now)
        if scope is None:
            return False
        return scope.matches(_scope_row(entry))


# ------------------------------------------------------------------- internals


def _order(entry: AuditEntry) -> tuple[datetime, str]:
    return (entry.at, entry.entry_hash)


def _row(entry: AuditEntry) -> AuditRow:
    kind, _, ident = entry.subject.partition(":")
    return AuditRow(
        at=entry.at,
        action=entry.action,
        actor_id=entry.actor_id,
        subject_kind=kind,
        subject_id=ident,
        details=redact_details(entry.details),
    )


def _scope_row(entry: AuditEntry) -> dict[str, Any]:
    """The fields an audit grant's scope may be written against.

    Four, all of them already closed or already a reference, and none of them a business
    attribute. A wider row would let an audit grant be scoped on something the ledger does
    not hold, which reads as working and admits nothing.
    """
    return {
        "action": entry.action.value,
        "subject_kind": entry.subject.partition(":")[0],
        "subject": entry.subject,
        "actor_id": entry.actor_id,
    }


def _encode_cursor(position: tuple[datetime, str]) -> str:
    """A position in the visible order, opaque but not secret.

    Carries the last row's timestamp, which the reader has just been shown, and its
    `entry_hash`, which they have not. The digest is the tie-break and it discloses nothing
    on its own: a chain position can be recovered from a digest only by holding the
    neighbouring entries' `prev_hash` values, which no row carries.

    Rejected: putting `seq` in the cursor. It is the obvious tie-break, it is one base64
    decode from the reader, and it would hand them the chain position of every row they can
    see - which is the gap count this module exists to withhold.

    Rejected: reusing `brain.api.encode_cursor`. Same shape, and it lives in the HTTP layer,
    which imports FastAPI. The audit package is domain logic and should not acquire a web
    framework to paginate a list.
    """
    at, tie = position
    payload = json.dumps({"at": at.isoformat(), "tie": tie}, sort_keys=True)
    return base64.urlsafe_b64encode(payload.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    """The position a cursor names.

    Raises on a malformed cursor rather than silently starting from the beginning, because a
    client bug that quietly re-serves page one looks like data loss to whoever is reading.
    The failure is a pure function of the string: it involves no entry, so it cannot take a
    different path, or a different amount of time, for an entry the reader may not see.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode())
        value = json.loads(raw)
        at = datetime.fromisoformat(value["at"])
        tie = value["tie"]
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, KeyError, ValueError) as exc:
        msg = "malformed cursor"
        raise ValueError(msg) from exc
    if not isinstance(tie, str) or at.tzinfo is None:
        msg = "malformed cursor"
        raise ValueError(msg)
    return (at, tie)
