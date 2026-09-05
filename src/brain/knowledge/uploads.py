"""How a file gets into the knowledge layer: the door, the link, the store and the queue.

`brain.knowledge.ingest` holds the decisions in isolation, and says so: nothing in it opens a
socket, spawns a scanner or reads a clock. This is the path those decisions sit on, and it is
a separate module for the reason the layout table gives about limits and the limit store, and
about the cache and its client: **a module that decides policy does not own a client**. The
sniff table and the type ceilings stay testable without a network; the fetch, the object store
and the capacity snapshot arrive here as protocols and parameters.

Four things are load-bearing.

**The size ceiling is enforced while the body is still arriving.** `admit_upload` takes
`bytes`, so by the time it can refuse a 2 GB upload the 2 GB is already in memory and the
refusal has cost exactly what admitting it would have. So there are three checks and they
catch different lies: the declared length before a byte is read, the running total as chunks
arrive, and `admit_upload` on the whole body. A `Content-Length` that overstates is caught by
the first, one that understates by the second, and the type is settled by neither, because a
declared length and a declared type are both claims made by whoever is uploading. This is the
same three-step `brain.tools.extract` uses on an archive, and the reason is identical: a
declared size is a claim the thing makes about itself.

**Ingesting from a link is the same SSRF surface `brain.tools.fetch` was written for, so it is
the same code.** Not a second implementation. See `THE_ADDRESS_CHECK_IS_NOT_COPIED`.

**An original is stored once it is clean and before it is parsed.** The parameter type of
`store_original` is `ScannedContent`, so an unscanned file cannot be written to the object
store at all; and storing before the parse rather than after means a parse failure is
recoverable without asking the person to find the file again. See
`AN_ORIGINAL_IS_STORED_ONLY_ONCE_IT_IS_CLEAN`.

**Backpressure reuses the machine's own budget rather than adding a knob beside it.**
`brain.ops.admission.INGESTION_THROTTLE_IS_THE_CLASS_CEILING` says in as many words that a
separate ingestion knob was considered and rejected, because two mechanisms governing one
resource drift and the operator watching a stalled parse cannot tell which of them stopped it.
So the in-flight ceiling here is *derived* from the document-job budget row rather than
configured next to it, in the same way `admission.SHED_ORDER` is derived from `CLASS_CEILING`,
and the refusal is `RefusalKind.CAPACITY` from the existing taxonomy rather than a new word.
See `INGESTION_CANNOT_BE_PROMOTED_OUT_OF_BATCH` for the half of this that yields.

What is not here: an HTTP route. M7.1.1 says "upload endpoint", and what is written is the
endpoint's decisions and the shape it hands on; `brain.api` owns routes and is not this
change. A route that called `receive_upload` and then `scan_for_parsing` would be four lines,
and it is honest to say those four lines do not exist yet rather than to claim the leaf whole.

Task ids: M7.1.1, M7.1.2, M7.1.4, M7.1.5
"""

from __future__ import annotations

import string
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Final
from urllib.parse import urlsplit

from brain.core.lane import Lane
from brain.gate.context import TrafficClass
from brain.knowledge.ingest import (
    ABSOLUTE_MAX_BYTES,
    TYPE_LIMITS,
    AdmittedUpload,
    IngestRefused,
    MediaType,
    QueueDecision,
    QueueLimits,
    admit_to_queue,
    admit_upload,
)
from brain.knowledge.scanning import ScannedContent
from brain.ops.admission import (
    AdmissionDecision,
    AdmissionRequest,
    Budget,
    CapacityState,
    RefusalKind,
    Resource,
    budget_for,
    decide,
    refusal_record,
)
from brain.ops.storage import (
    Bucket,
    ObjectKind,
    StorageBackend,
    StorageError,
    bucket_for,
    lifecycle_gaps,
)
from brain.tools.fetch import Fetcher, Resolver, fetch
from brain.tools.skills import SkillError

# ------------------------------------------------------------------ written-down reasons
#: Why a link is fetched through the skill importer's address check rather than a local one.
THE_ADDRESS_CHECK_IS_NOT_COPIED = (
    "Ingesting from a link means somebody hands this server an address and the server "
    "connects to it, which is precisely the surface brain.tools.fetch was written for: "
    "private and link-local ranges, IPv4-mapped IPv6, an unbracketed IPv6 literal that "
    "parses as a hostname and is then resolved, credentials smuggled into the URL, and a "
    "redirect to an address the first check never saw. Two implementations of that check do "
    "not stay the same. The one that drifts is the one nobody is looking at, and the drift is "
    "silent until the day something reaches a metadata endpoint through it. So fetch is "
    "called, redirect chain and all, and the accepted cost is that its refusals are worded "
    "for a skill import. Wording is the cheaper thing to be wrong about."
)

#: Why an original reaches the object store only after a scanner has cleared it.
AN_ORIGINAL_IS_STORED_ONLY_ONCE_IT_IS_CLEAN = (
    "store_original takes ScannedContent, so writing an unscanned upload into the object "
    "store is not an expression that type-checks. The bucket that holds originals also holds "
    "console images and report attachments, which are served to browsers, and a prefix is not "
    "isolation. The cost is real and accepted: a file refused by the scanner is not retained, "
    "so nobody can inspect it afterwards. A quarantine bucket is the right answer to that and "
    "it is a decision for brain.ops.storage, whose bucket set is closed on purpose and whose "
    "every bucket has to state a retention nobody has yet stated for known-infected bytes."
)

#: Why the key is a digest and carries neither the filename nor its extension.
THE_KEY_IS_THE_DIGEST_AND_NOT_THE_NAME = (
    "An object key is read in bucket listings, storage bills and support tickets, none of "
    "which inherit the document's permissions, and a filename is the likeliest place a "
    "client's name appears: Acme-redundancy-plan.pdf discloses the plan without anybody "
    "opening it. A digest key also makes a re-upload of identical bytes the same object "
    "rather than a second copy under a second name, and it cannot be steered by whoever chose "
    "the filename. The extension is left off for the reason the door refuses to trust it: the "
    "extension is a claim, and a claim we declined to believe has no business in an "
    "object name."
)

#: Why ingestion has no priority of its own and cannot be given one.
INGESTION_CANNOT_BE_PROMOTED_OUT_OF_BATCH = (
    "ingestion_request takes a trace id and nothing else, so there is no parameter through "
    "which ingestion can be declared interactive on a busy afternoon. That is the guarantee "
    "rather than an accident of the body, in the same way brain.tools.skills.execution_tool "
    "takes no arguments. Being BATCH is what yielding means here: BATCH may hold half of any "
    "budget, so half of the document-job budget is unavailable to ingestion however much is "
    "queued, which is the same reserve connectors.backfill holds back for callers who are "
    "waiting, held in the same shared budget and keyed the same way."
)


# ------------------------------------------------------------- the door (M7.1.1)
#: The longest filename accepted. Long enough for anything anybody types, short enough that
#: the message it is echoed into stays readable in a chat client.
MAX_FILENAME_CHARS: Final = 200

#: What survives being taken out of a URL for display. Deliberately narrower than what a
#: filesystem allows, because this string is never a path: it is shown to a person and written
#: to a log, and both of those have characters that mean something.
_NAME_CHARS: Final[frozenset[str]] = frozenset(string.ascii_letters + string.digits + "._-")


def accepted_type(declared_type: str) -> MediaType:
    """Resolve a declared type against the allowlist, before there are any bytes.

    Resolved here and again inside `admit_upload`, and the duplication is deliberate rather
    than an oversight. The door has to be able to refuse a type before a single byte has been
    read, and `admit_upload` must not depend on having been preceded by anything: it is called
    directly by tests and it is the function that refuses a rename. There is one allowlist
    either way, because the allowlist is the enum.
    """
    try:
        return MediaType(declared_type)
    except ValueError as exc:
        msg = f"{declared_type!r} is not a type the knowledge layer accepts"
        raise IngestRefused(msg) from exc


def ceiling_for(media_type: MediaType) -> int:
    """The most this type may weigh, taking whichever of the two ceilings is lower.

    Both, because they answer different questions and the per-type table is the one somebody
    edits. `ABSOLUTE_MAX_BYTES` exists so that a mistake in a row cannot admit a file the
    storage tier cannot hold, and taking the minimum is what makes that true rather than
    aspirational.
    """
    return min(TYPE_LIMITS[media_type].max_bytes, ABSOLUTE_MAX_BYTES)


def assert_safe_filename(filename: str) -> None:
    """Refuse a name that would not survive being put in a message.

    A filename is echoed back in every refusal and in `ParseFailure.message`, and those land
    in logs, alerts and chat clients. A newline in one of them is a second log line that
    somebody wrote; a control character is a terminal escape. Refused rather than stripped,
    because the uploader chose the name and can choose another, and silently renaming somebody
    else's file is how the file they look for later is not there.

    `str.isprintable` is the whole check: it is false for every control character and for a
    newline, and true for a space, which is what a real filename contains.
    """
    if not filename.strip():
        msg = "an upload arrives with a filename; an unnamed file cannot be reported on later"
        raise IngestRefused(msg)
    if len(filename) > MAX_FILENAME_CHARS:
        msg = (
            f"that filename is {len(filename)} characters, over the {MAX_FILENAME_CHARS} "
            "this will echo back into a message; rename the file and upload it again"
        )
        raise IngestRefused(msg)
    if not filename.isprintable():
        msg = (
            "that filename contains a control character. It is quoted back in every message "
            "about this file, and those are read in logs and chat clients where a newline is "
            "a second line somebody else wrote"
        )
        raise IngestRefused(msg)


def assert_declared_length(*, media_type: MediaType, content_length: int | None) -> None:
    """Refuse an oversized upload before reading a byte of it (M7.1.1).

    A missing length is not a refusal. Chunked transfer declares nothing, and it is the
    ordinary case for a browser streaming a large file; the running total in `read_within` is
    the check that does not depend on being told anything, and this one only saves the
    transfer when the client was honest about a size it should not have sent.
    """
    if content_length is None:
        return
    if content_length < 0:
        msg = f"a declared length of {content_length} is not a size"
        raise IngestRefused(msg)
    ceiling = ceiling_for(media_type)
    if content_length > ceiling:
        msg = (
            f"this upload declares {content_length} bytes and the ceiling for "
            f"{media_type.value} is {ceiling}; nothing was read"
        )
        raise IngestRefused(msg)


def read_within(chunks: Iterable[bytes], *, ceiling: int) -> bytes:
    """Read a body, stopping the moment it is over the ceiling.

    The check is after each chunk rather than after each byte, so what is actually held is at
    most one chunk past the ceiling and the transport's chunk size is what bounds the
    overshoot. Tightening that would mean slicing every chunk to the remaining allowance,
    which buys a few kilobytes and costs a copy of every upload.

    What this catches that the declared length cannot is a client that declares a kilobyte and
    sends gigabytes, which is the only version of that lie worth defending against: the other
    direction wastes the liar's bandwidth and nobody else's.
    """
    buffer = bytearray()
    for chunk in chunks:
        buffer += chunk
        if len(buffer) > ceiling:
            msg = (
                f"this upload passed {ceiling} bytes while it was still arriving, so the rest "
                "was not read; the declared length said otherwise"
            )
            raise IngestRefused(msg)
    return bytes(buffer)


@dataclass(frozen=True)
class ReceivedUpload:
    """An admitted upload and the bytes it was admitted on, before anything has scanned them.

    This is the unscanned side of the boundary and `brain.knowledge.scanning.ScannedContent`
    is the scanned side, and they are two types rather than one field because that is the
    whole ordering property: a parser takes the second and cannot be handed the first.

    The body travels with the admission because `AdmittedUpload` deliberately carries a digest
    and no bytes, and something has to hold them between the door and the scanner. Keeping the
    two together means the scanner can prove the buffer it was given is the buffer the door
    measured, which is the check `scan_for_parsing` makes.
    """

    upload: AdmittedUpload
    body: bytes


def receive_upload(
    *,
    filename: str,
    declared_type: str,
    chunks: Iterable[bytes],
    content_length: int | None = None,
) -> ReceivedUpload:
    """Take one upload at the door: name, ceiling, bytes, then the type the bytes prove.

    The order is the design. Everything that can be refused without reading is refused first,
    so the cost of a refusal is a header rather than a transfer, and the sniff is last because
    it is the only check that needs the file. `admit_upload` is what settles the type, and it
    is called rather than reimplemented, so a rename is refused in exactly one place.
    """
    assert_safe_filename(filename)
    media_type = accepted_type(declared_type)
    assert_declared_length(media_type=media_type, content_length=content_length)
    body = read_within(chunks, ceiling=ceiling_for(media_type))
    upload = admit_upload(filename=filename, declared_type=declared_type, content=body)
    return ReceivedUpload(upload=upload, body=body)


# ------------------------------------------------------- ingesting a link (M7.1.2)
#: The longest name taken out of a URL. Shorter than an upload's, because it was not chosen by
#: anybody and its only job is to be recognisable in a message.
MAX_LINK_FILENAME_CHARS: Final = 80

#: What a link with no usable last segment is called. A name is needed because every refusal
#: and every parse failure quotes one.
UNNAMED_LINK: Final = "the linked file"


def link_filename(url: str) -> str:
    """A display name from a URL's last path segment. Never a type, never a key.

    A URL has no filename; it has a path whose last segment usually looks like one. So this is
    for showing a person which link failed, and it is used for nothing else: the type comes
    from the bytes, the object key comes from the digest, and this string is not resolved
    against a filesystem anywhere.

    Kept to letters, digits, dot, underscore and hyphen by dropping everything else rather
    than by refusing, which is the opposite of `assert_safe_filename` and deliberately so.
    Nobody chose this name, so there is nobody to send back to rename it, and refusing the
    ingestion because a site's URL contains a percent sign would be refusing the wrong thing.
    """
    last = urlsplit(url).path.rpartition("/")[2]
    kept = "".join(ch for ch in last if ch in _NAME_CHARS)[:MAX_LINK_FILENAME_CHARS]
    return kept or UNNAMED_LINK


def receive_link(
    url: str,
    *,
    declared_type: str,
    fetcher: Fetcher,
    resolver: Resolver,
) -> ReceivedUpload:
    """Fetch a link and take it at the same door an upload comes through (M7.1.2).

    `brain.tools.fetch.fetch` does the connecting, and it is reused rather than reproduced:
    see `THE_ADDRESS_CHECK_IS_NOT_COPIED`. That call carries the whole of it, including the
    part that is easiest to leave out of a second copy, which is re-running the address rules
    on every redirect rather than on the first address only.

    The declared type is still required and still has to be proved by the bytes. A link is the
    case where trusting the name is most tempting, because the path ends in something that
    looks like a filename, and it is the case where the name is least connected to the file: a
    redirector, a share link and an export endpoint all end in whatever the site felt like.

    The type's ceiling is passed to the fetch, so a link cannot pull fifty megabytes to be
    refused afterwards for being a five megabyte type, and the size is enforced against the
    bytes received rather than against a `Content-Length` the far end supplied.
    """
    media_type = accepted_type(declared_type)
    try:
        fetched = fetch(url, fetcher=fetcher, resolver=resolver, max_bytes=ceiling_for(media_type))
    except SkillError as exc:
        msg = f"that link was refused by the address check a skill import goes through: {exc}"
        raise IngestRefused(msg) from exc
    filename = link_filename(fetched.final_url or url)
    upload = admit_upload(filename=filename, declared_type=declared_type, content=fetched.body)
    return ReceivedUpload(upload=upload, body=fetched.body)


# --------------------------------------------------- the original in the store (M7.1.4)
#: Where an ingested original lives, expressed as what it is rather than as a bucket name, so
#: `brain.ops.storage.bucket_for` decides and this module does not.
#:
#: `KNOWLEDGE_ORIGINAL`, which was added for this. It filed under `AGENT_ATTACHMENT` first,
#: with the mismatch written here rather than hidden, because `brain.ops.storage` was outside
#: the files this module's author could change and a bucket name written inline is the worse
#: alternative anybody reaches for.
#:
#: Naming it properly cost one enum member and one match arm, and `assert_never` in
#: `bucket_for` named the site that had to be updated. The two kinds share the assets bucket
#: today, which is exactly why the mismatch was worth closing: while they agree, nothing goes
#: wrong, and the day their retention differs the shared member is the reason nobody notices.
ORIGINAL_OBJECT_KIND: Final = ObjectKind.KNOWLEDGE_ORIGINAL

#: The prefix originals live under. A prefix is organisation, not isolation; see
#: `AN_ORIGINAL_IS_STORED_ONLY_ONCE_IT_IS_CLEAN` for what actually keeps hostile bytes out.
ORIGINAL_PREFIX: Final = "knowledge/originals"


@dataclass(frozen=True)
class StoredOriginal:
    """Where an original went and how long it stays.

    `retention_days` is carried rather than looked up again so that whatever recorded this can
    be compared with the bucket later. A lifecycle rule changed in a console is invisible from
    inside the application, which is the failure `brain.ops.storage` was written against, and a
    stored copy of what the rule was when the file was written is what a reconciliation job
    compares against.
    """

    bucket: str
    key: str
    digest: str
    retention_days: int | None


def assert_holds_originals(candidate: Bucket) -> None:
    """Refuse a bucket that is not fit to hold the file a citation points at.

    `lifecycle_gaps` is called rather than restated, so public-read and versioning are checked
    by the module that owns those rules. The check this adds is the one that module cannot
    make, because it is about originals rather than about buckets: **an original must outlive
    every answer that cites it.** A document citation deep-links to a passage in a file, and a
    lifecycle rule that expires the file turns every citation over it into a link to nothing,
    silently, on a schedule, long after anybody would connect the two.

    So an expiring bucket is refused here rather than being quietly used. If somebody gives
    `assets` a retention window, ingestion stops with a message naming why, which is the loud
    failure; the quiet one is a knowledge layer that gradually stops being able to show its
    working.
    """
    gaps = lifecycle_gaps([candidate])
    if gaps:
        msg = f"bucket {candidate.name!r} cannot hold originals: {'; '.join(gaps)}"
        raise StorageError(msg)
    if candidate.retention_days is not None:
        msg = (
            f"bucket {candidate.name!r} expires objects after {candidate.retention_days} days, "
            "and an original has to outlive every answer that cites it; a citation over an "
            "expired file is a deep link to nothing and nothing reports it"
        )
        raise StorageError(msg)


def original_bucket() -> Bucket:
    """The bucket originals go to, checked every time rather than once at import.

    Checked on the way past because `BUCKETS` is a declaration and the live store is the fact.
    `lifecycle_gaps` takes its buckets as a parameter for the same reason, and its docstring
    says it: a check that can only ever run against the constant beside it cannot be shown to
    fail.
    """
    candidate = bucket_for(ORIGINAL_OBJECT_KIND)
    assert_holds_originals(candidate)
    return candidate


def original_key(upload: AdmittedUpload) -> str:
    """Where these bytes live, addressed by what they are rather than by what they were called.

    See `THE_KEY_IS_THE_DIGEST_AND_NOT_THE_NAME`. The two-character fan-out is the convention
    every content-addressed store uses and it is here for the ordinary reason: a single flat
    prefix holding every document a company has ever uploaded is a listing nobody can page.
    """
    digest = upload.digest
    return f"{ORIGINAL_PREFIX}/{digest[:2]}/{digest}"


def store_original(content: ScannedContent, *, backend: StorageBackend) -> StoredOriginal:
    """Write the original to the object store, after the scan and before the parse (M7.1.4).

    The parameter is `ScannedContent`, which is the whole of the ordering: an unscanned upload
    cannot be written here because it cannot be spelled. See
    `AN_ORIGINAL_IS_STORED_ONLY_ONCE_IT_IS_CLEAN`.

    Before the parse rather than after, because a parse failure that lost the file makes the
    remedy "find that document again and upload it", and the remedies in `CAUSE_TEXT` are
    written on the assumption that the file is still here. A re-parse after a parser upgrade
    is the same argument at a longer timescale.

    The content type written is the media type the door proved, never the one that was
    declared. A store that is told a file is a PDF because somebody said so will serve it as
    one.
    """
    bucket = original_bucket()
    key = original_key(content.upload)
    backend.put_object(bucket.name, key, content.body, content.upload.media_type.value)
    return StoredOriginal(
        bucket=bucket.name,
        key=key,
        digest=content.upload.digest,
        retention_days=bucket.retention_days,
    )


# --------------------------------------------------------- backpressure (M7.1.5)
def ingestion_request(trace_id: str) -> AdmissionRequest:
    """One ingestion asking the machine for room.

    **There is no traffic-class parameter and there must not be one.** See
    `INGESTION_CANNOT_BE_PROMOTED_OUT_OF_BATCH`. `TrafficClass.SYSTEM` is what makes the
    workload class BATCH, and BATCH is what caps ingestion at half of any budget, which is how
    a bulk upload cannot take the slot a person's question needs.

    `Lane.TASK` is the honest description rather than a lever: `workload_class_for` lowers and
    never raises, so the lane cannot promote this out of BATCH either.
    """
    return AdmissionRequest(
        trace_id=trace_id,
        lane=Lane.TASK,
        traffic_class=TrafficClass.SYSTEM,
        resource=Resource.DOCUMENT_JOBS,
    )


def queue_limits_for(budgets: Sequence[Budget]) -> QueueLimits:
    """The ingestion queue's ceilings, derived from the budget rather than set beside it.

    Only `max_in_flight` is decided here, and it is `Budget.limit` rather than the BATCH share
    of it. The two numbers answer two questions at two moments. The class share decides whether
    *this* parse may start, and a parse that cannot start yet is queued rather than refused,
    because nobody is waiting for it. The full limit decides whether an arriving upload has any
    prospect of being started by anything at all: when every document-job slot is held, even
    the share reserved for interactive work, accepting more is accepting work we cannot begin.

    Depth and the retry hint are `QueueLimits`'s own defaults and are not restated. A second
    copy of a number is a second number, and the one that gets edited is never the one being
    read.
    """
    budget = budget_for(budgets, (Resource.DOCUMENT_JOBS, ""))
    if budget is None:
        msg = (
            "there is no document-job budget row, so nothing can say how much parsing this "
            "machine may do; an unbudgeted resource is the failure global budgets exist to "
            "prevent and ingestion does not proceed against one"
        )
        raise IngestRefused(msg)
    return QueueLimits(max_in_flight=budget.limit)


@dataclass(frozen=True)
class IngestionAdmission:
    """Whether this upload is kept, whether it starts now, and what to tell whoever sent it.

    Two booleans rather than one, because they are two different promises and collapsing them
    loses the useful half. `accepted` means the file is ours now and will be parsed. `starts_now`
    means a parse slot exists this instant. An uploader needs the first; a console showing a
    queue needs the second.

    There is no field for the queue depth or for a position, and `QueueDecision` gives the
    reason: a position is a count of other people's work, it moves backwards as often as
    forwards, and nobody watching it learns anything they can act on.
    """

    accepted: bool
    starts_now: bool
    retry_after_seconds: float
    reason: str
    #: The capacity half, kept whole so a caller can read the budget and the class off it.
    capacity: AdmissionDecision
    #: The queue-depth half.
    queue: QueueDecision

    def log_record(self) -> Mapping[str, str]:
        """The operator-facing line, in the refusal taxonomy the rest of the platform uses.

        `refusal_record` rather than a string of this module's own, so an alert about a refused
        upload carries the same `operator_action` as every other capacity refusal and nobody
        has to learn a second vocabulary during an incident. The subject names the resource
        that ran out and never who was uploading, which is the rule that function states.
        """
        if not self.accepted:
            return refusal_record(
                RefusalKind.CAPACITY,
                subject=f"{Resource.DOCUMENT_JOBS}/*",
                detail=self.reason,
            )
        return MappingProxyType(
            {
                "verdict": "started" if self.starts_now else "queued",
                "resource": str(Resource.DOCUMENT_JOBS),
                "workload_class": str(self.capacity.workload_class),
            }
        )


def admit_ingestion(
    *,
    trace_id: str,
    budgets: Sequence[Budget],
    state: CapacityState,
    depth: int,
    now: datetime,
) -> IngestionAdmission:
    """Decide whether one more upload joins the ingestion queue (M7.1.5).

    Two questions, asked of the two modules that already answer them.

    `brain.ops.admission.decide` answers "may a parse start", against the document-job budget
    and BATCH's share of it. Ingestion is never a person waiting, so its answer over the
    ceiling is a position rather than a refusal, which is the right way round: a queue position
    costs a retry nobody is watching.

    `brain.knowledge.ingest.admit_to_queue` answers "is there room to wait". This is the
    question a queue exists to refuse, and refusing it is the only lever there is: accepting
    and dropping later is the failure nobody can see from outside, where the uploader is told
    the document is in the knowledge layer, retrieval never finds it, and the symptom is an
    answer that is merely thin.

    **The limits are resolved first, so an unbudgeted resource raises rather than refusing.**
    There was a `Verdict.SHED` branch here returning a refusal for that case, and it was dead:
    `decide` sheds only when there is no budget row or when somebody is waiting, ingestion is
    never somebody waiting, and no budget row is the same condition `queue_limits_for` raises
    on. A clause that reads as a guard and guards nothing is worse than its absence. An
    exception is also the more honest shape for it: a refusal carries a retry hint, and there
    is nothing to retry until an operator adds a row.

    The hint on a refusal is the longer of the two, matching `brain.ops.limits.check`. A short
    hint handed out while a longer limit is also over brings the client straight back to be
    refused again, which is worse than no hint because it spends its trust in the next one.
    """
    limits = queue_limits_for(budgets)
    request = ingestion_request(trace_id)
    capacity = decide(request, budgets, state, now=now)
    queue = admit_to_queue(
        depth=depth,
        in_flight=state.used_for(request.budget_key),
        limits=limits,
    )
    capacity_hint = capacity.retry_after_seconds or 0.0

    if not queue.admitted:
        return IngestionAdmission(
            accepted=False,
            starts_now=False,
            retry_after_seconds=max(float(queue.retry_after_seconds), capacity_hint),
            reason=queue.reason,
            capacity=capacity,
            queue=queue,
        )

    return IngestionAdmission(
        accepted=True,
        starts_now=capacity.admitted,
        retry_after_seconds=0.0 if capacity.admitted else capacity_hint,
        reason=capacity.reason,
        capacity=capacity,
        queue=queue,
    )
