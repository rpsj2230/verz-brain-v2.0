"""Turning a redacted payload into something a person reads, and saying where it came from.

The last step before an answer leaves. Three things happen here and each exists because the
alternative is a specific failure people actually have.

**Citations are derived from the payload, never supplied alongside it.** A citation is a
claim that a particular record and field back a sentence, and a citation supplied by a
caller can name a record the caller never received. Deriving them from what survived
redaction makes citing something withheld structurally impossible rather than merely
discouraged.

**Freshness is stated, never inferred.** "As of 14:31" is a fact the answer carries because
somebody fetched it then. An answer with no freshness is read as current, which is the
quiet failure: a stale number that looks live gets acted on.

**The trace goes to the sink and nowhere else.** The post-redaction payload is the one thing
allowed into a trace, and it is allowed only there. Not into logs, not into an error
message, not attached to the answer as a debugging convenience.

Task ids: M3.9.1, M3.9.2, M3.9.3, M3.9.4
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from brain.core.envelope import Entity, TypedResult
from brain.core.field_policy import FieldPolicy
from brain.core.redaction import (
    ChannelPayload,
    RedactedAnswer,
    RedactionTrace,
    redact,
)
from brain.gate.context import GateContext, GateStep, Recorder

#: Length of a trace reference, in URL-safe bytes.
TRACE_REF_BYTES = 12


class TraceSink(Protocol):
    """Where a trace goes. The only destination for a post-redaction payload (M3.9.2).

    A protocol rather than a concrete class so the gate does not depend on where traces are
    stored, and so a test can assert that exactly one thing was emitted exactly once.
    """

    def emit(self, reference: str, payload: ChannelPayload, trace: RedactionTrace) -> None: ...


@dataclass(frozen=True)
class Citation:
    """One record and field standing behind a claim.

    `field` is a name, never a value. A citation that carried the value would be a second
    copy of the answer travelling under a different name, and it would survive into places
    the payload does not.
    """

    entity: str
    record_id: str
    field: str
    source: str
    fetched_at: str

    def render(self) -> str:
        where = f" from {self.source}" if self.source else ""
        when = f", as of {self.fetched_at}" if self.fetched_at else ""
        return f"{self.entity} {self.record_id}: {self.field}{where}{when}"


@dataclass(frozen=True)
class ComposedAnswer:
    """What a person receives.

    `trace_ref` is quotable and carries no authority. Knowing a reference must not be a way
    to read the trace: the reference identifies which trace, and entitlement decides who may
    open it. A reference that granted access would be a capability handed to whoever the
    answer was forwarded to.
    """

    text: str
    citations: tuple[Citation, ...]
    payload: ChannelPayload
    trace_ref: str
    #: True when nothing survived redaction. Carried so a channel can render an honest
    #: "I could not find that" rather than an empty answer that reads as a confident none.
    grounded: bool

    def render_citations(self) -> tuple[str, ...]:
        return tuple(c.render() for c in self.citations)


def _citations_from(payload: ChannelPayload) -> tuple[Citation, ...]:
    """Every field that survived, as a citation. Derived, so nothing withheld can be cited.

    Reserved keys are skipped: `@entity` and `@id` identify the record rather than saying
    anything about it, and citing them would produce "client 447: id", which is noise in
    every answer and crowds out the citations that matter.
    """
    out: list[Citation] = []
    for record in payload.records:
        entity = str(record.get("@entity") or record.get("entity") or "")
        record_id = str(record.get("@id") or record.get("id") or "")
        for field in sorted(record):
            if field in ("@entity", "entity", "@id", "id"):
                continue
            out.append(
                Citation(
                    entity=entity,
                    record_id=record_id,
                    field=field,
                    source=payload.source,
                    fetched_at=payload.fetched_at,
                )
            )
    return tuple(out)


def new_trace_ref() -> str:
    """A fresh reference, random rather than derived from the content.

    Deriving it from the answer would make two identical answers share a reference, so
    comparing references would reveal that two people received the same thing. That is a
    small leak with an unbounded reach: it works across people, across time, and without
    either of them seeing the other's answer.
    """
    return secrets.token_urlsafe(TRACE_REF_BYTES)


def redact_for_gate[T: Entity](
    result: TypedResult[T],
    context: GateContext,
    policy: FieldPolicy,
    recorder: Recorder,
    *,
    now: datetime | None = None,
    opaque: bool = False,
) -> RedactedAnswer:
    """The gate's REDACT step (M3.9.1). One call site, so there is one to audit.

    It takes the `GateContext` rather than an entitlement, and that is the point: the
    context is frozen and carries the entitlement hash checked against the set beside it, so
    the walk cannot run against a reach that nobody resolved. A signature accepting a bare
    `EntitlementSet` would let a caller assemble one here, which is exactly the step this
    pipeline exists to make impossible.

    Entering the step on the recorder is not bookkeeping. `Recorder.enter` refuses a step
    that runs out of order, so composing before redacting fails here rather than producing
    an answer built from fields nobody checked.
    """
    recorder.enter(GateStep.REDACT)
    return redact(
        result,
        entitlement=context.entitlements,
        policy=policy,
        now=now,
        opaque=opaque,
    )


def compose(
    text: str,
    redacted: RedactedAnswer,
    *,
    sink: TraceSink,
    now: datetime | None = None,
    reference: str | None = None,
) -> ComposedAnswer:
    """Compose the answer, emit the trace, and return only what a person may see.

    Takes the whole `RedactedAnswer` rather than a payload and a trace separately, because
    the two must not be paired by a caller holding two variables. Returns a `ComposedAnswer`
    that does not contain the trace at all, so the value handed onward cannot leak it.
    """
    del now  # Freshness comes from the payload, which recorded it at fetch time.
    ref = reference or new_trace_ref()

    # The one place a post-redaction payload is allowed to go (M3.9.2). Before composition,
    # so a failure while composing still leaves the trace behind: the runs worth reading are
    # the ones that went wrong.
    sink.emit(ref, redacted.payload, redacted.trace)

    citations = _citations_from(redacted.payload)
    return ComposedAnswer(
        text=text,
        citations=citations,
        payload=redacted.payload,
        trace_ref=ref,
        grounded=bool(redacted.payload.records),
    )
