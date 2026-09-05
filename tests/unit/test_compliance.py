"""The compliance layer's mechanics: the export document, the interception, the register
and the breach clock.

The invariant suite next door asserts the rules that block deploy. This file covers the
machinery underneath them, and in particular the arithmetic that looks like plumbing and
decides a legal deadline: which calendar a day belongs to, what an unknown count means, and
what a register refuses to write down.

Task ids: M24.1.6, M24.2.2, M24.2.3, M24.2.4
"""

from __future__ import annotations

import hashlib
from datetime import datetime, time, timedelta

import pytest
from pydantic import ValidationError

from brain.audit.compliance import (
    ASSESSMENT_GUIDELINE_DAYS,
    COMMISSION_NOTIFICATION_DAYS,
    MIN_DISCLOSABLE_COHORT,
    SINGAPORE,
    Assessment,
    Awareness,
    AwarenessBasis,
    AwarenessSource,
    BreachCase,
    DataTier,
    ExceptionGround,
    HarmDetermination,
    Interception,
    InterceptionTally,
    LawfulBasis,
    Notifiability,
    NotificationException,
    ObligationBasis,
    ObligationKind,
    ProcessingRecord,
    ProcessingRegister,
    SensitiveTopic,
    Transport,
    classify_topic,
    deadline_summary,
    intercept,
    open_case,
    render_referral,
)
from brain.audit.export import (
    EXPORT_FORMAT,
    Anchor,
    ExportManifest,
    ExportRefusedError,
    build_export,
    canonical_json,
    render_export,
    verify_document,
)
from brain.audit.ledger import AuditAction, AuditChain
from tests.fixtures.company import NOW, person

ENT = person("u_weiling").entitlement().ent_hash()

#: NOW is 12:00 UTC, which is 20:00 the same day in Singapore, so the fixture clock sits
#: comfortably inside one Singapore day. The cases below that straddle a day boundary build
#: their own timestamps rather than shifting this one.


def a_chain(count: int = 3) -> AuditChain:
    chain = AuditChain()
    for i in range(count):
        chain.append(
            action=AuditAction.GRANT,
            actor_id="u_rupash",
            subject=f"principal:u_subject{i}",
            ent_hash=ENT,
            trace_id=f"trace{i}",
            at=NOW + timedelta(minutes=i),
            details={"hours_remaining": "hours_remaining"},
        )
    return chain


def an_export_document(chain: AuditChain | None = None) -> str:
    return render_export(
        chain if chain is not None else a_chain(),
        exported_by="u_rupash",
        trace_id="trace_export",
        reason_code="regulator_request",
        at=NOW,
    )


def a_record(**kw: object) -> ProcessingRecord:
    base: dict[str, object] = {
        "connector_id": "conn_freshdesk",
        "source_system": "freshdesk",
        "transport": Transport.REST,
        "purpose": "ticket_status_lookup",
        "data_subjects": frozenset({"client_contact"}),
        "categories": frozenset({"contact_name", "ticket_body"}),
        "tier": DataTier.FEDERATED,
        "basis": LawfulBasis.LEGITIMATE_INTERESTS,
        "basis_reference": "dpia_2026_04",
        "basis_decided_by": "u_rupash",
        "basis_decided_at": NOW,
        "reviewed_at": NOW,
    }
    return ProcessingRecord(**(base | kw))  # type: ignore[arg-type]


def a_harm(*, significant: bool, at: datetime | None = None) -> HarmDetermination:
    return HarmDetermination(
        significant_harm=significant,
        decided_by="u_rupash",
        decided_at=at or NOW,
        rationale_reference="assessment_memo_1",
    )


def a_case(**kw: object) -> BreachCase:
    case = open_case(
        case_id="case_1",
        became_aware_at=NOW,
        basis=AwarenessBasis.OBSERVED,
        source=AwarenessSource.INTERNAL_DETECTION,
        recorded_at=NOW + timedelta(days=2),
        recorded_by="u_rupash",
        evidence_reference="alert_88",
    )
    return case.model_copy(update=kw) if kw else case


# =============================================================== export (M24.1.6)
def test_the_document_is_a_manifest_line_followed_by_one_line_per_entry() -> None:
    """The layout is the reader's entire experience of the format. If it changes shape,
    every instruction we have given a regulator about how to check it is wrong, including
    the shell command in the module docstring."""
    document = an_export_document(a_chain(3))
    lines = document.splitlines()

    assert len(lines) == 4
    assert '"format":"brain.audit.export.v1"' in lines[0]
    assert [line.count('"seq":') for line in lines[1:]] == [1, 1, 1]


def test_the_entry_digest_is_a_plain_sha256_of_the_lines_after_the_manifest() -> None:
    """The number is only worth carrying because a reader can reproduce it with a standard
    tool. Wrap it in a length prefix or a domain separator and it becomes a number they
    have to trust us about, which is the thing this whole module exists to avoid."""
    document = an_export_document()
    manifest = ExportManifest.model_validate_json(document.splitlines(keepends=True)[0])
    tail = "".join(document.splitlines(keepends=True)[1:])

    assert manifest.entries_digest == hashlib.sha256(tail.encode("utf-8")).hexdigest()


def test_an_empty_chain_exports_as_a_manifest_with_no_entries() -> None:
    """A ledger window with nothing in it is a real answer to a real request, and an export
    that raised on it would send somebody looking for a bug that is not there. Head falls
    back to the start hash, which is what makes an anchor taken before the first entry
    meaningful."""
    export = build_export(
        AuditChain(),
        exported_by="u_rupash",
        trace_id="trace_export",
        reason_code="regulator_request",
        at=NOW,
    )

    assert export.manifest.entry_count == 0
    assert export.manifest.first_seq is None
    assert export.manifest.last_seq is None
    assert export.manifest.head == export.manifest.start_hash
    assert export.manifest.verified


def test_two_exports_of_the_same_entries_are_byte_identical() -> None:
    """Determinism is what makes the digest mean anything. If serialisation varied with
    dictionary ordering or float formatting, two honest exports of one ledger would
    disagree and a real disagreement would be indistinguishable from that noise."""
    chain = a_chain(4)

    assert an_export_document(chain) == an_export_document(chain)
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


def test_an_anchorless_export_says_so_rather_than_implying_completeness() -> None:
    """The truncation hole is closed by an anchor and by nothing else. An export that
    carries none is still worth producing and must not read as though it proves more than
    continuity."""
    export = build_export(
        a_chain(),
        exported_by="u_rupash",
        trace_id="trace_export",
        reason_code="regulator_request",
        at=NOW,
    )
    assert not export.manifest.anchored

    anchored = build_export(
        a_chain(),
        exported_by="u_rupash",
        trace_id="trace_export",
        reason_code="regulator_request",
        at=NOW,
        anchors=[
            Anchor(
                seq=1,
                entry_hash=a_chain().entries[1].entry_hash,
                recorded_at=NOW,
                recorded_by="u_auditor",
                where="offsite_object_store",
            )
        ],
    )
    assert anchored.manifest.anchored


def test_a_citation_names_the_export_the_entry_the_sequence_and_the_digest() -> None:
    """A filtered view cannot verify itself, so a row in one has to point at something that
    can. Drop any of the four parts and the citation stops being followable: without the
    entries digest the reader does not know which export, and without the entry hash they
    cannot tell whether the row still says what it said."""
    export = build_export(
        a_chain(),
        exported_by="u_rupash",
        trace_id="trace_export",
        reason_code="regulator_request",
        at=NOW,
    )

    citation = export.cite_entry(1)
    assert citation.startswith(EXPORT_FORMAT)
    assert export.manifest.entries_digest in citation
    assert "seq=1" in citation
    assert export.entries[1].entry_hash in citation

    with pytest.raises(ExportRefusedError, match="not in this export"):
        export.cite_entry(99)


def test_a_damaged_document_is_reported_rather_than_raised() -> None:
    """`verify_document` is what the sender runs before handing the document over. A raise
    would be caught by whatever wrapped the call and turned into a generic failure, and the
    one thing the sender needs is which part of the document does not add up."""
    document = an_export_document()
    assert verify_document(document) == (True, "manifest matches the entry block")

    lines = document.splitlines(keepends=True)
    tampered = lines[0] + lines[1].replace("u_rupash", "u_jason") + "".join(lines[2:])
    ok, reason = verify_document(tampered)
    assert not ok
    assert "entries_digest" in reason

    ok, reason = verify_document(lines[0] + "".join(lines[1:-1]))
    assert not ok

    assert verify_document("") == (False, "empty document")


def test_a_manifest_that_disagrees_with_itself_cannot_be_built() -> None:
    """A document claiming verification while carrying a break, or claiming a break and
    naming none, is read as the opposite of what its author meant. This is the only place
    that pairing is checked."""
    kwargs: dict[str, object] = {
        "exported_at": NOW,
        "exported_by": "u_rupash",
        "trace_id": "trace_export",
        "reason_code": "regulator_request",
        "start_hash": "0" * 64,
        "entry_count": 0,
        "head": "0" * 64,
        "entries_digest": "0" * 64,
    }
    with pytest.raises(ValidationError, match="would mislead its reader"):
        ExportManifest(verified=False, **kwargs)  # type: ignore[arg-type]


# ========================================================= interception (M24.2.2)
@pytest.mark.parametrize(
    ("question", "topic"),
    [
        ("How do I raise a grievance?", SensitiveTopic.GRIEVANCE),
        ("i want to file a formal complaint about my team lead", SensitiveTopic.GRIEVANCE),
        ("is this harassment", SensitiveTopic.GRIEVANCE),
        ("my manager keeps bullying me", SensitiveTopic.GRIEVANCE),
        ("she was discriminated against because of her age", SensitiveTopic.GRIEVANCE),
        ("what counts as unfair dismissal", SensitiveTopic.GRIEVANCE),
        ("how do I whistleblow", SensitiveTopic.WHISTLEBLOWING),
        ("whistle-blowing policy", SensitiveTopic.WHISTLEBLOWING),
        ("how do I report suspected fraud in the finance team", SensitiveTopic.WHISTLEBLOWING),
        ("can I make an anonymous report", SensitiveTopic.WHISTLEBLOWING),
        ("what is my salary this month", SensitiveTopic.HR_PERSONAL),
        ("when is my appraisal", SensitiveTopic.HR_PERSONAL),
        ("what happens in a disciplinary hearing", SensitiveTopic.HR_PERSONAL),
        ("how much maternity leave do I get", SensitiveTopic.HR_PERSONAL),
    ],
)
def test_a_sensitive_question_is_recognised_whatever_its_wording(
    question: str, topic: SensitiveTopic
) -> None:
    """The corpus is the control. Every one of these went through the ordinary path before
    this leaf existed, which means it was retrieved over, cached under an entitlement hash
    and written to a store a department admin can open. Delete this and the patterns can
    rot one at a time with nothing noticing."""
    assert classify_topic(question) is topic


@pytest.mark.parametrize(
    "question",
    [
        "how many hours are left on Acme",
        "what is the status of ticket 4471",
        "when does SNM's hosting expire",
        "who manages the Tomato Glasses account",
        "which clients have an open P1 older than five days",
        "draft a summary of last month's maintenance work",
    ],
)
def test_an_ordinary_question_is_not_intercepted(question: str) -> None:
    """The false-positive case, and it is load-bearing. Interception that catches ordinary
    work is interception somebody switches off, and then none of the tests above protect
    anybody."""
    assert classify_topic(question) is None
    assert not intercept(question, trace_id="trace1").handled_privately


def test_recognition_survives_casing_and_spacing() -> None:
    """A question typed in a chat client arrives with whatever whitespace the client felt
    like sending. Matching on the raw string would make the control depend on the channel."""
    assert classify_topic("   GRIEVANCE   ") is SensitiveTopic.GRIEVANCE
    assert classify_topic("How\n do  I\tWhistle Blow") is SensitiveTopic.WHISTLEBLOWING


def test_an_intercepted_request_gets_the_referral_and_an_ordinary_one_gets_nothing() -> None:
    """The interception has to actually replace the answer. A decision object that nothing
    reads is a control that is not running, and it would pass every test about what the
    decision does not disclose."""
    assert intercept("how do I raise a grievance", trace_id="trace1").reply() == render_referral()
    assert intercept("how many hours are left on Acme", trace_id="trace1").reply() is None


def test_a_decision_cannot_claim_to_intercept_without_a_topic() -> None:
    """The two fields have to agree or the tally counts one thing and the gate does another.
    Cheap to check at construction, impossible to find later."""
    with pytest.raises(ValidationError, match="disagree"):
        Interception(trace_id="trace1", handled_privately=True, topic=None)
    with pytest.raises(ValidationError, match="disagree"):
        Interception(trace_id="trace1", handled_privately=False, topic=SensitiveTopic.GRIEVANCE)


def test_a_tally_releases_the_breakdown_only_when_every_bucket_clears_the_threshold() -> None:
    """The three tiers of the report, in one place. A breakdown with a small bucket names
    somebody; a total on its own does not; and below the threshold there is nothing to say
    but that there is nothing to say."""
    quiet = InterceptionTally(period="2026-09", counts={SensitiveTopic.GRIEVANCE: 2})
    assert quiet.report().suppressed
    assert quiet.report().total is None

    mixed = InterceptionTally(
        period="2026-09",
        counts={SensitiveTopic.HR_PERSONAL: 20, SensitiveTopic.WHISTLEBLOWING: 1},
    )
    assert mixed.report().total == 21
    assert mixed.report().by_topic is None
    assert mixed.report().suppressed

    busy = InterceptionTally(
        period="2026-09",
        counts={
            SensitiveTopic.HR_PERSONAL: MIN_DISCLOSABLE_COHORT,
            SensitiveTopic.GRIEVANCE: MIN_DISCLOSABLE_COHORT + 3,
        },
    )
    assert not busy.report().suppressed
    assert busy.report().by_topic == {
        SensitiveTopic.HR_PERSONAL: MIN_DISCLOSABLE_COHORT,
        SensitiveTopic.GRIEVANCE: MIN_DISCLOSABLE_COHORT + 3,
    }


def test_a_tally_is_a_month_and_never_a_day() -> None:
    """A daily tally in a company of this size is a timeline, and a timeline placed beside
    an attendance record identifies the person."""
    with pytest.raises(ValidationError):
        InterceptionTally(period="2026-09-04")
    with pytest.raises(ValidationError):
        InterceptionTally(period="2026-13")


# ============================================================ register (M24.2.3)
def test_a_record_is_refused_when_it_names_no_subject_or_no_category() -> None:
    """An entry with nothing in it satisfies a completeness check while describing nothing,
    which is the most dangerous kind of gap: one that reports as covered."""
    with pytest.raises(ValidationError, match="names no category of data subject"):
        a_record(data_subjects=frozenset())
    with pytest.raises(ValidationError, match="names no category of personal data"):
        a_record(categories=frozenset())


@pytest.mark.parametrize("denied", ["email", "phone", "nric", "bank_details", "salary"])
def test_a_denied_category_cannot_be_stored_in_any_tier_we_control(denied: str) -> None:
    """Section 8 puts these five on a permanent deny list with no exception. The register is
    where an exception would be written down, so it is where the refusal has to live."""
    for tier in (DataTier.LOCAL, DataTier.PROJECTED):
        with pytest.raises(ValidationError, match="permanent deny list"):
            a_record(
                tier=tier,
                categories=frozenset({"contact_name", denied}),
                projected_fields=("record_id",) if tier is DataTier.PROJECTED else (),
                retention_days=30,
            )

    # Federated is the one lawful answer: fetched live for the question and never stored.
    assert a_record(categories=frozenset({denied})).tier is DataTier.FEDERATED


def test_a_federated_connector_may_not_claim_a_retention_period() -> None:
    """Federated means fetched live and never stored. A retention period on a federated
    record means somebody has started keeping it, and the register would be the last place
    anybody looked."""
    with pytest.raises(ValidationError, match="never stored"):
        a_record(tier=DataTier.FEDERATED, retention_days=30)
    with pytest.raises(ValidationError, match="does not say for how long"):
        a_record(tier=DataTier.LOCAL, retention_days=None)


def test_a_projection_is_capped_at_twelve_fields() -> None:
    """The cap from section 8, which is what stops the projection quietly becoming a mirror.
    Nothing else in the codebase enforces the number at the register level."""
    twelve = tuple(f"field_{i}" for i in range(12))
    assert (
        len(
            a_record(
                tier=DataTier.PROJECTED, retention_days=365, projected_fields=twelve
            ).projected_fields
        )
        == 12
    )

    with pytest.raises(ValidationError, match="above the 12"):
        a_record(
            tier=DataTier.PROJECTED,
            retention_days=365,
            projected_fields=(*twelve, "field_12"),
        )
    with pytest.raises(ValidationError, match="not in the projected tier"):
        a_record(tier=DataTier.LOCAL, retention_days=365, projected_fields=("record_id",))


def test_data_leaving_singapore_needs_a_recorded_safeguard() -> None:
    """Section 21 puts the deployment in Singapore. Anything else is a transfer, and a
    transfer with no recorded mechanism is the finding a regulator opens with."""
    with pytest.raises(ValidationError, match="no recorded transfer safeguard"):
        a_record(egress_regions=frozenset({"sg", "us"}))

    allowed = a_record(
        egress_regions=frozenset({"sg", "us"}),
        transfer_safeguard="contractual_clauses",
        transfer_safeguard_reference="dpa_2026_11",
    )
    assert allowed.transfer_safeguard == "contractual_clauses"


def test_a_register_refuses_two_records_for_one_connector() -> None:
    """Two answers to "what does this connector do" is no answer, and the disagreement is
    discovered by whoever is reading the register out to a regulator."""
    with pytest.raises(ValidationError, match="a register with two answers has none"):
        ProcessingRegister(records=(a_record(), a_record(purpose="something_else")))


def test_an_unregistered_or_stale_connector_is_reported_as_a_gap() -> None:
    """A register is judged by its gaps. One that can only list what it holds cannot find
    them, and the missing connector is found by the regulator instead."""
    register = ProcessingRegister(records=(a_record(),))
    later = NOW + timedelta(days=400)

    assert register.gaps(["conn_freshdesk"], now=NOW) == ()
    assert [g.kind for g in register.gaps(["conn_freshdesk"], now=later)] == ["stale"]
    assert [g.connector_id for g in register.gaps(["conn_xero"], now=NOW)] == ["conn_xero"]
    assert [g.kind for g in register.gaps(["conn_xero"], now=NOW)] == ["unregistered"]


def test_a_connector_the_register_does_not_describe_is_not_permitted() -> None:
    """Default-deny, the same rule the field policy applies to an unclassified field. A
    connector with no record has not been assessed, and an unassessed connector processing
    personal data is the thing the register exists to prevent."""
    register = ProcessingRegister(records=(a_record(),))

    assert register.permits("conn_freshdesk", now=NOW)
    assert not register.permits("conn_xero", now=NOW)
    assert not register.permits("conn_freshdesk", now=NOW + timedelta(days=400))


# ======================================================== breach clock (M24.2.4)
def test_an_estimated_awareness_must_carry_the_earliest_it_could_have_been() -> None:
    """An estimate with no lower bound is a guess, and the clock would treat it as a fact.
    The bound is what makes the deadline conservative instead of flattering."""
    with pytest.raises(ValidationError, match="earliest it could have been"):
        Awareness(
            became_aware_at=NOW,
            basis=AwarenessBasis.ESTIMATED,
            source=AwarenessSource.STAFF_REPORT,
            recorded_at=NOW,
            recorded_by="u_rupash",
            evidence_reference="ticket_12",
        )
    with pytest.raises(ValidationError, match="later than the estimate"):
        Awareness(
            became_aware_at=NOW,
            basis=AwarenessBasis.ESTIMATED,
            earliest_possible_at=NOW + timedelta(days=1),
            source=AwarenessSource.STAFF_REPORT,
            recorded_at=NOW + timedelta(days=1),
            recorded_by="u_rupash",
            evidence_reference="ticket_12",
        )
    with pytest.raises(ValidationError, match="it is observed"):
        Awareness(
            became_aware_at=NOW,
            basis=AwarenessBasis.OBSERVED,
            earliest_possible_at=NOW - timedelta(days=1),
            source=AwarenessSource.INTERNAL_DETECTION,
            recorded_at=NOW,
            recorded_by="u_rupash",
            evidence_reference="alert_88",
        )


def test_awareness_cannot_be_recorded_before_it_happened() -> None:
    """Recording precedes nothing. A case whose record predates its own awareness has one
    of the two timestamps wrong, and every deadline computed from it is wrong too."""
    with pytest.raises(ValidationError, match="recorded before it was known"):
        Awareness(
            became_aware_at=NOW,
            basis=AwarenessBasis.OBSERVED,
            source=AwarenessSource.INTERNAL_DETECTION,
            recorded_at=NOW - timedelta(hours=1),
            recorded_by="u_rupash",
            evidence_reference="alert_88",
        )


def test_the_assessment_benchmark_is_thirty_calendar_days_from_awareness() -> None:
    """Thirty days measured in Singapore, expiring at the end of the thirtieth day. Getting
    the day boundary wrong by one moves a benchmark that a console renders as a countdown."""
    case = a_case()
    aware_date = NOW.astimezone(SINGAPORE).date()
    day_after_the_last = aware_date + timedelta(days=ASSESSMENT_GUIDELINE_DAYS + 1)

    assert case.assessment_due_before == datetime.combine(
        day_after_the_last, time.min, tzinfo=SINGAPORE
    )


def test_the_commission_deadline_runs_three_calendar_days_from_the_assessment_day() -> None:
    """The day of the assessment is day zero, so an assessment made at any time on the 4th
    is due by the end of the 7th. An off-by-one here is a missed statutory deadline."""
    assessed_at = datetime(2026, 9, 4, 23, 30, tzinfo=SINGAPORE)
    case = a_case(
        assessment=Assessment(
            assessed_at=assessed_at, harm=a_harm(significant=True), affected_count=3
        )
    )

    assert case.commission_due_before == datetime(2026, 9, 8, tzinfo=SINGAPORE)
    assert COMMISSION_NOTIFICATION_DAYS == 3


def test_the_calendar_day_is_singapores_and_not_the_servers() -> None:
    """An assessment at 09:00 on the 1st in Singapore is 17:00 on 31 August in UTC. Counting
    the day in UTC produces a deadline one day early here, and one day late for a timestamp
    on the other side of midnight. The regulator's calendar is the one that counts."""
    assessed_at = datetime(2026, 9, 1, 1, 0, tzinfo=SINGAPORE)  # 31 Aug 17:00 UTC
    case = a_case(
        assessment=Assessment(
            assessed_at=assessed_at, harm=a_harm(significant=True), affected_count=3
        )
    )

    assert case.commission_due_before == datetime(2026, 9, 5, tzinfo=SINGAPORE)


def test_an_assessment_is_notifiable_on_either_limb_and_not_only_on_both() -> None:
    """Reading the two limbs as a conjunction is the single most likely way to under-notify:
    a breach of significant scale is notifiable whatever the harm judgement says, and a
    breach causing significant harm to one person is notifiable whatever the count is."""
    harm_only = Assessment(assessed_at=NOW, harm=a_harm(significant=True), affected_count=1)
    scale_only = Assessment(assessed_at=NOW, harm=a_harm(significant=False), affected_count=500)
    neither = Assessment(assessed_at=NOW, harm=a_harm(significant=False), affected_count=499)

    assert harm_only.outcome is Notifiability.NOTIFIABLE
    assert scale_only.outcome is Notifiability.NOTIFIABLE
    assert neither.outcome is Notifiability.NOT_NOTIFIABLE


def test_an_undetermined_assessment_starts_no_clock_and_closes_nothing() -> None:
    """The honest third answer. A count nobody has established leaves the scale limb open,
    so the assessment is not finished, the three days have not started, and the case must
    not read as closed."""
    case = a_case(
        assessment=Assessment(assessed_at=NOW, harm=a_harm(significant=False), affected_count=None)
    )

    assert case.assessment.outcome is Notifiability.UNDETERMINED  # type: ignore[union-attr]
    assert case.commission_due_before is None
    assess = next(o for o in case.outstanding(NOW) if o.kind is ObligationKind.ASSESS)
    assert not assess.satisfied
    assert "undetermined" in " ".join(case.findings(NOW))


def test_an_overdue_obligation_and_a_late_one_are_reported_differently() -> None:
    """Two different findings that a single boolean would flatten. "Still not done, and the
    deadline has passed" sends somebody to do it; "done, but late" sends somebody to write
    it up."""
    late_assessment = a_case(
        assessment=Assessment(
            assessed_at=NOW + timedelta(days=40),
            harm=a_harm(significant=True, at=NOW + timedelta(days=40)),
            affected_count=2,
        )
    )
    assess = next(o for o in late_assessment.outstanding(NOW) if o.kind is ObligationKind.ASSESS)
    assert assess.satisfied
    assert assess.satisfied_late
    assert not assess.overdue

    nothing_done = a_case()
    assess = next(
        o
        for o in nothing_done.outstanding(NOW + timedelta(days=40))
        if o.kind is ObligationKind.ASSESS
    )
    assert not assess.satisfied
    assert assess.overdue


def test_a_guideline_benchmark_is_never_labelled_statutory() -> None:
    """Thirty days is the regulator's published expectation, three days is in the Act. A
    console that renders them identically teaches people the statutory one is negotiable."""
    case = a_case(
        assessment=Assessment(assessed_at=NOW, harm=a_harm(significant=True), affected_count=2)
    )
    basis = {o.kind: o.basis for o in case.outstanding(NOW)}

    assert basis[ObligationKind.ASSESS] is ObligationBasis.GUIDELINE
    assert basis[ObligationKind.NOTIFY_COMMISSION] is ObligationBasis.STATUTORY
    assert basis[ObligationKind.NOTIFY_INDIVIDUALS] is ObligationBasis.STATUTORY


def test_notifying_individuals_before_the_commission_is_recorded_as_a_finding() -> None:
    """The duty is to tell individuals at the same time as or after the Commission. A model
    that refused to record the wrong order would be a model people kept the real dates out
    of, so it is recorded and reported instead."""
    case = a_case(
        assessment=Assessment(assessed_at=NOW, harm=a_harm(significant=True), affected_count=2),
        commission_notified_at=NOW + timedelta(days=2),
        individuals_notified_at=NOW + timedelta(days=1),
    )
    individuals = next(
        o for o in case.outstanding(NOW) if o.kind is ObligationKind.NOTIFY_INDIVIDUALS
    )

    assert individuals.out_of_order
    assert "before the Commission" in " ".join(case.findings(NOW + timedelta(days=3)))


def test_individuals_have_no_deadline_of_their_own() -> None:
    """There is no elapsed-time duty to invent here, and inventing one would put a number in
    a console that no instrument supports. The constraint is ordering."""
    case = a_case(
        assessment=Assessment(assessed_at=NOW, harm=a_harm(significant=True), affected_count=2)
    )
    individuals = next(
        o for o in case.outstanding(NOW) if o.kind is ObligationKind.NOTIFY_INDIVIDUALS
    )

    assert individuals.due_before is None
    assert not individuals.overdue


def test_a_recorded_exception_satisfies_the_duty_to_notify_individuals() -> None:
    """A decision not to notify is a decision somebody made, with a name against it and the
    reasoning written down elsewhere. Without this the case never closes and the register of
    open breaches stops meaning anything."""
    case = a_case(
        assessment=Assessment(assessed_at=NOW, harm=a_harm(significant=True), affected_count=2),
        commission_notified_at=NOW + timedelta(days=1),
        individuals_exception=NotificationException(
            ground=ExceptionGround.TECHNOLOGICAL_PROTECTION,
            decided_by="u_rupash",
            decided_at=NOW + timedelta(days=1),
            rationale_reference="encryption_memo_2",
        ),
    )
    individuals = next(
        o for o in case.outstanding(NOW) if o.kind is ObligationKind.NOTIFY_INDIVIDUALS
    )

    assert individuals.satisfied


def test_a_summary_lists_every_case_including_the_ones_with_nothing_wrong() -> None:
    """A case missing from the summary must mean it was not passed in, never that it is
    fine. Dropping the clean ones makes an omission look like an all-clear."""
    clean = a_case(
        assessment=Assessment(assessed_at=NOW, harm=a_harm(significant=False), affected_count=1)
    )
    late = a_case(case_id="case_2")

    summary = deadline_summary([late, clean], NOW + timedelta(days=40))
    assert list(summary) == ["case_1", "case_2"]
    assert summary["case_1"] == ()
    assert summary["case_2"] != ()
