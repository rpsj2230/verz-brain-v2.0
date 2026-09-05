"""The door, the link, the original in the store, and what happens when ingestion is full.

The decisions these tests are about live in `brain.knowledge.ingest` and are tested there. What
is tested here is the path: that a refusal happens before the bytes are read rather than after,
that a link goes through the address check a skill import goes through rather than a second copy
of it, that an original cannot be written unscanned or into a bucket that expires, and that the
queue's ceilings come off the budget row rather than from a number beside it.

The fetcher and resolver fakes are the same shape as `test_fetch.py`'s, because the cases worth
testing are the same ones: a name that resolves somewhere private, and a redirect to an address
the first check never saw. Neither is reachable against a real resolver on a machine with a
network.

Task ids: M7.1.1, M7.1.2, M7.1.4, M7.1.5
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import get_type_hints

import pytest

from brain.knowledge.ingest import TYPE_LIMITS, IngestRefused, MediaType
from brain.knowledge.scanning import ScannedContent, scan_for_parsing
from brain.knowledge.uploads import (
    ORIGINAL_PREFIX,
    UNNAMED_LINK,
    admit_ingestion,
    assert_declared_length,
    assert_holds_originals,
    assert_safe_filename,
    ceiling_for,
    ingestion_request,
    link_filename,
    original_bucket,
    original_key,
    queue_limits_for,
    read_within,
    receive_link,
    receive_upload,
    store_original,
)
from brain.ops.admission import (
    CapacityState,
    RefusalKind,
    Resource,
    WorkloadClass,
    budget_for,
    refusal_record,
    seed_budgets,
)
from brain.ops.storage import Bucket, StorageError
from brain.tools.fetch import FetchedBytes
from tests.unit.test_scanning import FakeScanner

PDF = b"%PDF-1.7\n1 0 obj\n<< >>\nendobj\n"
ZIP = b"PK\x03\x04\x14\x00\x06\x00\x08\x00\x00\x00!\x00"
MARKDOWN = b"# Escalation runbook\n\nA P1 is acknowledged within thirty minutes.\n"
NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
BUDGETS = seed_budgets()
DOCUMENT_JOBS = (Resource.DOCUMENT_JOBS, "")


class Recorded:
    """A body that reports how much of it was actually consumed.

    Every "refused before anything was read" claim in this file is checked against this rather
    than against a message, because a message can be right while the read already happened.
    """

    def __init__(self, chunks: Sequence[bytes]) -> None:
        self.chunks = list(chunks)
        self.consumed: list[bytes] = []

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self.chunks:
            self.consumed.append(chunk)
            yield chunk


class Resolver:
    """Whatever the test says a name resolves to; anything else is public."""

    def __init__(self, answers: dict[str, Sequence[str]] | None = None) -> None:
        self.answers = answers or {}

    def resolve(self, host: str) -> Sequence[str]:
        return self.answers.get(host, ["93.184.216.34"])


class Hops:
    """A fetcher with a scripted chain: each URL either redirects or returns bytes."""

    def __init__(self, script: dict[str, str | bytes] | None = None) -> None:
        self.script = script or {}
        self.connected: list[tuple[str, str]] = []
        self.max_bytes: list[int] = []

    def get_once(self, url: str, *, address: str, max_bytes: int) -> FetchedBytes | str:
        self.connected.append((url, address))
        self.max_bytes.append(max_bytes)
        answer = self.script.get(url, MARKDOWN)
        if isinstance(answer, bytes):
            return FetchedBytes(body=answer, final_url=url)
        return answer


class FakeStore:
    """An object store that records what was written and refuses to be read from.

    Reading is refused rather than faked because nothing in this module reads, and a fake that
    answers a call nobody makes is a fake that will one day answer it wrongly.
    """

    def __init__(self) -> None:
        self.written: list[tuple[str, str, bytes, str]] = []

    def put_object(self, bucket_name: str, key: str, body: bytes, content_type: str) -> None:
        self.written.append((bucket_name, key, body, content_type))

    def get_object(self, bucket_name: str, key: str) -> bytes:
        raise NotImplementedError

    def delete_object(self, bucket_name: str, key: str) -> None:
        raise NotImplementedError

    def list_objects(self, bucket_name: str, prefix: str) -> Iterator[str]:
        raise NotImplementedError


def _cleared(content: bytes = PDF, filename: str = "sop.pdf") -> ScannedContent:
    received = receive_upload(
        filename=filename, declared_type=MediaType.PDF.value, chunks=[content]
    )
    return scan_for_parsing(received.upload, received.body, scanner=FakeScanner())


# ------------------------------------------------------------- the door (M7.1.1)
def test_an_upload_within_its_ceiling_is_received() -> None:
    """The positive case every refusal below needs. A door that refuses everything satisfies
    all of them, and a knowledge layer that accepts nothing passes its own safety tests."""
    received = receive_upload(
        filename="sop.pdf",
        declared_type=MediaType.PDF.value,
        chunks=[PDF[:8], PDF[8:]],
        content_length=len(PDF),
    )

    assert received.body == PDF
    assert received.upload.media_type is MediaType.PDF
    assert received.upload.size_bytes == len(PDF)


def test_a_declared_length_over_the_ceiling_is_refused_before_a_byte_is_read() -> None:
    """`admit_upload` takes bytes, so on its own it can only refuse a two gigabyte upload once
    the two gigabytes are in memory, which costs exactly what admitting it would have. Delete
    this and the cheapest refusal available stops happening."""
    body = Recorded([MARKDOWN])

    with pytest.raises(IngestRefused, match="nothing was read"):
        receive_upload(
            filename="runbook.md",
            declared_type=MediaType.MARKDOWN.value,
            chunks=body,
            content_length=TYPE_LIMITS[MediaType.MARKDOWN].max_bytes + 1,
        )

    assert body.consumed == []


def test_a_type_outside_the_allowlist_is_refused_before_a_byte_is_read() -> None:
    """Same argument as the length, and the more common case: an unrecognised type is knowable
    from a header. Reading the body first means the refusal costs the transfer."""
    body = Recorded([b"MZ"])

    with pytest.raises(IngestRefused, match="not a type the knowledge layer accepts"):
        receive_upload(filename="setup.exe", declared_type="application/x-dosexec", chunks=body)

    assert body.consumed == []


def test_a_body_is_refused_while_it_is_still_arriving_rather_than_afterwards() -> None:
    """This is the check that does not depend on being told anything. A client that declares a
    kilobyte and sends gigabytes defeats every header check there is, and the only defence is
    stopping mid-stream. The consumed count is the property: reading it all and then measuring
    would pass a message-only assertion."""
    body = Recorded([b"a" * 4] * 10)

    with pytest.raises(IngestRefused, match="still arriving"):
        read_within(body, ceiling=10)

    assert len(body.consumed) == 3


def test_the_door_reads_within_the_ceiling_for_the_declared_type_and_not_the_absolute_one() -> None:
    """The test above proves `read_within` stops; this proves the door hands it the right
    number. Passing the absolute ceiling instead would let a five megabyte type stream a
    hundred megabytes before anything objected, and every other test in this file would still
    pass, because the per-type ceiling is only ever reached by a body this large."""
    ceiling = TYPE_LIMITS[MediaType.MARKDOWN].max_bytes
    body = Recorded([b"# runbook\n", b"a" * ceiling])

    with pytest.raises(IngestRefused, match="still arriving"):
        receive_upload(filename="runbook.md", declared_type=MediaType.MARKDOWN.value, chunks=body)

    assert len(body.consumed) == 2


def test_a_body_exactly_at_its_ceiling_is_read_whole() -> None:
    """The sibling of the test above. An off-by-one that refused at the ceiling rather than
    past it would reject a file whose size is the documented limit, and the documented limit is
    the number somebody sized their export against."""
    assert read_within([b"a" * 5, b"b" * 5], ceiling=10) == b"aaaaabbbbb"


def test_a_missing_declared_length_is_not_a_refusal() -> None:
    """Chunked transfer declares nothing and is the ordinary case for a browser sending a large
    file. Refusing it would take the upload path out of service for exactly the uploads it
    exists for."""
    assert_declared_length(media_type=MediaType.PDF, content_length=None)


def test_a_filename_carrying_a_control_character_is_refused() -> None:
    """The filename is quoted back in every message about this file, and those are read in
    logs and chat clients where a newline is a second line somebody else wrote."""
    with pytest.raises(IngestRefused, match="control character"):
        assert_safe_filename("quarterly\nreport.pdf")


def test_an_ordinary_filename_with_spaces_in_it_is_accepted() -> None:
    """The sibling. `isprintable` is false for a newline and true for a space, and a check that
    refused spaces would refuse most real filenames, which is how it gets deleted rather than
    fixed."""
    assert_safe_filename("Q3 escalation runbook.pdf")


def test_the_ceiling_is_the_lower_of_the_type_and_the_absolute_limit() -> None:
    """The per-type table is the thing somebody edits. The absolute ceiling exists so an edit
    there cannot admit a file the storage tier cannot hold, and taking the minimum is what
    makes that true rather than a sentence in a comment."""
    assert ceiling_for(MediaType.MARKDOWN) == TYPE_LIMITS[MediaType.MARKDOWN].max_bytes
    assert ceiling_for(MediaType.PDF) == TYPE_LIMITS[MediaType.PDF].max_bytes


# ------------------------------------------------------- ingesting a link (M7.1.2)
def test_a_link_to_a_private_address_is_refused_and_nothing_connects() -> None:
    """Ingesting from a link makes this server connect to an address somebody else chose, on a
    host inside the client's network. Delete this and the knowledge layer becomes a second
    front door to the metadata endpoint, one that the skill importer already closed."""
    fetcher = Hops()

    with pytest.raises(IngestRefused, match="address check"):
        receive_link(
            "https://internal.example/notes.md",
            declared_type=MediaType.MARKDOWN.value,
            fetcher=fetcher,
            resolver=Resolver({"internal.example": ["169.254.169.254"]}),
        )

    assert fetcher.connected == []


def test_a_redirect_into_a_private_address_is_refused_after_the_first_hop_passed() -> None:
    """This is the test that proves the whole fetch is reused rather than only its address
    check. A first address that passes and then answers `302 Location: 10.0.0.5` defeats any
    check made once, and following redirects is the default in every client anybody reaches
    for."""
    fetcher = Hops({"https://docs.example/a.md": "https://internal.example/b.md"})

    with pytest.raises(IngestRefused, match="address check"):
        receive_link(
            "https://docs.example/a.md",
            declared_type=MediaType.MARKDOWN.value,
            fetcher=fetcher,
            resolver=Resolver({"internal.example": ["10.0.0.5"]}),
        )

    assert [url for url, _ in fetcher.connected] == ["https://docs.example/a.md"]


def test_a_link_is_fetched_within_the_ceiling_for_the_type_it_declares() -> None:
    """Without this a link could pull fifty megabytes and be refused afterwards for being a
    five megabyte type, which spends the bandwidth and the memory anyway. The ceiling belongs
    on the fetch, not after it."""
    fetcher = Hops()

    receive_link(
        "https://docs.example/runbook.md",
        declared_type=MediaType.MARKDOWN.value,
        fetcher=fetcher,
        resolver=Resolver(),
    )

    assert fetcher.max_bytes == [TYPE_LIMITS[MediaType.MARKDOWN].max_bytes]


def test_a_link_whose_bytes_disagree_with_the_declared_type_is_refused() -> None:
    """A link is where trusting the name is most tempting and least justified: a redirector, a
    share link and an export endpoint all end in whatever the site felt like. The bytes are the
    only evidence there is."""
    fetcher = Hops({"https://docs.example/notes.md": ZIP})

    with pytest.raises(IngestRefused, match="the extension is not the type"):
        receive_link(
            "https://docs.example/notes.md",
            declared_type=MediaType.MARKDOWN.value,
            fetcher=fetcher,
            resolver=Resolver(),
        )


def test_a_public_link_is_received_the_same_way_an_upload_is() -> None:
    """The positive case. Every refusal above is satisfied by a link path that never fetches
    anything, and this is what stops that being an acceptable implementation."""
    received = receive_link(
        "https://docs.example/runbook.md",
        declared_type=MediaType.MARKDOWN.value,
        fetcher=Hops(),
        resolver=Resolver(),
    )

    assert received.body == MARKDOWN
    assert received.upload.filename == "runbook.md"
    assert received.upload.media_type is MediaType.MARKDOWN


def test_a_name_taken_from_a_url_keeps_nothing_that_would_land_oddly_in_a_log() -> None:
    """Nobody chose this string, so it is stripped rather than refused, and it is used for
    display only. Delete this and a URL's last segment goes straight into the message a person
    reads and the log line beside it."""
    assert link_filename("https://ex.example/a/Q3%20plan;rm.pdf?v=1") == "Q320planrm.pdf"
    assert link_filename("https://ex.example/exports/") == UNNAMED_LINK


# ---------------------------------------------------- the original in the store (M7.1.4)
def test_only_scanned_content_can_be_written_to_the_object_store() -> None:
    """The bucket that holds originals also holds console images and report attachments, which
    are served to browsers. Widening this parameter to accept a received upload would make an
    unscanned file storable, and no behavioural test would notice."""
    hints = get_type_hints(store_original)

    assert hints["content"] is ScannedContent


def test_an_original_is_addressed_by_its_digest_and_never_by_its_name() -> None:
    """An object key is read in bucket listings, storage bills and support tickets, none of
    which inherit the document's permissions, and a filename is where a client's name appears.
    A digest key also makes a re-upload of identical bytes the same object rather than a second
    copy under a second name."""
    cleared = _cleared(filename="Acme-redundancy-plan.pdf")

    key = original_key(cleared.upload)

    assert key == f"{ORIGINAL_PREFIX}/{cleared.upload.digest[:2]}/{cleared.upload.digest}"
    assert "Acme" not in key
    assert ".pdf" not in key


def test_an_original_is_written_with_the_type_its_bytes_proved() -> None:
    """A store told a file is a PDF because somebody said so will serve it as one. The declared
    type is a claim the door checked; what is written is the checked answer."""
    cleared = _cleared()
    store = FakeStore()

    stored = store_original(cleared, backend=store)

    assert store.written == [
        (stored.bucket, stored.key, PDF, MediaType.PDF.value),
    ]
    assert stored.digest == cleared.upload.digest
    assert stored.retention_days is None


def test_a_bucket_that_expires_its_contents_cannot_hold_originals() -> None:
    """An original has to outlive every answer that cites it. A document citation deep-links to
    a passage in a file, so a lifecycle window turns every citation over it into a link to
    nothing, silently, on a schedule, long after anybody would connect the two. `lifecycle_gaps`
    does not check this, because it is a rule about originals rather than about buckets."""
    expiring = Bucket(
        name="assets",
        holds="console images",
        retention_days=30,
        retention_reason="a month is plenty for a logo",
        versioned=False,
    )

    with pytest.raises(StorageError, match="outlive every answer"):
        assert_holds_originals(expiring)


def test_a_bucket_readable_without_credentials_cannot_hold_originals() -> None:
    """Checked by calling `lifecycle_gaps` rather than by restating its rules, so public-read
    and versioning stay owned by the module that declares the buckets. Delete this and the
    reuse becomes decoration: nothing would fail if the call were dropped."""
    public = Bucket(
        name="assets",
        holds="console images",
        retention_days=None,
        retention_reason="referenced by documents that outlive any window",
        versioned=False,
        public_read=True,
    )

    with pytest.raises(StorageError, match="cannot hold originals"):
        assert_holds_originals(public)


def test_the_declared_bucket_for_originals_is_fit_to_hold_them() -> None:
    """The sibling of the two refusals above, and the one that would have caught the mistake if
    `bucket_for` had been pointed at the recordings bucket, which expires after thirty days."""
    assert original_bucket().retention_days is None


# --------------------------------------------------------- backpressure (M7.1.5)
def test_ingestion_is_batch_work_and_has_no_parameter_that_could_change_that() -> None:
    """Being BATCH is what yielding means here, and a traffic-class parameter is how it stops
    being true: somebody promotes ingestion on a busy afternoon to clear a backlog, and the
    chat goes slow for a reason nobody can find. The signature is the guarantee."""
    assert list(inspect.signature(ingestion_request).parameters) == ["trace_id"]
    assert ingestion_request("t-1").workload_class is WorkloadClass.BATCH


def test_ingestion_may_never_hold_more_than_half_the_document_job_budget() -> None:
    """This is the reserve, and it is the same one `connectors.backfill` holds back for callers
    who are waiting: held in the shared budget, keyed the same way, so what ingestion leaves
    alone is room the request path can actually use."""
    budget = budget_for(BUDGETS, DOCUMENT_JOBS)

    assert budget is not None
    assert budget.ceiling_for(WorkloadClass.BATCH) * 2 <= budget.limit
    assert budget.ceiling_for(WorkloadClass.BATCH) < budget.ceiling_for(WorkloadClass.INTERACTIVE)


def test_the_in_flight_ceiling_follows_the_budget_row_rather_than_a_number_of_its_own() -> None:
    """Two mechanisms governing one resource drift apart, and the operator watching a stalled
    parse cannot tell which of them stopped it. Deriving the ceiling means the queue and the
    budget cannot disagree; a literal here would pass every other test in this file."""
    widened = [replace(b, limit=9) if b.budget_key == DOCUMENT_JOBS else b for b in BUDGETS]

    assert queue_limits_for(BUDGETS).max_in_flight == 4
    assert queue_limits_for(widened).max_in_flight == 9


def test_ingestion_does_not_proceed_against_a_resource_with_no_budget_row() -> None:
    """An unbudgeted resource is the failure global budgets exist to prevent, and a queue in
    front of one is a queue with no drain. It raises rather than refusing because a refusal
    carries a retry hint and there is nothing to retry until somebody adds a row."""
    with pytest.raises(IngestRefused, match="no document-job budget row"):
        queue_limits_for([b for b in BUDGETS if b.budget_key != DOCUMENT_JOBS])


def test_an_upload_starts_immediately_when_there_is_room() -> None:
    """The positive case for backpressure. Every refusal below is satisfied by an ingestion
    path that refuses everything, and a knowledge layer that never accepts an upload has no
    backpressure problem at all."""
    admission = admit_ingestion(
        trace_id="t-1", budgets=BUDGETS, state=CapacityState(), depth=0, now=NOW
    )

    assert admission.accepted
    assert admission.starts_now
    assert admission.retry_after_seconds == 0.0


def test_an_upload_is_kept_and_queued_when_the_class_share_is_full() -> None:
    """The right way round for work nobody is waiting for: a queue position costs a retry
    nobody is watching. Refusing here instead would make a bulk upload fail as soon as two
    parses were running, which is most of the time."""
    state = CapacityState(used={DOCUMENT_JOBS: 2})

    admission = admit_ingestion(trace_id="t-1", budgets=BUDGETS, state=state, depth=0, now=NOW)

    assert admission.accepted
    assert not admission.starts_now
    assert admission.retry_after_seconds > 0.0


def test_an_upload_is_refused_when_every_document_job_slot_is_held() -> None:
    """The ceiling that is not the class share. When even the interactive reserve is occupied,
    accepting more is accepting work nothing can begin, and the honest answer is to say so now
    rather than to take the file and drop it later."""
    state = CapacityState(used={DOCUMENT_JOBS: 4})

    admission = admit_ingestion(trace_id="t-1", budgets=BUDGETS, state=state, depth=0, now=NOW)

    assert not admission.accepted
    assert admission.retry_after_seconds > 0.0


def test_an_upload_is_refused_when_the_queue_has_no_room_to_wait_in() -> None:
    """A queue that accepts everything is a memory leak with a name. The failure it replaces is
    the invisible one: the uploader is told the document is in the knowledge layer, retrieval
    never finds it, and the symptom is an answer that is merely thin."""
    admission = admit_ingestion(
        trace_id="t-1",
        budgets=BUDGETS,
        state=CapacityState(),
        depth=queue_limits_for(BUDGETS).max_depth,
        now=NOW,
    )

    assert not admission.accepted
    assert not admission.starts_now


def test_a_refused_upload_reports_the_retry_hint_of_whichever_wait_is_longer() -> None:
    """A short hint handed out while a longer limit is also over brings the client straight
    back to be refused again, which is worse than no hint because it spends its trust in the
    next one. The queue's hint is a constant, so only the capacity estimate can be the longer
    of the two."""
    slow = [
        replace(b, mean_service_seconds=300.0) if b.budget_key == DOCUMENT_JOBS else b
        for b in BUDGETS
    ]
    state = CapacityState(used={DOCUMENT_JOBS: 4})

    admission = admit_ingestion(trace_id="t-1", budgets=slow, state=state, depth=0, now=NOW)

    assert not admission.accepted
    assert admission.retry_after_seconds == pytest.approx(225.0)


def test_a_refused_upload_is_logged_in_the_refusal_taxonomy_the_platform_already_has() -> None:
    """An alert about a refused upload has to carry the same `operator_action` as every other
    capacity refusal, or somebody learns a second vocabulary during an incident. The subject
    names the resource that ran out and never who was uploading."""
    state = CapacityState(used={DOCUMENT_JOBS: 4})
    admission = admit_ingestion(trace_id="t-1", budgets=BUDGETS, state=state, depth=0, now=NOW)

    assert admission.log_record() == refusal_record(
        RefusalKind.CAPACITY,
        subject=f"{Resource.DOCUMENT_JOBS}/*",
        detail=admission.reason,
    )


def test_an_accepted_upload_is_logged_as_started_or_queued_and_not_as_a_refusal() -> None:
    """`AdmissionDecision.log_record` reports a queued verdict as a capacity refusal, because
    for it a position is a refusal to start. Passing that through would fill the refusal
    dashboard with uploads that were accepted and are being parsed."""
    state = CapacityState(used={DOCUMENT_JOBS: 2})
    admission = admit_ingestion(trace_id="t-1", budgets=BUDGETS, state=state, depth=0, now=NOW)

    assert admission.log_record()["verdict"] == "queued"
    assert "refusal_kind" not in admission.log_record()
