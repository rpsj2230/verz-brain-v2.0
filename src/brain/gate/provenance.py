"""Where an answer's evidence came from, and how old that evidence is.

`brain.gate.compose` already derives one citation per field that survived redaction, which
is what makes citing something withheld structurally impossible. This module is the rest of
the provenance story: the document plane's half of it, the freshness state that turns a
citation into something a person can weigh, and the guard that keeps a citation from being
authored by the model rather than derived from what was actually fetched.

**What breaks without it.** Two things, and both are quiet.

Every figure in every answer reads as current. The questions this system exists to answer
are time-sensitive by nature ("how many hours are left on SNM's block", "is that invoice
paid"), and a number read at 09:00 and quoted at 17:00 is acted on as though somebody had
just looked. Nobody files a bug, because the answer was true when it was fetched.

And a document answer cites a document rather than a passage, so checking a claim means
rereading a forty-page contract. A citation nobody can follow is a citation nobody checks,
which is the same as having none while looking like diligence.

Three rules run through everything here.

**Freshness is stated, never inferred.** A read time is a fact somebody recorded at fetch
time. Where no read time was recorded, or where what was recorded cannot be dated, the
state is UNSTATED and it renders as such. It is never quietly promoted to "current" and it
is never guessed from when the request happened, because the whole failure this prevents is
a stale value wearing a live label.

**A citation is assembled from the trace, never from the model.** A model asked to cite its
sources produces citation-shaped text, which is not the same thing and fails in the
direction nobody checks: it names real documents that do not contain the claim. The row
half of this is already structural in `compose`; the document half is `assert_derived`.

**A document citation points at a passage by position, never by quoting it.** The anchor is
a chunk id, a page and a character span. The rejected alternative was a W3C text fragment
(`#:~:text=...`), which is a better link and puts the quoted passage into a URL, and from
there into browser history, referer headers and every chat client's link preview. A passage
that survived scope filtering may be shown to the asker; it may not be scattered across
infrastructure nobody governs.

Scope: domain logic. Nothing here reads a clock, opens a connection, or calls a model.
`now` is always a parameter, for the reason `brain.models.routing.CircuitBreaker` gives:
a freshness rule that reads the clock itself cannot be tested at its own boundary, and the
boundary is the part that goes wrong.

Task ids: M8.1.1, M8.1.2, M8.1.3, M8.1.4
"""

from __future__ import annotations

import enum
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Final, Protocol

from brain.core.redaction import ChannelPayload
from brain.gate.compose import Citation, ComposedAnswer

# --------------------------------------------------------------------- grammars

#: A document or chunk reference. The same grammar as `brain.audit.ledger.IDENTIFIER` and
#: `brain.gate.leash.IDENTIFIER`, restated rather than imported for the reason
#: `brain.core.redaction` restates its own name pattern: this module's guarantee should not
#: move when somebody widens an unrelated one.
_REFERENCE_RE: Final = re.compile(r"^[A-Za-z0-9_.@-]{1,128}$")


# ------------------------------------------------------------- freshness (M8.1.3)


class Freshness(enum.StrEnum):
    """How much weight a citation's age allows.

    Four states rather than a boolean, because "fresh or stale" forces one threshold to
    carry two different jobs: the point past which a person should re-check, and the point
    past which the system should not have shown the number at all. Those are hours apart
    for a ticket count and months apart for a signed contract.
    """

    #: Read inside the horizon's live window. Safe to act on.
    LIVE = "live"
    #: Older than the live window, younger than the stale threshold. Worth re-checking.
    AGEING = "ageing"
    #: Past the stale threshold. Reported, and never silently substituted for a live read.
    STALE = "stale"
    #: No read time was recorded, or what was recorded cannot be dated. The fail-closed
    #: state, and the one that must never be rendered as anything resembling "current".
    UNSTATED = "unstated"


#: What each state says to a person. UNSTATED deliberately does not echo whatever string
#: was in `fetched_at`: repeating "14:31" back while admitting we cannot date it is the
#: inference this module refuses, dressed as candour.
FRESHNESS_TEXT: Mapping[Freshness, str] = MappingProxyType(
    {
        Freshness.LIVE: "current",
        Freshness.AGEING: "may have changed",
        Freshness.STALE: "out of date",
        Freshness.UNSTATED: "read time not stated",
    }
)


@dataclass(frozen=True)
class StalenessHorizon:
    """The two thresholds that turn an age into a state.

    A value passed in, not a constant read here, for the same reason `FieldPolicy` is a
    value: a horizon is per entity in production (hours remaining ages in minutes, a signed
    contract in months) and the console owns the numbers. A module-level default applied
    silently would be exactly the inference this file exists to prevent.
    """

    live_for: timedelta
    stale_after: timedelta

    def __post_init__(self) -> None:
        if self.live_for <= timedelta():
            msg = "live_for must be positive; a zero live window makes every read stale at once"
            raise ValueError(msg)
        if self.stale_after < self.live_for:
            # Inverted thresholds produce an AGEING band that cannot be entered, so the
            # state machine silently becomes a boolean and nobody notices until somebody
            # asks why nothing is ever ageing.
            msg = (
                f"stale_after {self.stale_after} is earlier than live_for {self.live_for}; "
                "the ageing band would be empty and freshness would collapse to a boolean"
            )
            raise ValueError(msg)


#: A seed, in the shape of `brain.models.routing.TIER_CONTEXT_WINDOW`: something to start a
#: console row from, never a value this module applies on a caller's behalf. Fifteen minutes
#: is roughly the span in which a person asking twice expects the same answer; a day is the
#: point past which quoting a business figure without re-reading it is indefensible.
DEFAULT_HORIZON: Final = StalenessHorizon(
    live_for=timedelta(minutes=15), stale_after=timedelta(hours=24)
)


def read_time(fetched_at: str) -> datetime | None:
    """The recorded read time, or None when there is not one we can date.

    Strict on purpose, in three ways.

    **ISO 8601 only.** `brain.core.envelope.TypedResult.fetched_at` is a bare string with no
    format contract, so connectors can and do put a wall clock in it. "14:31" is not a date,
    and a module that helpfully assumed today would attach a real timestamp to a value that
    might have been fetched last Tuesday.

    **Timezone-aware only.** A naive timestamp is a silent bug, in the words
    `brain.gate.leash.ActionRecord` already uses about its own. Singapore reads a UTC
    timestamp as eight hours old, which is the difference between LIVE and AGEING for every
    answer in the building.

    **Nothing is inferred from failure.** None means "not stated", and every caller turns
    that into `Freshness.UNSTATED` rather than into a guess.
    """
    candidate = fetched_at.strip()
    if not candidate:
        return None
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def assess_freshness(fetched_at: str, *, horizon: StalenessHorizon, now: datetime) -> Freshness:
    """One read time and one horizon, as a state.

    A read time in the future returns UNSTATED rather than LIVE. It means a clock is wrong
    somewhere, and clock skew is the one condition under which "definitely current" is
    exactly the claim we cannot make. Treating it as live would make a misconfigured
    connector the freshest source in the company.
    """
    if now.tzinfo is None:
        # Raised rather than absorbed: a naive `now` is a programming error in the calling
        # layer, and absorbing it would silently mark every citation UNSTATED, which reads
        # in the console as a connector problem and sends somebody to the wrong system.
        msg = "now must be timezone-aware; comparing it with a recorded read time otherwise lies"
        raise ValueError(msg)
    read = read_time(fetched_at)
    if read is None:
        return Freshness.UNSTATED
    age = now - read
    if age < timedelta():
        return Freshness.UNSTATED
    if age <= horizon.live_for:
        return Freshness.LIVE
    if age <= horizon.stale_after:
        return Freshness.AGEING
    return Freshness.STALE


@dataclass(frozen=True)
class StatedFreshness:
    """A freshness state and the read time it was computed from.

    Carries the raw `fetched_at` so a trace can show what the connector actually recorded,
    and renders it only when it was datable. The two together are what makes "stated, never
    inferred" checkable after the fact rather than merely asserted here.
    """

    state: Freshness
    fetched_at: str = ""

    def render(self) -> str:
        if self.state is Freshness.UNSTATED:
            return FRESHNESS_TEXT[self.state]
        return f"{FRESHNESS_TEXT[self.state]}, read {self.fetched_at}"


def state_freshness(
    fetched_at: str, *, horizon: StalenessHorizon, now: datetime
) -> StatedFreshness:
    """`assess_freshness`, keeping the evidence beside the verdict."""
    return StatedFreshness(
        state=assess_freshness(fetched_at, horizon=horizon, now=now), fetched_at=fetched_at
    )


# --------------------------------------------------- document citations (M8.1.2)


@dataclass(frozen=True)
class Anchor:
    """Where in a document a passage sits, by position rather than by content.

    `chunk_id` is required and the rest is optional, because the chunk is the thing
    retrieval actually returned and everything else is a convenience for a person reading
    the citation. A citation that named only a page would not survive the document being
    re-paginated; one that named only a character span would not survive a re-parse. The
    chunk id is what the index can resolve, and the human coordinates ride along.
    """

    chunk_id: str
    page: int | None = None
    section: str = ""
    #: Half-open character span within the document, as parsed. Both ends or neither.
    start: int | None = None
    end: int | None = None

    def __post_init__(self) -> None:
        if not _REFERENCE_RE.match(self.chunk_id):
            msg = f"chunk id {self.chunk_id!r} is not a reference"
            raise ValueError(msg)
        if self.page is not None and self.page < 1:
            msg = f"page {self.page} is not a page number"
            raise ValueError(msg)
        if (self.start is None) != (self.end is None):
            # Half a span is worse than none: it renders as a location and resolves to
            # nothing, so the citation looks followable and is not.
            msg = "a character span needs both ends or neither"
            raise ValueError(msg)
        if self.start is not None and self.end is not None and self.end < self.start:
            msg = f"character span {self.start}:{self.end} ends before it begins"
            raise ValueError(msg)

    def fragment(self) -> str:
        """A URL fragment locating the passage, for the channel to hang off a document link.

        A fragment rather than a whole URL, because this module does not know where the
        console is deployed, and a base URL baked in here is the thing that silently points
        every citation in an email at localhost.
        """
        parts = [f"chunk={self.chunk_id}"]
        if self.page is not None:
            parts.append(f"page={self.page}")
        if self.start is not None and self.end is not None:
            parts.append(f"chars={self.start}-{self.end}")
        return "&".join(parts)

    def describe(self) -> str:
        """The human half: "page 4, section 3.2", or the chunk when there is nothing else."""
        parts: list[str] = []
        if self.page is not None:
            parts.append(f"page {self.page}")
        if self.section:
            parts.append(f"section {self.section}")
        return ", ".join(parts) if parts else f"chunk {self.chunk_id}"


@dataclass(frozen=True)
class DocumentCitation:
    """One passage standing behind a claim (M8.1.2).

    The mirror of `brain.gate.compose.Citation` for the document plane, and it carries no
    passage text for the same reason that one carries no field value: a citation holding the
    content would be a second copy of the answer travelling under a different name, and it
    survives into traces, logs and forwarded messages that the payload does not reach.

    `title` is shown. The document plane puts the scope filter inside the query, so a
    passage that reached retrieval is one this asker may read, and a citation they cannot
    identify is one they cannot check.

    Rejected: a separate `RetrievedPassage` type, with this one derived from it. Two types
    with identical fields is ceremony that reads as a guarantee, and the guarantee is
    actually `assert_derived` below, which compares against the trace.
    """

    document_id: str
    title: str
    anchor: Anchor
    source: str = ""
    fetched_at: str = ""

    def __post_init__(self) -> None:
        if not _REFERENCE_RE.match(self.document_id):
            msg = f"document id {self.document_id!r} is not a reference"
            raise ValueError(msg)

    def render(self) -> str:
        where = f" from {self.source}" if self.source else ""
        when = f", as of {self.fetched_at}" if self.fetched_at else ""
        return f"{self.title}, {self.anchor.describe()}{where}{when}"


@dataclass(frozen=True)
class RetrievalTrace:
    """What the document plane returned, as retrieval recorded it (M8.1.4).

    The provenance of the provenance. It exists so that "assembled from the tool trace" is
    an argument a function takes rather than a convention somebody follows, and so
    `assert_derived` has something to compare against that no model has touched.
    """

    passages: tuple[DocumentCitation, ...] = ()


#: An empty trace, for a row-only answer. A module constant rather than a default_factory
#: because the type is frozen, in the shape `brain.models.routing.UNCONSTRAINED` uses.
NO_DOCUMENTS: Final = RetrievalTrace()


class ModelAuthoredCitationError(Exception):
    """A citation was offered that the retrieval trace does not contain (M8.1.4).

    Outside the user-facing taxonomy, deliberately, for the reason
    `brain.core.redaction.UntypedShapeError` gives about its own: nobody asking a question
    should see this. It means the layer above tried to cite something nothing fetched, and
    it should stop that code being written rather than degrade somebody's answer.
    """


def assert_derived(claimed: Iterable[DocumentCitation], *, trace: RetrievalTrace) -> None:
    """Refuse any document citation the trace does not hold (M8.1.4).

    Compared by value rather than by identity, so a citation reconstructed field for field
    from the trace is admitted. That is not a loophole: a citation identical to one the
    trace holds names a passage that was genuinely retrieved, which is the whole property.
    What it refuses is the one that differs, and the difference is invariably the model
    having produced a plausible page number for a real document.

    Rejected: parsing `[1]`-style markers out of the model's prose and resolving them
    against the trace. It reads as the same check and is the opposite of one, because the
    resolution step invents the mapping the model was supposed to supply, so an answer that
    cited nothing at all comes back fully cited.
    """
    held = set(trace.passages)
    stray = [c for c in claimed if c not in held]
    if stray:
        named = ", ".join(f"{c.document_id}#{c.anchor.chunk_id}" for c in stray)
        msg = (
            f"{len(stray)} citation(s) name passages the retrieval trace does not hold: "
            f"{named}; a citation is derived from what was fetched, never authored"
        )
        raise ModelAuthoredCitationError(msg)


def assert_rows_derived(claimed: Iterable[Citation], *, answer: ComposedAnswer) -> None:
    """The same rule for row citations, checked against the composer's own derivation.

    It compares against `ComposedAnswer.citations` rather than recomputing them from the
    payload. Recomputing would be a second opinion about what may be cited, and the day the
    two disagree the permissive one wins by being the one a caller happened to use.
    """
    held = set(answer.citations)
    stray = [c for c in claimed if c not in held]
    if stray:
        named = ", ".join(f"{c.entity} {c.record_id}: {c.field}" for c in stray)
        msg = (
            f"{len(stray)} citation(s) name fields the answer does not contain: {named}; "
            "citations are derived from what survived redaction, never supplied beside it"
        )
        raise ModelAuthoredCitationError(msg)


# ----------------------------------------------------- the evidence behind an answer


class Cited(Protocol):
    """What every citation, row or document, can be asked.

    A protocol rather than a base class so that `compose.Citation` satisfies it without
    this module reaching into `compose` to make it. Read-only properties on purpose: a
    citation that could be edited after it was derived is a citation that can be made to
    name something else.
    """

    @property
    def source(self) -> str: ...

    @property
    def fetched_at(self) -> str: ...

    def render(self) -> str: ...


@dataclass(frozen=True)
class Evidence:
    """One citation and the freshness stated for it (M8.1.3).

    Freshness lives here rather than on the citation because a citation is a fact about
    where a value came from and freshness is a judgement about when, evaluated against a
    horizon and a clock that neither `compose` nor the connector holds. Putting a state on
    the citation itself would mean computing it at fetch time and having it be wrong by the
    time anybody read it.
    """

    citation: Cited
    freshness: StatedFreshness

    def render(self) -> str:
        """The citation, with the state and not the read time again.

        `Citation.render` and `DocumentCitation.render` already append "as of ...", so
        repeating the timestamp here would print it twice in every answer. The state is the
        part this module adds, and where it is UNSTATED it is a correction to the "as of"
        the citation printed from a string nothing could date.
        """
        return f"{self.citation.render()} ({FRESHNESS_TEXT[self.freshness.state]})"


@dataclass(frozen=True)
class Provenance:
    """Everything standing behind one answer.

    Two tuples rather than one, because a row citation and a document citation are followed
    to different places and a channel renders them differently: one opens a record, the
    other opens a passage.

    There is nowhere here to put the model's words, and that is the point of the type. The
    answer's text travels in `ComposedAnswer`; provenance is computed from the payload and
    the retrieval trace, so it cannot vary with what the model chose to say.
    """

    rows: tuple[Evidence, ...] = ()
    documents: tuple[Evidence, ...] = ()

    @property
    def is_empty(self) -> bool:
        """True when nothing at all stands behind the answer.

        This is the predicate `brain.gate.abstain` turns into a refusal to state a claim.
        It is a property rather than a count for the reason `brain.core.redaction` gives
        about hidden-item counts: a number here would eventually be rendered.
        """
        return not self.rows and not self.documents

    def stalest(self) -> Freshness:
        """The weakest freshness in the set, which is the one an answer may claim.

        An answer built from a live row and a stale document is a stale answer. Reporting
        the best state present would let one fresh citation launder the rest, which is
        precisely how a stale number ends up looking live.

        UNSTATED outranks STALE. "We do not know how old this is" is a weaker position than
        "we know it is old", and a caller comparing states should not be able to treat the
        unknown as merely dated.
        """
        states = [e.freshness.state for e in (*self.rows, *self.documents)]
        if not states:
            return Freshness.UNSTATED
        if Freshness.UNSTATED in states:
            return Freshness.UNSTATED
        for state in (Freshness.STALE, Freshness.AGEING):
            if state in states:
                return state
        return Freshness.LIVE

    def render(self) -> tuple[str, ...]:
        return tuple(e.render() for e in (*self.rows, *self.documents))


def provenance_for(
    answer: ComposedAnswer,
    *,
    horizon: StalenessHorizon,
    now: datetime,
    trace: RetrievalTrace = NO_DOCUMENTS,
) -> Provenance:
    """Assemble the evidence behind a composed answer (M8.1.1, M8.1.3, M8.1.4).

    It reads `answer.citations`, which the composer derived from the post-redaction
    payload, and `trace.passages`, which retrieval recorded. It never reads `answer.text`.
    That is the whole of M8.1.4 expressed as a data dependency: change what the model said
    and this function returns the same provenance, because the model's words are not an
    input to it. The invariant suite checks exactly that.
    """
    rows = tuple(
        Evidence(
            citation=citation,
            freshness=state_freshness(citation.fetched_at, horizon=horizon, now=now),
        )
        for citation in answer.citations
    )
    documents = tuple(
        Evidence(
            citation=passage,
            freshness=state_freshness(passage.fetched_at, horizon=horizon, now=now),
        )
        for passage in trace.passages
    )
    return Provenance(rows=rows, documents=documents)


def payload_is_empty(payload: ChannelPayload) -> bool:
    """Whether anything survived redaction.

    A one-line predicate with a name, so that the abstention path asks this question of the
    post-redaction payload and has no way to ask it of anything earlier. A caller that
    counted rows before redaction would be counting records the asker may not see, and the
    difference between the two counts is exactly the fact that must never be observable.
    """
    return not payload.records
