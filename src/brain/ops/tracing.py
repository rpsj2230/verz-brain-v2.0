"""Traces an operator can read without the trace becoming a second copy of the business.

A trace ledger is the most useful thing to have during an incident and the most dangerous
thing to have afterwards. Langfuse stores whatever is sent to it, indexes it, keeps it for
as long as retention says, and exposes it to whoever can log in. Every control the rest of
this system spent a wave building - field-level redaction, scope predicates, a closed audit
vocabulary - is bypassed by one span carrying the answer text.

So the masking happens **in this process, before the span is constructed**, not in a
Langfuse configuration setting. A server-side mask is a mask that was not applied while
the payload crossed the network, sat in the ingest queue, and was written to the payload
store; and it is a mask that stops existing the moment somebody changes a setting in a UI
nobody reviews. `mask` is a pure function over a span so that the assertion "the canary
never leaves this process" can be written as a test rather than as a hope.

**The allowlist is names, and never values.** `brain.core.redaction` settled this already:
a trace is the one artefact of an answer that outlives the answer, which makes it the worst
possible place to put the thing that was just withheld. So an attribute survives only if
its key is one of a closed set whose values are known to be system vocabulary - a tool
name, an outcome, a capability, a latency - and even then only if the value is a small
scalar that still *looks* like system vocabulary. A dictionary under an allowlisted key is
masked, because a dictionary is where somebody puts a record while meaning to put a
summary. `principal_id` is deliberately not on the list: it identifies a person, and a
trace store is not somewhere a person's movements should be reconstructable by an operator
who cannot read the underlying data.

The value grammar was added because the canary test failed without it, which is worth
recording. A length rule alone let any short string through an allowlisted key, so the one
place a caller is trusted was the one place nothing was checked. The allowlist protects the
key; `VALUE_TOKEN_RE` is what makes the key's trust conditional.

Rejected: masking by pattern - scan the payload for things that look like personal data and
redact those. That is `brain.ops.pii`, it is the right tool for text on its way to a model,
and it is the wrong tool here, because a detector that misses one span has written that
span to a store that keeps it for ninety days. Default-deny costs diagnostic detail and
cannot miss.

**The environment vocabulary is fixed at first ingest, by Langfuse and not by us.** The
first trace that arrives establishes the set of environment values the project will accept,
and an unseen value later is rejected at ingest rather than added. That makes the
vocabulary a decision taken once, in silence, by whichever container started first. So it
is written down here and asserted against the vocabulary the application already validates
against, and a mismatch is a test failure rather than a discovery made in production three
weeks later when staging traces stop arriving.

Contradiction worth stating rather than resolving quietly: the tracker asks for this to be
asserted against "the database enum", and there is no database enum. `brain.app.Settings`
carries a `Literal`, `brain.config.REQUIRED` is keyed by the same three names, and no
migration in this repository creates an environment type. `brain.config.REQUIRED` is what
is asserted against, because it is the vocabulary that already refuses a deployment, and a
second copy in a migration would be a third place for the three names to drift.

**A payload read is audited before it happens, not after.** `read_payload` calls the
recorder first and only then the fetch, so a recorder that fails means a payload that was
never read. The other order is the one that is easy to write and produces an audit trail
that is missing exactly the reads that went wrong.

Rejected: putting these reads in `brain.audit.ledger`. Its `SUBJECT_KINDS` and
`AuditAction` are closed sets, and widening them means every holder of `read:audit.*`
starts seeing trace-payload reads in the client-facing audit view. `brain.ops.deployments`
met this same boundary and refused to widen for the same reason; deciding it differently
here would leave two answers to one question in the same package.

**Nothing here is kept for ever, and that follows from the first paragraph.** A trace
ledger with no expiry becomes the longest-lived copy of the business: it outlives the
records it describes, the permissions that governed them, and the people who could read
them. So every kind of trace record states a window and a reason, and the three windows
nest - an observation may not outlive the trace that is the only way to reach it, and a
blob may not outlive the observation that points at it. An orphaned observation is not a
risk anybody weighed, it is storage nobody can navigate to and everybody keeps paying for.

Task ids: M32.1.1.3, M32.1.2.1, M32.1.2.2, M32.1.2.3, M32.1.2.4, M32.1.2.5
"""

from __future__ import annotations

import enum
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, assert_never

from brain.config import REQUIRED

#: The environment tags a span may carry. Fixed at first ingest by Langfuse, so wrong here
#: means wrong for the life of the project.
TRACE_ENVIRONMENTS: Final[tuple[str, ...]] = ("development", "staging", "production")

#: Attribute keys whose values are system vocabulary rather than business data. Closed, and
#: short on purpose: everything not on it is masked, so the cost of forgetting a key is a
#: less informative trace, and the cost of adding a wrong one is a leak that persists for
#: the retention period.
SAFE_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {
        "abstained",
        "capability",
        "channel",
        "environment",
        "latency_ms",
        "model",
        "outcome",
        "principal_kind",
        "token_count",
        "tool",
        "traffic_class",
        "trace_id",
    }
)

#: An allowlisted key still loses its value above this length. A tool name is short; a
#: hundred-and-twenty-character "outcome" is somebody's sentence, and the key it was filed
#: under does not change what it is.
SAFE_VALUE_MAX_CHARS: Final = 120

#: Attribute keys must be system-shaped. A key is kept unmasked, so a key built by
#: interpolating a client's name would put that name in the trace store through the one
#: channel the value mask does not cover.
ATTRIBUTE_KEY_RE: Final = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")

#: A string value under an allowlisted key must look like system vocabulary, not merely be
#: short. This was found by the canary test rather than by design: with only a length rule,
#: `tool` accepted any twenty-character string, so anything a caller filed under an
#: allowlisted key went straight through. Every real value in that set is a lowercase token
#: - `client.read_summary`, `denied`, `human_interactive`, `read:client.name`,
#: `moonshot/kimi-k2` - and business data is not: a person's name has a capital, a sentence
#: has spaces, an address has commas.
VALUE_TOKEN_RE: Final = re.compile(r"^[a-z0-9][a-z0-9._:/-]*$")

#: Size classes rather than lengths. An operator needs to tell an empty completion from a
#: large one; nobody needs to know a masked string was exactly nine characters, which is
#: the length of an NRIC and of nothing else anybody types into a support ticket.
_SIZE_CLASSES: Final[tuple[tuple[int, str], ...]] = ((0, "empty"), (64, "small"), (1024, "medium"))

#: The Keycloak realm role that may read stored payloads. Deliberately not a member of
#: `brain.identity.roles.Role`: that enum is the six things a person can *be* in this
#: system and adding a seventh is a permission-model change. Reading raw trace payloads is
#: something an operator is granted for an afternoon during an incident and loses again,
#: which is a realm role in the identity provider, not an identity in the platform.
PAYLOAD_ROLE: Final = "brain-langfuse-payload"


class TracingError(Exception):
    """Raised when a span, an environment tag or a payload read is not admissible."""


def assert_environment_vocabulary(known: Iterable[str] | None = None) -> None:
    """Refuse a trace vocabulary that has drifted from the application's own.

    Called from the test suite rather than at startup, deliberately. At startup the only
    available response to a mismatch is to refuse to boot over a label, and a trace label
    is not worth an outage; in a test the response is that somebody fixes it before it
    reaches an ingest endpoint that will remember the wrong answer permanently.
    """
    theirs = set(known) if known is not None else set(REQUIRED)
    ours = set(TRACE_ENVIRONMENTS)
    if ours != theirs:
        msg = (
            f"trace environments {sorted(ours)} do not match the application's "
            f"{sorted(theirs)}; Langfuse fixes this vocabulary at first ingest, so the "
            "mismatch becomes permanent the first time a span arrives"
        )
        raise TracingError(msg)


def assert_environment(value: str) -> str:
    """The one place a span's environment tag is checked, and it refuses rather than defaults.

    Defaulting an unknown value to "production" would file a developer's laptop traces
    beside the client's; defaulting to "development" would hide a production incident in a
    view nobody opens during one.
    """
    if value not in TRACE_ENVIRONMENTS:
        msg = f"environment {value!r} is not one of {list(TRACE_ENVIRONMENTS)}"
        raise TracingError(msg)
    return value


def _size_class(length: int) -> str:
    for limit, name in _SIZE_CLASSES:
        if length <= limit:
            return name
    return "large"


def mask_value(value: object) -> str:
    """What replaces anything not on the allowlist: a shape, never a sample.

    A prefix or a first-and-last-character hint is the usual compromise and it is not one.
    Four characters of an email address, repeated across a few hundred spans, is the
    address; four characters of an NRIC is most of the entropy in it.
    """
    kind = type(value).__name__
    if value is None:
        return "[masked:none]"
    if isinstance(value, str):
        return f"[masked:str/{_size_class(len(value))}]"
    if isinstance(value, (list, tuple, set, frozenset, dict)):
        return f"[masked:{kind}/{_size_class(len(value))}]"
    return f"[masked:{kind}]"


def _keep(key: str, value: object) -> bool:
    """Whether an attribute survives masking untouched.

    Four conditions, all of them necessary. The key is on the allowlist; the value is a
    scalar, because a container under a safe key is a record somebody filed under a summary;
    a string value is short, because length is the only thing separating "denied" from a
    paragraph explaining who was denied what; and it is shaped like system vocabulary,
    because shortness alone let a twenty-seven-character canary through the `tool` key when
    this was first written.
    """
    if key not in SAFE_ATTRIBUTES:
        return False
    # `bool` is a subclass of `int`, so this admits True and False too, which is what an
    # `abstained` attribute is.
    if isinstance(value, int | float):
        return True
    if isinstance(value, str):
        return len(value) <= SAFE_VALUE_MAX_CHARS and bool(VALUE_TOKEN_RE.match(value))
    return False


@dataclass(frozen=True)
class Span:
    """One unit of trace, as this process would send it.

    `payload_in` and `payload_out` are the question and the answer. They are separate
    fields rather than two more attributes because they are the two that are never
    allowlistable, and a shape that lets them be confused with an attribute is a shape in
    which somebody adds them to the allowlist.
    """

    name: str
    environment: str
    attributes: Mapping[str, object] = field(default_factory=dict)
    payload_in: str = ""
    payload_out: str = ""

    def __post_init__(self) -> None:
        assert_environment(self.environment)


def mask(span: Span) -> Span:
    """The masked copy of a span, which is the only thing that may leave this process.

    The payloads are always masked. There is no argument that turns that off: an option to
    send raw payloads is an option that is set to true in one deployment during one
    debugging session and never set back, and the trace store keeps what it was given for
    the whole retention window.

    An attribute whose key is not system-shaped is dropped entirely rather than masked. Its
    value would be masked and its key would not, and the key is the half somebody built by
    interpolation.
    """
    kept: dict[str, object] = {}
    for key, value in span.attributes.items():
        if not ATTRIBUTE_KEY_RE.match(key):
            continue
        kept[key] = value if _keep(key, value) else mask_value(value)
    return Span(
        name=span.name,
        environment=span.environment,
        attributes=kept,
        payload_in=mask_value(span.payload_in),
        payload_out=mask_value(span.payload_out),
    )


# ----------------------------------------------------------------- retention
class TraceRecord(enum.StrEnum):
    """The three things a trace ledger stores, which have three different costs.

    Separated because they are retained separately in Langfuse and because they live in
    different stores: the trace row is in Postgres, the observations are in ClickHouse, and
    anything large is in the blob store. One retention number for all three would be set by
    whichever of those bills arrived first.
    """

    #: The parent record: one question, one answer, who asked, how long it took.
    TRACE = "trace"
    #: The spans under it: each model call, each tool call, each retrieval.
    OBSERVATION = "observation"
    #: Anything too large to inline, in the S3-compatible store.
    BLOB = "blob"


@dataclass(frozen=True)
class Retention:
    """How long one kind of trace record is kept, and why that number.

    `because` is required for the reason `brain.ops.storage.Bucket.retention_reason` is: a
    retention nobody can explain is one that gets extended the first time an investigation
    wants an older trace, and the extension is permanent because nobody knows what the
    original number was protecting.
    """

    record: TraceRecord
    days: int
    because: str

    def __post_init__(self) -> None:
        if self.days < 1:
            # There is no unbounded option and no zero. Zero would mean traces that vanish
            # before anybody can read them, which reads in an incident as the ledger being
            # broken; unbounded is the failure this whole module argues against.
            msg = f"{self.record.value} retention is {self.days} days, which is not a window"
            raise TracingError(msg)
        if not self.because.strip():
            msg = f"{self.record.value} retention states no reason"
            raise TracingError(msg)


RETENTION: Final[tuple[Retention, ...]] = (
    Retention(
        record=TraceRecord.TRACE,
        days=30,
        because=(
            "long enough to investigate anything a client reports in the month it happened, "
            "and short enough that the ledger is never the oldest copy of who asked what"
        ),
    ),
    Retention(
        record=TraceRecord.OBSERVATION,
        days=30,
        because=(
            "the same window as the trace, because a trace whose spans have expired says "
            "that something happened and cannot say what, which is the half nobody needs"
        ),
    ),
    Retention(
        record=TraceRecord.BLOB,
        days=7,
        because=(
            "the shortest of the three: a blob is what was too large to inline, so it is "
            "the most likely of the three to be a copy of a record, and it is only wanted "
            "while somebody is actively looking at the run that produced it"
        ),
    ),
)


def retention_for(record: TraceRecord) -> Retention:
    """The window for this kind of record.

    `assert_never` for the reason `brain.ops.storage.bucket_for` uses it: a fourth kind of
    trace record cannot reach production without somebody deciding how long it is kept. A
    dictionary lookup with a default would give it whatever the default was, and the
    default in every system that has one is "the longest".
    """
    for entry in RETENTION:
        if entry.record is record:
            return entry
    match record:  # pragma: no cover - unreachable while RETENTION is complete
        case TraceRecord.TRACE | TraceRecord.OBSERVATION | TraceRecord.BLOB:
            msg = f"{record.value} has no retention declared"
            raise TracingError(msg)
        case _:
            assert_never(record)


def retention_gaps(windows: Sequence[Retention] | None = None) -> tuple[str, ...]:
    """Every way a set of windows does not hold together.

    Two nesting rules and one closure rule. The nesting is what stops the ledger paying to
    store things nothing can reach: an observation is reached through its trace and a blob
    through its observation, so outliving the parent is not extra safety, it is a bill for
    data with no route to it.

    The windows are a parameter defaulting to the declared set, for the reason
    `brain.ops.queue.concurrency_gaps` takes one: a check that can only ever be run against
    the constant beside it cannot be shown to fail, and a check nobody has seen fail is a
    check nobody knows works.
    """
    declared_windows = RETENTION if windows is None else tuple(windows)
    by_record = {entry.record: entry for entry in declared_windows}
    findings: list[str] = []
    for record in TraceRecord:
        if record not in by_record:
            findings.append(f"{record.value}: no retention declared")
    if set(by_record) != set(TraceRecord):
        return tuple(findings)

    trace = by_record[TraceRecord.TRACE]
    observation = by_record[TraceRecord.OBSERVATION]
    blob = by_record[TraceRecord.BLOB]
    if observation.days > trace.days:
        findings.append(
            f"observations are kept {observation.days} days and traces {trace.days}; "
            "an observation outliving its trace is unreachable and still billed"
        )
    if blob.days > observation.days:
        findings.append(
            f"blobs are kept {blob.days} days and observations {observation.days}; "
            "a blob outliving the observation that points at it is unreachable and still billed"
        )
    return tuple(findings)


def may_read_payloads(realm_roles: Iterable[str]) -> bool:
    """Whether these identity-provider roles admit reading a stored trace payload.

    Exact membership, never a prefix or a substring. `brain-langfuse-payload-readonly`
    looks narrower than the real role and is not it; a substring check would admit it, and
    would also admit whatever a client's own Keycloak administrator names a group next
    week.
    """
    return PAYLOAD_ROLE in set(realm_roles)


@dataclass(frozen=True)
class PayloadRead:
    """The audit row for one look at a stored payload.

    `reason` is required and non-empty. A payload read with no stated reason is a row that
    proves somebody looked and nothing else, and the question asked of this table six months
    later is always why.
    """

    at: datetime
    actor: str
    trace_id: str
    reason: str

    def __post_init__(self) -> None:
        if self.at.tzinfo is None:
            # Same rule as the deployment chain: two operators in two timezones produce a
            # sequence that cannot be ordered, and the ordering is what the row is for.
            msg = "payload read timestamp has no timezone"
            raise TracingError(msg)
        for name in ("actor", "trace_id", "reason"):
            if not str(getattr(self, name)).strip():
                msg = f"payload read has no {name}; an unattributable read is not an audit row"
                raise TracingError(msg)


def read_payload(
    event: PayloadRead,
    record: Callable[[PayloadRead], None],
    fetch: Callable[[], str],
) -> str:
    """Record the read, then perform it. The order is the whole function.

    Fetching first and recording after produces an audit trail that is complete except for
    the reads that failed partway, which are the reads worth having a trail of. Recording
    first means a recorder that is down stops payload reads entirely, and that is the
    correct trade: a payload store nobody can audit is a payload store nobody should be
    reading from.
    """
    record(event)
    return fetch()
