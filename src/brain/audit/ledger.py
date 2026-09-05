"""The audit ledger: hash-chained, append-only, and deliberately incurious.

Two failures this module exists to prevent.

**A ledger nobody can trust.** A table that is append-only by convention is one UPDATE
away from saying whatever the person holding the database password wants it to say, and
nothing about the table afterwards reveals that it happened. So each entry carries a
digest over its own fields *and* over the previous entry's digest. Editing entry 12
invalidates 12 and every entry after it, and checking that is a walk rather than an act
of faith.

**A ledger that is itself the leak.** The audit trail is the longest-retained and most
widely read table in the system, which makes it the worst possible place to keep anything
sensitive. Two rules follow, and both are enforced in the model rather than left to
callers, because a check that lives in a helper is a check someone can construct their way
around:

- an entry records `ent_hash`, never the capability list. A ledger of capabilities is a
  map of who can see what, which is a document nobody should have;
- an entry records field *names*, never field *values*. `redact_details` is how a caller
  gets from one to the other.

What the chain does not prove, stated plainly because a hash chain is routinely credited
with more than it does:

- **Tail truncation.** Delete the newest three entries and what remains verifies
  perfectly. Nothing inside the data can close that. Only a digest recorded outside the
  database can, which is what `head` produces and `covers_anchor` checks.
- **Wholesale rewriting.** Anyone able to rewrite every row from the tamper point forward
  produces a chain that verifies. The chain proves nothing was *quietly* edited; the
  external anchor is what makes a rewrite visible.

Scope: M24.1 is the chain logic only. Nothing here touches a database. The table that
eventually persists these entries stores the same fields and runs `verify` as its check
job (M24.1.2).

Task ids: M24.1.1, M24.1.2, M24.1.3, M24.1.4, M24.2.1
"""

from __future__ import annotations

import enum
import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from brain.core.entitlement import CAPABILITY_RE, VERBS

# --------------------------------------------------------------------- grammars

#: What a field name looks like: `contract_value`, `client.contract_value`. This is the
#: field half of the capability grammar in brain.core.entitlement, on purpose: the names
#: the ledger is allowed to record are exactly the names a grant can be written about.
FIELD_NAME = r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$"

#: A reference, not prose. No whitespace, bounded length. Real ids in this system are
#: mixed case (`recuA1B2C3` from Lark, `c_0447` from Laravel), so case cannot be part of
#: the rule.
IDENTIFIER = r"^[A-Za-z0-9_.@-]{1,128}$"

#: db.py sizes trace_id at 64 characters; app.py mints them as a uuid4 hex, which is 32.
TRACE_ID = r"^[A-Za-z0-9_.-]{1,64}$"

#: EntitlementSet.ent_hash truncates its sha256 to 32 characters. If that ever changes,
#: this fails loudly on the next entry written rather than silently storing a short hash.
ENT_HASH = r"^[0-9a-f]{32}$"

#: A full sha256 hexdigest: the chain links.
DIGEST = r"^[0-9a-f]{64}$"

_FIELD_NAME_RE = re.compile(FIELD_NAME)
#: 32 hex (an ent_hash) or 64 hex (a chain digest). Both are safe to record as a detail
#: value; see `_is_recordable` for why a digest is not treated as a value.
_RECORDABLE_DIGEST_RE = re.compile(r"^[0-9a-f]{32}(?:[0-9a-f]{32})?$")
_IDENTIFIER_RE = re.compile(IDENTIFIER)

#: Domain separation, and a warning. Every digest begins with this string, so a digest
#: produced here can never collide with one produced by some other hashing in the system.
#: If the set of hashed fields ever changes, this constant must change with it, and at
#: that moment every historical entry stops verifying. A chain cannot be re-hashed in
#: place without destroying the one thing it was built to prove, so changing the covered
#: fields means storing a schema version per entry and teaching `verify` both. It is a
#: migration, not an edit to this line.
HASH_SCHEMA = "brain.audit.v1"

#: sha256 hexdigest width. Genesis is the same width as every other link so that nothing
#: in `verify` has to special-case the first entry.
DIGEST_CHARS = 64
GENESIS_HASH = "0" * DIGEST_CHARS

#: What a stripped value becomes. It deliberately carries no type and no length. A marker
#: reading `<redacted:int:5>` would tell any reader the order of magnitude of the salary
#: underneath, which is most of what the salary was worth hiding; the shape of a value is
#: still information about the value.
REDACTED = "<redacted>"

#: The things an audit entry can be *about*. Closed, because the client-visible audit view
#: filters on this (M24.1.5), and a free-text subject kind makes "everything that ever
#: happened to this principal" unanswerable without a full scan and a guess.
SUBJECT_KINDS = frozenset(
    {"principal", "grant", "agent", "leash", "entity", "artifact", "connector", "session"}
)


class AuditAction(enum.StrEnum):
    """Everything that must reach the ledger (M24.1.3).

    Closed on purpose. An open action vocabulary is how an auditable event ends up
    unaudited: someone adds a code path, invents a string for it, and nothing anywhere
    notices that no entry was ever written. Adding a member here fails the invariant test
    that pins this set, which is the point - a new auditable action becomes a deliberate
    edit in two places rather than an omission in one.

    DENY and REVOKE are separate members although the delivery document names them
    together as one item. A deny is a request refused at runtime; a revoke is a grant
    taken away by an administrator. They have different actors, they differ by orders of
    magnitude in frequency (denies are routine, revokes are rare), and they answer
    different questions. Collapsing them makes "who removed her access, and when"
    unanswerable without reading the details of every refusal in between.
    """

    GRANT = "grant"
    DENY = "deny"
    REVOKE = "revoke"
    LEASH_CHANGE = "leash_change"
    ENTITY_MERGE = "entity_merge"
    PUBLISH = "publish"
    BREAK_GLASS = "break_glass"


# --------------------------------------------------------------------- redaction


def _is_recordable(value: str) -> bool:
    """True when a details value is a name, a comma-joined list of names, a digest, or
    the redaction marker.

    A digest is admitted where a raw value is not, and the difference is enumerability.
    An `ent_hash` is a sha256 over a whole grant set: the input space is large enough that
    the digest reveals nothing. A digest of a five-digit salary is a different object
    entirely, because ninety thousand candidates is a lookup table, not a secret. That is
    why this admits digests by *shape* only where the producer is known to be an
    entitlement set or the chain itself, and why `changed_fields` records names rather
    than hashing the values it compares.
    """
    if value == REDACTED:
        return True
    if _RECORDABLE_DIGEST_RE.match(value):
        return True
    if _is_capability(value):
        # A named exception, decided on 5 September, rather than a loosening of the rule.
        #
        # The strict version was defensible and unusable: an audit view that cannot say
        # *what* was granted is not an audit view, and "Aaron granted Wei Ling something"
        # is not a sentence anyone can act on. A capability is not personal data, it names
        # a permission rather than a person or a value, and it is already legible in the
        # grant table to anyone who can read this ledger.
        #
        # The exception is narrow on purpose. It admits the capability grammar and nothing
        # adjacent to it, so a value cannot arrive disguised as one: the grammar is a known
        # verb, a colon, and dotted lowercase names, with no spaces and no digits.
        return True
    return all(_FIELD_NAME_RE.match(part) for part in value.split(","))


def _is_capability(value: str) -> bool:
    """A capability string, by the same grammar the rest of the system uses.

    Imported rather than copied. Two definitions of one grammar drift, and the drift here
    would be silent in the direction that matters: a ledger admitting a shape the
    capability type rejects is a ledger admitting something that is not a capability.

    The verb is checked as well as the shape, so `notice:the_client_is_overdue` does not
    slip through by happening to look like one.
    """
    if not CAPABILITY_RE.match(value):
        return False
    return value.split(":", 1)[0] in VERBS


def _redact_value(value: object) -> str:
    if isinstance(value, bool):
        # There are exactly two booleans, so neither can carry content.
        return "true" if value else "false"
    if isinstance(value, str):
        return value if _is_recordable(value) else REDACTED
    if isinstance(value, Mapping):
        # A nested mapping is a record. Keep the names of its fields, drop everything
        # else: this is what makes a before/after state recordable at all (M24.1.4).
        names = sorted(k for k in value if isinstance(k, str) and _FIELD_NAME_RE.match(k))
        return ",".join(names) if names else REDACTED
    if isinstance(value, Sequence):
        # All-or-nothing. A list where one element is a value must not half-survive: the
        # surviving half tells the reader which element was the interesting one.
        items = [v for v in value if isinstance(v, str) and _FIELD_NAME_RE.match(v)]
        return ",".join(sorted(items)) if items and len(items) == len(value) else REDACTED
    return REDACTED


def redact_details(details: Mapping[str, object]) -> dict[str, str]:
    """Reduce a details mapping to names and digests, and nothing that could be a value.

    This is an allowlist, and that is the whole design. A denylist of things that look
    like values ("contains spaces", "looks like money", "matches an email") is unbounded:
    for every rule written, some real value eventually slips past, and the failure is
    silent and permanent because the ledger is never deleted. An allowlist of things that
    look like *field names* is closed, and the cost of getting it wrong is a redacted
    entry rather than a leaked one.

    Survives redaction: a field name, a digest, a bool, a sequence of field names
    (comma-joined), and a mapping (reduced to the field names among its keys). Everything
    else becomes `REDACTED`.

    A key that is not itself a field name is dropped rather than redacted, because in that
    case the key is the leak: `{"SNM Construction Pte Ltd": "overdue"}` gives away the
    client by naming it, and redacting only the value would keep the name.
    """
    return {
        key: _redact_value(value) for key, value in details.items() if _FIELD_NAME_RE.match(key)
    }


def changed_fields(before: Mapping[str, object], after: Mapping[str, object]) -> tuple[str, ...]:
    """The names of the fields whose values differ. Names only; never the values.

    The delivery document asks an entry to carry "before and after state" (M24.1.4). This
    is as much of that as the ledger is allowed to hold, and the gap is deliberate rather
    than an oversight. Recording the values would put every salary and every contract
    value into the longest-retained table in the system. Recording a digest of them is
    worse than it first sounds, for the reason given in `_is_recordable`: a low-entropy
    value and its digest are the same secret.

    So the ledger proves *that* a field changed, when, and who changed it, and the row's
    own version history says what it changed to. Two records, two different retentions,
    two different access controls; joining them is a deliberate act that leaves its own
    audit entry.
    """
    missing = object()
    names = set(before) | set(after)
    return tuple(
        sorted(
            name
            for name in names
            if _FIELD_NAME_RE.match(name) and before.get(name, missing) != after.get(name, missing)
        )
    )


# --------------------------------------------------------------------- hashing


def _digest(parts: Iterable[str]) -> str:
    """Length-prefixed concatenation, then sha256.

    The prefix is not decoration. Joining parts with a separator makes the digest
    ambiguous the moment any part can contain that separator: `("ab", "c")` and
    `("a", "bc")` join to the same string, so two different entries share a digest and one
    can be swapped for the other without the chain noticing. Prefixing each part with its
    length removes the ambiguity outright, rather than resting on a promise that no actor
    id, subject or detail key will ever contain the separator character.
    """
    joined = "".join(f"{len(part)}:{part}" for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def compute_entry_hash(
    *,
    seq: int,
    at: datetime,
    actor_id: str,
    action: AuditAction,
    subject: str,
    ent_hash: str,
    trace_id: str,
    details: Mapping[str, str],
    prev_hash: str,
) -> str:
    """The digest an entry carries. Covers every field, and the previous entry's digest.

    Public because whatever persists these rows has to be able to recompute them without
    reconstructing an `AuditEntry` first.
    """
    # The order of these lines is part of the hash schema. Reordering them changes every
    # digest the system has ever produced; see HASH_SCHEMA.
    parts: list[str] = [
        HASH_SCHEMA,
        prev_hash,
        str(seq),
        # Normalised to UTC so that one instant written as +08:00 and as Z digests
        # identically. Two workers in different timezones recording the same event must
        # not disagree about it.
        at.astimezone(UTC).isoformat(),
        actor_id,
        action.value,
        subject,
        ent_hash,
        trace_id,
    ]
    # Sorted, because a dict preserves insertion order and two entries with identical
    # details built in different orders would otherwise digest differently. This is the
    # same mistake EntitlementSet.ent_hash avoids by sorting its grants before hashing.
    for key in sorted(details):
        parts.append(key)
        parts.append(details[key])
    return _digest(parts)


# --------------------------------------------------------------------- the entry


class AuditEntry(BaseModel):
    """One fact, chained to the one before it.

    `entry_hash` is stored rather than computed on read. A computed property would follow
    the data wherever it went, so an altered entry would produce an altered digest and
    agree with itself forever. Storing the digest is what makes disagreement possible, and
    the disagreement is the detection.

    The model does not check `entry_hash` on construction, which reads like an omission
    and is not. A tampered row has to load so that `AuditChain.verify` can *report* it. A
    validator here would raise on load instead, and a ledger that refuses to load its own
    damaged rows cannot tell anybody which row is damaged, or what it says.

    `frozen=True` stops attributes being rebound; it does not stop `entry.details["k"]`
    being written in place, since the value is a real dict. That is acceptable precisely
    because nothing here depends on immutability: `verify` recomputes rather than trusting,
    so an in-place edit is caught in the same breath as an edit to any other field.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=0)
    at: datetime
    actor_id: str = Field(pattern=IDENTIFIER)
    action: AuditAction
    subject: str = Field(min_length=3, max_length=160)
    #: The actor's entitlement at the time, as a hash. Never the capabilities themselves.
    ent_hash: str = Field(pattern=ENT_HASH)
    trace_id: str = Field(pattern=TRACE_ID)
    details: dict[str, str] = Field(default_factory=dict)
    prev_hash: str = Field(pattern=DIGEST)
    entry_hash: str = Field(pattern=DIGEST)

    @field_validator("at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            msg = "at must be timezone-aware; a naive timestamp is a silent bug"
            raise ValueError(msg)
        return v

    @field_validator("subject")
    @classmethod
    def _subject_grammar(cls, v: str) -> str:
        kind, sep, ident = v.partition(":")
        if not sep or kind not in SUBJECT_KINDS:
            msg = f"subject {v!r} must be <kind>:<id>, kind one of {sorted(SUBJECT_KINDS)}"
            raise ValueError(msg)
        if not _IDENTIFIER_RE.match(ident):
            msg = f"subject id {ident!r} is not an identifier; a subject is a reference, not prose"
            raise ValueError(msg)
        return v

    @field_validator("details")
    @classmethod
    def _names_only(cls, v: dict[str, str]) -> dict[str, str]:
        """Refuse anything `redact_details` would have stripped.

        This duplicates the redactor deliberately. `AuditChain.append` always redacts, but
        entries also arrive by being loaded from a table, and a row written by an older
        version of the code, by a migration, or by hand must not be able to introduce a
        value that the redactor would have caught. Enforcing it at the type means there is
        one answer to "can a value be in the ledger" rather than one answer per code path.
        """
        bad: list[str] = []
        for key, value in v.items():
            if not _FIELD_NAME_RE.match(key):
                bad.append(f"key {key!r} is not a field name")
            elif not _is_recordable(value):
                bad.append(f"value of {key!r} is not a name, a digest or {REDACTED}")
        if bad:
            msg = "details would put a value in the ledger: " + "; ".join(bad)
            raise ValueError(msg)
        return v

    def recompute_hash(self) -> str:
        """What this entry's digest should be, given what it currently says."""
        return compute_entry_hash(
            seq=self.seq,
            at=self.at,
            actor_id=self.actor_id,
            action=self.action,
            subject=self.subject,
            ent_hash=self.ent_hash,
            trace_id=self.trace_id,
            details=self.details,
            prev_hash=self.prev_hash,
        )


# --------------------------------------------------------------------- breakage


class BreakReason(enum.StrEnum):
    """Why the walk stopped.

    The verification job (M24.1.2) reports this alongside the index, because "the chain
    broke at 47" with no reason sends an operator to read forty-seven rows by hand to work
    out whether they are looking at a tamper, a bad migration or a deletion.
    """

    SEQUENCE_BROKEN = "sequence_broken"
    LINK_BROKEN = "link_broken"
    CONTENT_ALTERED = "content_altered"


class ChainBreak(BaseModel):
    """Where the chain stopped holding, and what was expected there instead."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int
    seq: int
    reason: BreakReason
    expected: str
    actual: str


# --------------------------------------------------------------------- legal hold


class LegalHold(BaseModel):
    """A predicate that suspends deletion (M24.2.1).

    A predicate rather than a flag on the row, for one reason: a hold is placed before
    anyone knows which entries it will need to cover, and it has to cover entries written
    *after* it is placed. A flag can only mark what already exists, so a flag-based hold
    quietly fails to hold exactly the entries a live dispute is generating.

    `reason_code` is a field-name token and not free text. A free-text reason on a legal
    hold is where the names of the parties, the complainant and the allegation end up, in
    the one table that outlives every retention policy in the system.

    A released hold is marked released, never deleted: which entries were held, on whose
    authority and for how long is itself a thing that gets asked about later.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=IDENTIFIER)
    reason_code: str = Field(pattern=FIELD_NAME, max_length=80)
    subjects: frozenset[str] = frozenset()
    actors: frozenset[str] = frozenset()
    #: A company-wide hold. Explicit, because the alternative reading of "no subjects and
    #: no actors" is "everything", and a hold that means everything by accident is as bad
    #: as one that means nothing by accident.
    all_subjects: bool = False
    placed_at: datetime
    released_at: datetime | None = None

    @field_validator("placed_at", "released_at")
    @classmethod
    def _tz_aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            msg = "hold timestamps must be timezone-aware; a naive one is a silent bug"
            raise ValueError(msg)
        return v

    def model_post_init(self, _context: object, /) -> None:
        if not (self.all_subjects or self.subjects or self.actors):
            # The failure this prevents: a hold is placed, the sweep runs, and nothing is
            # held, because the hold names nothing and nothing complains. It is discovered
            # when the data is asked for and is gone.
            msg = "a hold must name subjects or actors, or set all_subjects"
            raise ValueError(msg)

    def is_active(self, now: datetime | None = None) -> bool:
        moment = now or datetime.now(UTC)
        if moment < self.placed_at:
            return False
        return self.released_at is None or moment < self.released_at

    def covers(self, entry: AuditEntry) -> bool:
        """Whether this hold reaches the entry. Says nothing about whether it is active."""
        return self.all_subjects or entry.subject in self.subjects or entry.actor_id in self.actors


def is_held(entry: AuditEntry, holds: Iterable[LegalHold], now: datetime | None = None) -> bool:
    """True when any active hold reaches this entry. A held entry cannot be deleted."""
    return any(hold.is_active(now) and hold.covers(entry) for hold in holds)


# --------------------------------------------------------------------- the chain


class AuditChain:
    """An ordered run of entries, and the walk that proves nothing in it moved.

    `start_hash` exists so that a *window* of a longer ledger can be verified on its own.
    A verification job that can only ever start from genesis is a job that gets slower
    every day and is eventually switched off; one that can verify last month against the
    digest it recorded last month is one that keeps running. It is also what makes a
    retention prune leave something that still verifies rather than something that looks
    like a forgery.
    """

    def __init__(
        self, entries: Sequence[AuditEntry] = (), *, start_hash: str = GENESIS_HASH
    ) -> None:
        self._entries: list[AuditEntry] = list(entries)
        self._start_hash = start_hash

    @property
    def entries(self) -> tuple[AuditEntry, ...]:
        return tuple(self._entries)

    @property
    def start_hash(self) -> str:
        return self._start_hash

    def __len__(self) -> int:
        return len(self._entries)

    def head(self) -> str:
        """The digest to record outside the database.

        An empty chain's head is its start hash, so an anchor taken before the first entry
        is still meaningful and nothing has to special-case the empty case.
        """
        return self._entries[-1].entry_hash if self._entries else self._start_hash

    def append(
        self,
        *,
        action: AuditAction,
        actor_id: str,
        subject: str,
        ent_hash: str,
        trace_id: str,
        at: datetime,
        details: Mapping[str, object] | None = None,
    ) -> AuditEntry:
        """Write one entry and return it.

        `seq`, `prev_hash` and `entry_hash` are computed here and are not parameters. A
        caller who can choose them can forge a link, and there is then no such thing as a
        well-formed entry, only a conventional one.

        `at` has no default, and `datetime.now(UTC)` is deliberately not one. db.py makes
        the argument in full: application time is the clock of whichever container handled
        the write, those clocks drift, and a ledger ordered by them is subtly wrong exactly
        when the ordering matters. The caller must pass one authoritative clock's reading.
        Note the consequence, which is real: because the timestamp is inside the digest,
        the database cannot fill it in with `server_default` the way every other table
        here does. Either the caller reads the clock from the database and passes it in, or
        the digest has to be computed in the database.

        Details are redacted here so that no caller has to remember to.
        """
        seq = self._entries[-1].seq + 1 if self._entries else 0
        prev_hash = self.head()
        safe_details = redact_details(details or {})
        entry = AuditEntry(
            seq=seq,
            at=at,
            actor_id=actor_id,
            action=action,
            subject=subject,
            ent_hash=ent_hash,
            trace_id=trace_id,
            details=safe_details,
            prev_hash=prev_hash,
            entry_hash=compute_entry_hash(
                seq=seq,
                at=at,
                actor_id=actor_id,
                action=action,
                subject=subject,
                ent_hash=ent_hash,
                trace_id=trace_id,
                details=safe_details,
                prev_hash=prev_hash,
            ),
        )
        self._entries.append(entry)
        return entry

    def first_break(self) -> ChainBreak | None:
        """The first entry that does not hold, with the reason, or None."""
        previous_hash = self._start_hash
        previous_seq: int | None = None
        for index, entry in enumerate(self._entries):
            # Sequence first. It is redundant with the link check for a deletion, since
            # both catch it, but it names the failure: "seq jumped from 4 to 6" reads as a
            # missing row, where "digest mismatch" reads as a tamper, and an operator
            # follows those two findings to different places.
            if previous_seq is not None and entry.seq != previous_seq + 1:
                return ChainBreak(
                    index=index,
                    seq=entry.seq,
                    reason=BreakReason.SEQUENCE_BROKEN,
                    expected=str(previous_seq + 1),
                    actual=str(entry.seq),
                )
            if entry.prev_hash != previous_hash:
                return ChainBreak(
                    index=index,
                    seq=entry.seq,
                    reason=BreakReason.LINK_BROKEN,
                    expected=previous_hash,
                    actual=entry.prev_hash,
                )
            recomputed = entry.recompute_hash()
            if recomputed != entry.entry_hash:
                return ChainBreak(
                    index=index,
                    seq=entry.seq,
                    reason=BreakReason.CONTENT_ALTERED,
                    expected=recomputed,
                    actual=entry.entry_hash,
                )
            previous_hash = entry.entry_hash
            previous_seq = entry.seq
        return None

    def verify(self) -> int | None:
        """The index of the first entry that does not hold, or None when the chain is
        whole. Index rather than sequence number, because a window's indices are what
        address the entries the caller is holding."""
        found = self.first_break()
        return None if found is None else found.index

    def covers_anchor(self, *, seq: int, entry_hash: str) -> bool:
        """True when this chain still contains the anchored entry, unchanged.

        `verify` alone cannot see a truncated tail: remove the newest entries and what is
        left is a valid chain that ends earlier. Nothing inside the data distinguishes
        that from a ledger where those events never happened. The only fix is a digest
        recorded somewhere the database administrator does not control - by the
        verification job, in a separate store, or published - and asked for later. This is
        the asking.
        """
        for entry in self._entries:
            if entry.seq == seq:
                return entry.entry_hash == entry_hash
        return False

    def prune_before(
        self,
        cutoff: datetime,
        *,
        holds: Iterable[LegalHold] = (),
        now: datetime | None = None,
    ) -> tuple[AuditChain, tuple[AuditEntry, ...]]:
        """Remove the oldest entries retention has released, stopping at the first it has
        not. Returns the retained chain and what was removed.

        A retention sweep over a hash chain can only ever take a prefix. Removing an entry
        from the middle leaves the next one pointing at a digest that is no longer there,
        so the chain reports a break for the rest of its life and the sweep has destroyed
        the only property the ledger existed for. The sweep therefore walks from the oldest
        entry and stops dead at the first that is either newer than the cutoff or under
        legal hold. One held entry from three years ago pins every entry after it, which is
        expensive and is the correct behaviour: that is what a hold is.

        The retained chain carries the last removed entry's digest as its `start_hash`, so
        what remains still verifies as a window instead of looking like a chain whose
        beginning was forged.

        This returns a new chain rather than mutating, so a sweep that is going to refuse
        can be inspected before anything is written back.
        """
        held = list(holds)
        cut = 0
        for entry in self._entries:
            if entry.at >= cutoff or is_held(entry, held, now):
                break
            cut += 1
        removed = tuple(self._entries[:cut])
        start = removed[-1].entry_hash if removed else self._start_hash
        return AuditChain(self._entries[cut:], start_hash=start), removed
