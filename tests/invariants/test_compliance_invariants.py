"""The compliance rules that must never break. A failure here blocks deploy.

Three families, and they fail in three different directions.

**The export must prove itself to a stranger.** The ledger's hash chain is only worth
building if somebody who trusts none of our code can check it, so the load-bearing test
here is the one that implements a verifier from the exported recipe and nothing else, and
asserts it reproduces the ledger's own digests. If that test ever needs to import something
from `brain.audit.ledger` to pass, the export has stopped being self-describing and the
document has become a set of numbers a regulator has to take on faith.

**The interception must not be the disclosure.** These are canaries in the manner of the
permission ones next door, inverted from ordinary tests: they ask for the signal and fail if
it arrives. What is being protected is not the answer to an HR question, it is the fact that
a particular person asked one, and that fact leaks through a trace note, a metric label, a
differing referral or a report with one small bucket in it long before it leaks through a
field value.

**The clock must not flatter us.** Every property here is one where the comfortable
implementation and the correct one differ, and the comfortable one always reports more time
than there is: starting at confirmation rather than at credible awareness, taking an
estimate instead of its lower bound, treating an unknown count as zero, counting the days on
the server's calendar, or computing a judgement a person is required to make.

Task ids: M24.1.6, M24.2.2, M24.2.3, M24.2.4
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from brain.audit.compliance import (
    MIN_DISCLOSABLE_COHORT,
    SIGNIFICANT_SCALE_THRESHOLD,
    SINGAPORE,
    Assessment,
    AwarenessBasis,
    AwarenessSource,
    BreachCase,
    DataTier,
    Disclosure,
    HarmDetermination,
    InterceptionTally,
    LawfulBasis,
    Notifiability,
    ObligationKind,
    ProcessingRecord,
    ProcessingRegister,
    SensitiveTopic,
    Transport,
    classify_topic,
    intercept,
    open_case,
    render_referral,
)
from brain.audit.export import (
    Anchor,
    ExportManifest,
    ExportRefusedError,
    build_export,
    entries_digest,
    render_export,
)
from brain.audit.ledger import AuditAction, AuditChain
from tests.fixtures.company import CANARIES, NOW, canary_tokens, person

pytestmark = pytest.mark.invariant

ENT = person("u_weiling").entitlement().ent_hash()

#: Questions that must never reach the ordinary path. Every one of them is a real sentence
#: an employee types, and every one of them is a disclosure about that employee before a
#: single field is read.
SENSITIVE_QUESTIONS: tuple[str, ...] = (
    "how do I raise a grievance about my manager",
    "I want to report suspected fraud in the finance team",
    "is what my supervisor said to me harassment",
    "how do I whistleblow anonymously",
    "what happens at a disciplinary hearing",
    "when is my appraisal and what is my increment",
)

ORDINARY_QUESTIONS: tuple[str, ...] = (
    "how many hours are left on Acme",
    "what is the status of ticket 4471",
    "when does SNM's hosting expire",
)

#: The probe used against every field that is supposed to refuse free text. A canary token
#: on its own is the wrong instrument here: `CANARY-PERF-R3NDQ` is a legal *identifier*
#: under the ledger's grammar, which admits mixed case, digits and hyphens because real
#: record ids in this system look like that. A sentence is not, under any of the grammars,
#: and a sentence is what "somebody attached a helpful note" actually looks like.
#:
#: What this therefore does not prove, stated so it is not mistaken for more: an
#: identifier-shaped field will still accept an identifier-shaped value, and some real
#: values are identifier-shaped. That limit is the ledger's too, and it is why these fields
#: hold references to principals rather than anything about them.
PROSE = f"exported because {CANARIES['hr.performance_note']}, she complained about her manager"


def a_chain(count: int = 5) -> AuditChain:
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


def a_case(**kw: object) -> BreachCase:
    case = open_case(
        case_id="case_1",
        became_aware_at=NOW,
        basis=AwarenessBasis.OBSERVED,
        source=AwarenessSource.INTERNAL_DETECTION,
        recorded_at=NOW + timedelta(days=1),
        recorded_by="u_rupash",
        evidence_reference="alert_88",
    )
    return case.model_copy(update=kw) if kw else case


def a_harm(*, significant: bool) -> HarmDetermination:
    return HarmDetermination(
        significant_harm=significant,
        decided_by="u_rupash",
        decided_at=NOW,
        rationale_reference="assessment_memo_1",
    )


# ================================================ the export proves itself (M24.1.6)
def test_a_third_party_can_recompute_every_digest_from_the_recipe_alone() -> None:
    """The whole reason the export exists, and the only test here that would be worth
    keeping if every other one were deleted.

    The verifier below is written the way a stranger writes one: it reads the recipe out of
    the manifest, follows it literally, and imports nothing from this system. It does not
    know the field order, the length prefix, the UTC rule or the details sorting except from
    the document in front of it.

    If this fails, the export has become a set of numbers nobody outside can check, and the
    hash chain is a promise rather than a proof. It also fails if somebody reorders
    `compute_entry_hash` without updating `HASHED_FIELDS`, which is the duplication the
    export module accepts and this test is the reason it can.
    """
    document = render_export(
        a_chain(6),
        exported_by="u_rupash",
        trace_id="trace_export",
        reason_code="regulator_request",
        at=NOW,
    )
    lines = document.splitlines()
    manifest = json.loads(lines[0])
    recipe = manifest["recipe"]

    assert recipe["hash_function"] == "sha256"
    previous = manifest["start_hash"]
    for line in lines[1:]:
        entry = json.loads(line)
        parts: list[str] = []
        for name in recipe["fields_in_order"]:
            value = recipe["hash_schema"] if name == "hash_schema" else entry[name]
            parts.append(str(value))
        for key in sorted(entry["details"]):
            parts.append(key)
            parts.append(entry["details"][key])
        joined = "".join(f"{len(part)}:{part}" for part in parts)

        assert entry["prev_hash"] == previous, "the links do not meet"
        assert hashlib.sha256(joined.encode("utf-8")).hexdigest() == entry["entry_hash"]
        previous = entry["entry_hash"]

    assert previous == manifest["head"]


def test_the_entry_block_digest_is_checkable_with_an_ordinary_shell_tool() -> None:
    """`tail -n +2 export.jsonl | sha256sum` has to produce the number in the manifest.

    That command is the difference between a digest a recipient can confirm in ten seconds
    and one they have to write a parser for. Wrapping it in a domain separator or a length
    prefix would be marginally tidier and would cost the only property that makes it worth
    carrying.
    """
    chain = a_chain(4)
    document = render_export(
        chain,
        exported_by="u_rupash",
        trace_id="trace_export",
        reason_code="regulator_request",
        at=NOW,
    )
    lines = document.splitlines(keepends=True)

    assert json.loads(lines[0])["entries_digest"] == entries_digest(chain.entries)
    assert (
        entries_digest(chain.entries)
        == hashlib.sha256("".join(lines[1:]).encode("utf-8")).hexdigest()
    )


def test_a_broken_chain_still_exports_and_names_where_it_broke() -> None:
    """Refusing to export a damaged ledger would mean the one circumstance in which an
    external copy matters most is the one in which it cannot be produced.

    A tampered ledger is evidence. The export carries the break instead of blocking on it,
    and says plainly that it did not verify, so nobody reads the document as a clean bill of
    health.
    """
    entries = list(a_chain(5).entries)
    entries[2] = entries[2].model_copy(update={"subject": "principal:u_somebody_else"})
    export = build_export(
        AuditChain(entries),
        exported_by="u_rupash",
        trace_id="trace_export",
        reason_code="pending_litigation",
        at=NOW,
    )

    assert not export.manifest.verified
    assert export.manifest.break_found is not None
    assert export.manifest.break_found.seq == entries[2].seq
    assert export.manifest.entry_count == 5


def test_a_filtered_selection_cannot_be_exported_as_a_chain() -> None:
    """ "Every entry about this employee" is not a chain. Ship it as one and the links no
    longer meet, so the document either fails verification or has to be sent with the
    verification switched off, and an unverifiable artefact that looks verified is the worst
    thing to hand a regulator.

    The refusal also keeps two findings apart. A caller who filtered gets told they asked
    for the wrong shape; a manifest saying `verified: false` means somebody tampered with
    the ledger. Those go to very different places.
    """
    chain = a_chain(5)
    filtered = AuditChain([e for e in chain.entries if e.seq != 2])

    with pytest.raises(ExportRefusedError, match="not a chain"):
        build_export(
            filtered,
            exported_by="u_rupash",
            trace_id="trace_export",
            reason_code="regulator_request",
            at=NOW,
        )


def test_no_canary_value_survives_into_an_export() -> None:
    """The core export canary, inverted like the ledger's: it fails if the data arrives.

    Two halves, because there are two ways in. Entries carry canaries as details and must
    come out redacted, which is the ledger's guarantee holding through serialisation. And
    the export's own cover page must refuse a canary in the one field a caller writes prose
    into if nobody stops them.
    """
    chain = AuditChain()
    chain.append(
        action=AuditAction.GRANT,
        actor_id="u_rupash",
        subject="principal:u_weiling",
        ent_hash=ENT,
        trace_id="trace1",
        at=NOW,
        details={
            "client.contract_value": CANARIES["client.contract_value"],
            "hr.salary": CANARIES["hr.salary"],
            "before": {"client.margin": CANARIES["client.margin"]},
        },
    )
    document = render_export(
        chain,
        exported_by="u_rupash",
        trace_id="trace_export",
        reason_code="regulator_request",
        at=NOW,
    )
    for token in canary_tokens():
        assert token not in document, f"{token} reached an export"

    with pytest.raises(ValidationError):
        render_export(
            chain,
            exported_by="u_rupash",
            trace_id="trace_export",
            reason_code=CANARIES["hr.performance_note"],
            at=NOW,
        )


def test_no_field_on_an_export_manifest_will_accept_prose() -> None:
    """The structural half of the canary above, and the one that survives a new field.

    An export is assembled for an outside reader, which makes its cover page the most
    natural place in the system for somebody to attach a helpful note saying which employee
    it concerns. Every string on the manifest is therefore either pattern-constrained or
    pinned to a module constant, and this asserts it field by field rather than trusting
    that whoever adds the next one will remember.

    Adding a field to `ExportManifest` fails this test until somebody decides, in writing,
    whether it can carry prose.
    """
    entry_hash = a_chain(2).entries[1].entry_hash
    valid: dict[str, object] = {
        "exported_at": NOW,
        "exported_by": "u_rupash",
        "trace_id": "trace_export",
        "reason_code": "regulator_request",
        "start_hash": "0" * 64,
        "entry_count": 0,
        "head": "0" * 64,
        "entries_digest": "0" * 64,
        "verified": True,
    }
    probes: dict[str, object] = {
        "format": PROSE,
        "exported_by": PROSE,
        "trace_id": PROSE,
        "reason_code": PROSE,
        "start_hash": PROSE,
        "head": PROSE,
        "entries_digest": PROSE,
        "limitations": (PROSE,),
        "recipe": {"part_encoding": PROSE},
        "break_found": {
            "index": 0,
            "seq": 0,
            "reason": "link_broken",
            "expected": PROSE,
            "actual": "0" * 64,
        },
    }
    # Any field not probed is one that cannot hold a string at all. Pinning the union means
    # a new field is a deliberate decision in two places rather than an omission in one.
    numeric_or_temporal = {"exported_at", "entry_count", "first_seq", "last_seq", "verified"}
    assert set(probes) | numeric_or_temporal | {"anchors"} == set(ExportManifest.model_fields)

    for name, value in probes.items():
        try:
            ExportManifest(**{**valid, "verified": name != "break_found", name: value})  # type: ignore[arg-type]
        except ValidationError:
            continue
        pytest.fail(f"{name} accepted prose onto a document assembled for an outside reader")

    # An anchor is a nested model and refuses on its own account, which is why it is not in
    # the loop above: the manifest never sees the bad value.
    with pytest.raises(ValidationError):
        Anchor(
            seq=1,
            entry_hash=entry_hash,
            recorded_at=NOW,
            recorded_by="u_auditor",
            where=PROSE,
        )


# ============================ the interception is not the disclosure (M24.2.2)
def test_an_intercepted_question_looks_identical_from_outside() -> None:
    """The disclosure this whole leaf exists to prevent, asserted directly.

    If an intercepted request is recorded, rendered, counted or timed any differently from
    an ordinary one, the difference *is* the leak, and it is a worse leak than the answer
    would have been because it is one concentrated bit: this person raised something. The
    only thing that may leave the interception is a projection built from the trace id and
    constants, so two requests on the same trace produce identical bytes whatever was asked.

    Delete this and the first helpful addition to the trace ("intercepted: true", a topic
    label on a metric, a distinct reason string) reintroduces the disclosure silently.
    """
    for sensitive in SENSITIVE_QUESTIONS:
        for ordinary in ORDINARY_QUESTIONS:
            hidden = intercept(sensitive, trace_id="trace_same")
            plain = intercept(ordinary, trace_id="trace_same")

            assert hidden.handled_privately
            assert not plain.handled_privately
            assert hidden.disclosure() == plain.disclosure()
            assert hidden.disclosure().model_dump_json() == plain.disclosure().model_dump_json()


def test_nothing_a_disclosure_carries_can_name_a_topic_or_the_question() -> None:
    """The other direction of the same rule. Equality between the two branches would still
    hold if both carried the question text, so this asserts on content as well as sameness:
    no topic value, and no word from the question, may appear in what may be recorded."""
    for question in SENSITIVE_QUESTIONS:
        dumped = intercept(question, trace_id="trace1").disclosure().model_dump_json().lower()
        for topic in SensitiveTopic:
            assert topic.value not in dumped
        # Words of four letters or more, so the check is about content rather than about
        # "a" and "my" turning up inside unrelated JSON keys.
        for word in (w.strip(".,?'") for w in question.lower().split() if len(w) >= 4):
            assert word not in dumped, f"{word!r} reached a disclosure"

    assert set(Disclosure.model_fields) == {"trace_id", "note"}


def test_the_referral_cannot_vary_because_it_takes_no_arguments() -> None:
    """The mechanism is the empty signature, not the body, exactly as with
    `brain.core.redaction.render_lock`.

    A referral that varied by topic would put the topic into a stored transcript, and a
    transcript is replayed into session memory and rendered in a console. A function with
    nothing to vary on cannot vary, so the property is checked by reading the signature.
    """
    assert inspect.signature(render_referral).parameters == {}

    referral = render_referral().lower()
    for topic in SensitiveTopic:
        assert topic.value.replace("_", " ") not in referral
    for word in ("grievance", "whistleblow", "harassment", "disciplinary", "complaint"):
        assert not re.search(rf"\b{word}", referral), f"the referral names {word}"


def test_every_sensitive_question_is_kept_off_the_ordinary_path() -> None:
    """The control itself. Without it these questions are classified, retrieved over, cached
    under an entitlement hash, written to the payload store for thirty days and rendered in
    a console the asker's department admin can open. Nothing in the permission model is
    broken by that, which is why nothing in the permission model catches it."""
    for question in SENSITIVE_QUESTIONS:
        decision = intercept(question, trace_id="trace1")
        assert decision.handled_privately, f"{question!r} reached the ordinary path"
        assert decision.reply() == render_referral()


def test_a_small_bucket_is_never_recoverable_by_subtraction() -> None:
    """Complementary suppression, and the mistake it prevents.

    Publishing the total beside a breakdown with one bucket hidden discloses the hidden
    bucket exactly: it is the total minus the rest. So the breakdown is all-or-nothing. A
    report that suppressed only the small bucket would look careful and would name the one
    person it was trying to protect.
    """
    tally = InterceptionTally(
        period="2026-09",
        counts={
            SensitiveTopic.HR_PERSONAL: 40,
            SensitiveTopic.GRIEVANCE: 12,
            SensitiveTopic.WHISTLEBLOWING: 1,
        },
    )
    report = tally.report()

    assert report.by_topic is None, "a breakdown was released with a bucket of one in it"
    assert report.total == 53
    assert report.suppressed


def test_a_period_below_the_cohort_threshold_discloses_no_number_at_all() -> None:
    """Below the threshold even the total is a small number about a small group, so the
    report says only that it is suppressed. It still says that much: a data protection
    officer has to be able to tell "the control ran and saw very little" from "the control
    is not running", and an absent report cannot make that distinction."""
    quiet = InterceptionTally(
        period="2026-09", counts={SensitiveTopic.WHISTLEBLOWING: MIN_DISCLOSABLE_COHORT - 1}
    )
    report = quiet.report()

    assert report.suppressed
    assert report.total is None
    assert report.by_topic is None


def test_the_ordinary_path_is_never_intercepted_by_accident() -> None:
    """The false-positive half. Interception that swallows ordinary work is interception
    that gets switched off, and then none of the tests above protect anybody."""
    for question in ORDINARY_QUESTIONS:
        assert classify_topic(question) is None


# ================================= the register holds names, never data (M24.2.3)
def test_the_permanent_deny_list_cannot_be_written_around() -> None:
    """Section 8: "Email, phone, NRIC, bank details and salary are on a permanent deny list
    with no exception." The register is the document on which an exception would be recorded,
    so it is the one place the refusal has to be enforced rather than remembered."""
    for denied in ("email", "phone", "nric", "bank_details", "salary"):
        with pytest.raises(ValidationError, match="permanent deny list"):
            ProcessingRecord(
                connector_id="conn_x",
                source_system="xero",
                transport=Transport.REST,
                purpose="invoice_ageing",
                data_subjects=frozenset({"client_contact"}),
                categories=frozenset({denied}),
                tier=DataTier.LOCAL,
                retention_days=30,
                basis=LawfulBasis.LEGITIMATE_INTERESTS,
                basis_reference="dpia_2026_04",
                basis_decided_by="u_rupash",
                basis_decided_at=NOW,
                reviewed_at=NOW,
            )


def test_a_register_entry_cannot_carry_an_example_of_the_data_it_describes() -> None:
    """A data processing record is a document about personal data that circulates more
    widely than the data does. Every free-text box on such a document eventually contains an
    example, so there are none: every field is an enumerated value, an identifier or a
    field-name token."""
    valid: dict[str, object] = {
        "connector_id": "conn_x",
        "source_system": "xero",
        "transport": Transport.REST,
        "purpose": "invoice_ageing",
        "data_subjects": frozenset({"client_contact"}),
        "categories": frozenset({"contact_name"}),
        "tier": DataTier.FEDERATED,
        "basis": LawfulBasis.LEGITIMATE_INTERESTS,
        "basis_reference": "dpia_2026_04",
        "basis_decided_by": "u_rupash",
        "basis_decided_at": NOW,
        "reviewed_at": NOW,
    }
    probes: dict[str, object] = {
        "connector_id": PROSE,
        "source_system": PROSE,
        "purpose": PROSE,
        "data_subjects": frozenset({PROSE}),
        "categories": frozenset({PROSE}),
        "egress_regions": frozenset({PROSE}),
        "projected_fields": (PROSE,),
        "basis_reference": PROSE,
        "basis_decided_by": PROSE,
        "transfer_safeguard": PROSE,
        "transfer_safeguard_reference": PROSE,
    }
    for field, value in probes.items():
        try:
            ProcessingRecord(**{**valid, field: value})  # type: ignore[arg-type]
        except ValidationError:
            continue
        pytest.fail(f"{field} accepted prose into the register")


def test_a_connector_with_no_record_may_not_process_personal_data() -> None:
    """Default-deny, the same rule the field policy applies to an unclassified field. A
    connector the register does not describe has not been assessed, and an unassessed
    connector processing personal data is exactly the finding the register exists to
    prevent."""
    assert not ProcessingRegister().permits("conn_anything", now=NOW)
    assert ProcessingRegister().gaps(["conn_a", "conn_b"], now=NOW) != ()


# ==================================== the clock does not flatter us (M24.2.4)
def test_the_clock_starts_at_awareness_and_never_at_confirmation() -> None:
    """Confirmation is always later than the reason to believe, so a clock keyed on it
    reports headroom that does not exist, by exactly the length of the investigation.

    `confirmed_at` is recorded because a post-incident review asks for it and is read by
    nothing. This asserts that: three cases confirmed weeks apart have the same deadline.
    """
    deadlines = {
        a_case(confirmed_at=NOW + timedelta(days=days)).assessment_due_before for days in (0, 7, 25)
    }
    assert len(deadlines) == 1
    assert a_case().assessment_due_before == deadlines.pop()

    # And the same for the notification deadline, which runs from the assessment day.
    assessment = Assessment(assessed_at=NOW, harm=a_harm(significant=True), affected_count=2)
    with_confirmation = a_case(assessment=assessment, confirmed_at=NOW + timedelta(days=20))
    assert (
        with_confirmation.commission_due_before
        == a_case(assessment=assessment).commission_due_before
    )


def test_an_estimated_awareness_starts_the_clock_at_the_earliest_it_could_have_been() -> None:
    """A workflow that takes the estimate moves the deadline later by exactly the width of
    its own uncertainty, which is the wrong direction: being early costs urgency, being late
    costs the people in the breach."""
    estimated = open_case(
        case_id="case_2",
        became_aware_at=NOW,
        earliest_possible_at=NOW - timedelta(days=6),
        basis=AwarenessBasis.ESTIMATED,
        source=AwarenessSource.STAFF_REPORT,
        recorded_at=NOW + timedelta(days=1),
        recorded_by="u_rupash",
        evidence_reference="ticket_12",
    )

    assert estimated.awareness.clock_starts_at == NOW - timedelta(days=6)
    assert estimated.assessment_due_before < a_case().assessment_due_before


def test_an_unknown_number_of_affected_individuals_is_never_read_as_zero() -> None:
    """The most expensive rounding error available here. Defaulting the count to zero makes
    every unfinished assessment come out as not notifiable on the scale limb, and the case
    then closes itself."""
    unknown = Assessment(assessed_at=NOW, harm=a_harm(significant=False), affected_count=None)

    assert unknown.scale_is_significant is None
    assert unknown.outcome is Notifiability.UNDETERMINED
    assert a_case(assessment=unknown).commission_due_before is None

    known_small = Assessment(assessed_at=NOW, harm=a_harm(significant=False), affected_count=0)
    assert known_small.outcome is Notifiability.NOT_NOTIFIABLE


def test_significant_scale_is_five_hundred_and_is_the_only_limb_that_is_arithmetic() -> None:
    """Five hundred is a number in the regulations and is computed. Significant harm is a
    legal judgement and is not: the boundary between the two is where a helpful function
    would start giving advice."""
    assert SIGNIFICANT_SCALE_THRESHOLD == 500

    below = Assessment(assessed_at=NOW, harm=a_harm(significant=False), affected_count=499)
    at_the_line = Assessment(assessed_at=NOW, harm=a_harm(significant=False), affected_count=500)

    assert below.outcome is Notifiability.NOT_NOTIFIABLE
    assert at_the_line.outcome is Notifiability.NOTIFIABLE


def test_notifiability_cannot_be_reached_without_a_recorded_human_decision() -> None:
    """Whether a breach is likely to cause significant harm turns on categories of data read
    against a schedule, in the circumstances. That is a legal assessment, and a function
    returning True or False for it would be advice that gets relied on precisely because it
    looks like arithmetic.

    So an assessment cannot exist without a `HarmDetermination`, and a determination cannot
    exist without a person's name, the moment they decided and a reference to where the
    reasoning is written down.
    """
    with pytest.raises(ValidationError):
        Assessment(assessed_at=NOW, affected_count=800)  # type: ignore[call-arg]

    for missing in ("decided_by", "decided_at", "rationale_reference"):
        payload: dict[str, object] = {
            "significant_harm": True,
            "decided_by": "u_rupash",
            "decided_at": NOW,
            "rationale_reference": "assessment_memo_1",
        }
        del payload[missing]
        with pytest.raises(ValidationError):
            HarmDetermination(**payload)  # type: ignore[arg-type]


def test_three_calendar_days_is_three_calendar_days_and_not_three_working_days() -> None:
    """An assessment made on a Friday is due by the following Monday, not the Wednesday
    after. Reading the three days as working days is a natural mistake for anybody who has
    written a business-day helper before, and it is a missed statutory deadline.

    Counted on Singapore's calendar, because the day boundary that matters is the
    regulator's and not the server's.
    """
    friday = datetime(2026, 9, 4, 10, 0, tzinfo=SINGAPORE)
    assert friday.weekday() == 4

    case = a_case(
        assessment=Assessment(assessed_at=friday, harm=a_harm(significant=True), affected_count=900)
    )

    assert case.commission_due_before == datetime(2026, 9, 8, tzinfo=SINGAPORE)
    # Notifying at any point on the Monday is in time; the Tuesday is not.
    monday_late = datetime(2026, 9, 7, 23, 59, tzinfo=SINGAPORE)
    tuesday = datetime(2026, 9, 8, 0, 1, tzinfo=SINGAPORE)
    in_time = case.model_copy(update={"commission_notified_at": monday_late})
    too_late = case.model_copy(update={"commission_notified_at": tuesday})

    assert not next(
        o for o in in_time.outstanding(tuesday) if o.kind is ObligationKind.NOTIFY_COMMISSION
    ).satisfied_late
    assert next(
        o for o in too_late.outstanding(tuesday) if o.kind is ObligationKind.NOTIFY_COMMISSION
    ).satisfied_late


def test_a_notifiable_breach_that_was_never_notified_is_always_reported() -> None:
    """The failure mode a compliance workflow is bought to prevent: a case that is assessed,
    filed and forgotten. Nothing about a satisfied assessment may quiet the notification
    obligation underneath it."""
    case = a_case(
        assessment=Assessment(assessed_at=NOW, harm=a_harm(significant=True), affected_count=900)
    )
    much_later = NOW + timedelta(days=30)

    obligation = next(
        o for o in case.outstanding(much_later) if o.kind is ObligationKind.NOTIFY_COMMISSION
    )
    assert obligation.overdue
    assert not obligation.satisfied
    assert any("notify_commission" in finding for finding in case.findings(much_later))
