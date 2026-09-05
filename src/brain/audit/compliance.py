"""The three compliance surfaces that are not the ledger: interception, the register, and
the breach clock.

**What breaks without it.** Each of the three fails silently and expensively, and each
fails in a different direction.

*Without interception*, an employee's question about a grievance is an ordinary question.
It is classified, retrieved over, answered from company knowledge, cached under their
entitlement hash, written to the payload store for thirty days and rendered in a console
that their department admin can open. Nothing in the permission model is broken by that:
they were entitled to ask, and every field they saw was theirs to see. The disclosure is
not the answer, it is *the asking*, and no field-level control can see it because the
sensitive fact never enters a field.

*Without the register*, the data protection officer's answer to "what personal data does
this system touch, and why are you allowed to" is assembled from memory by whoever is
available on the day. It will be incomplete, it will disagree with the previous version,
and the gap will be found by the regulator rather than by us.

*Without the breach clock*, the deadline is computed from whenever somebody got round to
opening a ticket. Singapore's PDPA starts the assessment clock when the organisation has
reason to believe a breach occurred, not when it is confirmed and not when it is filed, so
a workflow keyed on the filing date reports comfort it has not got.

Four rules run through everything here.

**The interception must not be the disclosure.** If an intercepted question is handled in
any way an outsider can observe, the difference *is* the leak, and it is a worse leak than
the answer would have been because it is concentrated: one bit that says "this person
raised something". So the topic never leaves this module, the referral is one sentence with
no arguments (the same trick, and for the same reason, as `brain.core.redaction.render_lock`),
no audit entry is written that an ordinary question would not write, and the only aggregate
that leaves is suppressed below a cohort size.

**The register holds names, never data.** A data processing record is a document about
personal data that must not contain any, so every field is an enumerated value, an
identifier or a field-name token, checked the way `brain.audit.ledger` checks its details.

**The clock is honest about what it does not know.** Awareness is a recorded fact with its
own evidence and its own basis, and where the basis is an estimate the deadline is computed
from the earliest moment we could have known rather than from the estimate. A deadline that
is too early costs us urgency; one that is too late costs the people in the breach.

**No judgement is computed that a person must make.** Whether a breach is likely to cause
significant harm is a legal assessment. This module records who made it, when, and where
the reasoning is written down; it does not make it, and it refuses to close an assessment
that has not been made. What is arithmetic is treated as arithmetic: five hundred is a
number, and the scale limb is computed.

Statutory positions relied on, with the parts that are *not* statutory marked as such:

- assessment must be conducted in a reasonable and expeditious manner. The thirty days in
  `ASSESSMENT_GUIDELINE_DAYS` is the PDPC's published expectation of what that means, not a
  deadline in the Act, and `ObligationBasis` says so on every obligation it produces;
- notification to the Commission is no later than three calendar days after the day the
  organisation assesses the breach to be notifiable. Calendar days, and the day of the
  assessment is day zero;
- affected individuals are notified at the same time as, or after, the Commission;
- a breach is notifiable if it results in or is likely to result in significant harm to an
  affected individual, or is or is likely to be of significant scale. Significant scale is
  five hundred or more affected individuals.

Architecture positions relied on, quoted:

- "Email, phone, NRIC, bank details and salary are on a permanent deny list with no
  exception." (section 8) - enforced by `ProcessingRecord`;
- "Max 12 fields per entity type." (section 8, the projected tier) - enforced as
  `MAX_PROJECTED_FIELDS`;
- "Region. Singapore, for PDPA comfort" (section 21) - why `SINGAPORE` is the timezone a
  calendar day is counted in, and why any other egress region is a transfer;
- "sensitive-topic interception for HR and grievance" appears in section 27 under "not yet
  designed", so the interception below is the first design of it rather than an
  implementation of an agreed one.

Task ids: M24.2.2, M24.2.3, M24.2.4
"""

from __future__ import annotations

import enum
import re
from collections.abc import Iterable, Mapping
from datetime import datetime, time, timedelta, timezone
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

from brain.audit.ledger import FIELD_NAME, IDENTIFIER, TRACE_ID

# =====================================================================================
# M24.2.2  Sensitive-topic interception
# =====================================================================================


class SensitiveTopic(enum.StrEnum):
    """What the interception recognises. Closed, and it never leaves this module.

    Three members rather than one, and the distinction is used for exactly one thing: the
    suppressed aggregate a data protection officer reads to confirm the control is running.
    Nothing else may branch on it. In particular the referral does not, which is the point
    of `render_referral` taking no arguments.
    """

    #: The asker's own employment: pay, appraisal, discipline, leave, health.
    HR_PERSONAL = "hr_personal"
    #: A complaint about treatment at work, whoever it is about.
    GRIEVANCE = "grievance"
    #: A report of misconduct, made or contemplated.
    WHISTLEBLOWING = "whistleblowing"


#: Where a sensitive question is sent instead. One route for every topic, deliberately.
#:
#: Rejected: a per-topic route, which reads as more helpful and is a disclosure. The
#: referral is delivered into a conversation, and conversations are stored, replayed into
#: session memory and rendered in the console. A transcript containing the whistleblowing
#: hotline tells whoever opens it what was raised, which is exactly the fact the
#: interception exists to protect. One route, triaged by a person, discloses nothing.
#:
#: A deployment edits this constant. It is not a parameter for the same reason.
CONFIDENTIAL_ROUTE: Final = "the confidential reporting route in the staff handbook"

REFERRAL_TEXT: Final = (
    "This is not something to put through me, and I have not looked anything up. "
    f"Please go to {CONFIDENTIAL_ROUTE}, which sits outside this system and outside "
    "your reporting line."
)

#: What the trace is allowed to say about how a request was handled. One constant, so it
#: cannot vary between the intercepted and the ordinary path.
#:
#: The wording is deliberately dull and deliberately true of every request. "Handled" would
#: be enough; the rest exists so that a reader who finds this note does not go looking for
#: the interesting one.
HANDLING_NOTE: Final = "request handled"


def render_referral() -> str:
    """What an intercepted asker is told. Identical for every topic, by construction.

    This function takes no arguments, and the empty signature is the mechanism rather than
    an accident of the body, exactly as in `brain.core.redaction.render_lock`. A referral
    that varied by topic would put the topic into a stored transcript; a referral that
    varied by person would put the person into it. A signature with nothing in it cannot
    vary by anything, and the invariant suite checks the signature rather than trusting the
    implementation.
    """
    return REFERRAL_TEXT


def _normalise(text: str) -> str:
    """Lowercase and collapse whitespace. Nothing cleverer.

    Deliberately not the aggressive normaliser in `brain.gate.injection`, which strips
    zero-width characters and homoglyphs to defeat an adversary trying to smuggle an
    instruction past a filter. The adversary model here is the opposite: the person writing
    the question is the person being protected, and they are not trying to evade the
    interception. Over-normalising would only widen the false-positive surface.
    """
    return re.sub(r"\s+", " ", text.lower()).strip()


#: The patterns, in the order they are tried. First match wins, and because every branch
#: produces the same referral the order affects only which bucket the suppressed aggregate
#: counts. Ordered most-protective first so that a question which reads as both a report of
#: misconduct and a grievance is counted as the former.
#:
#: These are patterns and not a model, for the reason `brain.gate.classify` gives about the
#: lane and one more that is specific to this leaf: asking a model whether a question is
#: about whistleblowing means sending the question about whistleblowing to a model
#: provider, so the classifier would perform the disclosure it exists to prevent.
TOPIC_PATTERNS: Final[tuple[tuple[SensitiveTopic, re.Pattern[str]], ...]] = (
    (SensitiveTopic.WHISTLEBLOWING, re.compile(r"\bwhistle[- ]?blow\w*\b")),
    (
        SensitiveTopic.WHISTLEBLOWING,
        re.compile(
            r"\breport(?:ing|ed)?\s+(?:\w+\s+){0,3}?"
            r"(?:misconduct|wrongdoing|fraud|corruption|bribery|kickback|malpractice)\b"
        ),
    ),
    (
        SensitiveTopic.WHISTLEBLOWING,
        re.compile(r"\b(?:anonymous(?:ly)?|confidential(?:ly)?)\s+report\w*\b"),
    ),
    (SensitiveTopic.WHISTLEBLOWING, re.compile(r"\bspeak[- ]?up\s+(?:policy|line|channel)\b")),
    (SensitiveTopic.GRIEVANCE, re.compile(r"\bgrievance\b")),
    (SensitiveTopic.GRIEVANCE, re.compile(r"\bharass(?:ment|ed|ing)?\b")),
    (SensitiveTopic.GRIEVANCE, re.compile(r"\bbull(?:y|ied|ying)\b")),
    (SensitiveTopic.GRIEVANCE, re.compile(r"\bdiscriminat\w*\s+against\b")),
    (SensitiveTopic.GRIEVANCE, re.compile(r"\bhostile work environment\b")),
    (
        SensitiveTopic.GRIEVANCE,
        re.compile(r"\b(?:unfair|wrongful|constructive)\s+(?:dismissal|termination|treatment)\b"),
    ),
    (
        SensitiveTopic.GRIEVANCE,
        re.compile(r"\b(?:file|raise|lodge|submit)\s+(?:a\s+)?(?:formal\s+)?complaint\b"),
    ),
    (
        SensitiveTopic.HR_PERSONAL,
        re.compile(
            r"\bmy\s+(?:salary|pay|payslip|bonus|increment|appraisal|performance review|"
            r"probation|notice period|leave balance|annual leave|medical leave)\b"
        ),
    ),
    (
        SensitiveTopic.HR_PERSONAL,
        re.compile(r"\bdisciplinary\s+(?:action|hearing|procedure|process|meeting)\b"),
    ),
    (SensitiveTopic.HR_PERSONAL, re.compile(r"\b(?:resign|resignation|hand in my notice)\b")),
    (SensitiveTopic.HR_PERSONAL, re.compile(r"\b(?:maternity|paternity|parental)\s+leave\b")),
)


def classify_topic(question: str) -> SensitiveTopic | None:
    """The sensitive topic this question is about, or None.

    Errs towards interception, and the asymmetry is deliberate. A false negative puts a
    grievance through retrieval, the cache, the payload store and a console a department
    admin can open, and nothing downstream can undo that. A false positive costs somebody a
    referral instead of an answer to a policy question, which is annoying and reversible.

    Note what is *not* attempted: distinguishing "where is the grievance policy" from "I
    want to raise a grievance". The two cannot be told apart from text with any reliability,
    and they do not need to be, because the sensitive fact is the same in both cases. It is
    not the answer that discloses anything. It is that this person asked.
    """
    text = _normalise(question)
    for topic, pattern in TOPIC_PATTERNS:
        if pattern.search(text):
            return topic
    return None


class Disclosure(BaseModel):
    """Everything anyone other than the asker may learn about how a request was handled.

    Two objects rather than one, in the shape `brain.core.redaction.RedactedAnswer` uses to
    keep a payload apart from a trace: `Interception` carries the topic and never leaves
    this module, and this is what may be recorded, rendered, exported or counted.

    What is absent is the design. There is no topic, no flag saying whether the ordinary
    path ran, no timing and no reason, and `extra="forbid"` means a later caller cannot
    attach one because it seemed useful. Every field here is either a constant or copied
    from the request, so two disclosures from the same trace id are equal whatever happened
    inside, which is the property the invariant suite asserts.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    trace_id: str = Field(pattern=TRACE_ID)
    note: str = HANDLING_NOTE


class Interception(BaseModel):
    """The decision, including the part that must not be recorded.

    `topic` is here because the suppressed aggregate needs it and for no other reason. It
    must not reach a trace, an audit entry, a metric label, a log line, a cache key or an
    exception message. `disclosure()` is the only sanctioned way out, and it is a function
    of the trace id alone.

    Rejected: making this a plain boolean and returning the topic separately. It reads
    tidier and it removes the one thing that makes the rule enforceable, which is having a
    single object whose safe projection can be asserted equal across both branches.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    trace_id: str = Field(pattern=TRACE_ID)
    #: True when the ordinary path must not run. The gate reads this and nothing else.
    handled_privately: bool
    topic: SensitiveTopic | None = None

    def model_post_init(self, _context: object, /) -> None:
        if self.handled_privately != (self.topic is not None):
            msg = "handled_privately and topic disagree; one of them is a lie about the other"
            raise ValueError(msg)

    def disclosure(self) -> Disclosure:
        """What may be recorded. Built from the trace id and constants, so it cannot vary.

        Written as a constructor call with no branch rather than as a conditional that
        happens to produce the same value on both sides. A branch would pass the invariant
        test today and be one edit away from failing it, and the edit would look like an
        improvement.
        """
        return Disclosure(trace_id=self.trace_id)

    def reply(self) -> str | None:
        """The referral, when this was intercepted. None when the ordinary path should run."""
        return render_referral() if self.handled_privately else None


def intercept(question: str, *, trace_id: str) -> Interception:
    """Decide, for every request, whether the ordinary path may run.

    Called unconditionally, for every question from every channel, and the "every" is part
    of the control. A classifier that runs only for some requests is a classifier whose
    having run is itself a signal, and the cost of it having run is then measurable from
    outside as latency.

    Where this belongs in the gate: at CLASSIFY, and in any case **before CACHE**. Running
    it after the cache means an intercepted question can be served from, and written to, a
    keyed store, and a cache entry is an artefact that outlives the conversation. Running it
    after SELECT means an agent has already been chosen for it, and choosing an agent is a
    read of the catalogue that leaves its own trail. Wiring that call site is not part of
    this leaf: `brain/gate/context.py` is not in this change's allowlist, so today this
    function is correct and uncalled.

    **No audit entry is written, and that is deliberate.** `AuditAction` is closed and holds
    no member for this, which is the right answer rather than a missing one: an entry that an
    intercepted request writes and an ordinary one does not *is* the disclosure, sitting in
    the longest-retained and most widely read table in the system. The evidence that the
    control runs is the suppressed aggregate below, and nothing else.

    **The timing channel is not closed here, and this module cannot close it.** A referral
    returned in ten milliseconds where an answer takes two seconds is observable to anyone
    watching the channel, and a person who can see when a colleague's request was unusually
    fast can infer what it was about. Closing it needs the response path to hold an
    intercepted reply for the same visible time an ordinary one would have taken, which is a
    property of the channel adapter rather than of a pure function. It is recorded here as an
    obligation on whoever wires this in, not as something already done.
    """
    topic = classify_topic(question)
    return Interception(trace_id=trace_id, handled_privately=topic is not None, topic=topic)


#: Below this many interceptions in a period, nothing is disclosed but the fact of
#: suppression.
#:
#: Five, matching the smallest cohort convention used in staff reporting generally, and the
#: number is a policy choice rather than a derived one. What matters more than the value is
#: that the suppression is complementary: see `InterceptionTally.report`.
MIN_DISCLOSABLE_COHORT: Final = 5

#: A calendar month. Coarse deliberately: a daily tally in a company of this size is a
#: timeline, and a timeline plus an attendance record is an identification.
PERIOD_PATTERN: Final = r"^\d{4}-(?:0[1-9]|1[0-2])$"


class TallyReport(BaseModel):
    """What a data protection officer is shown. Possibly nothing, and it says so.

    `suppressed` is present rather than the report simply being absent, because a data
    protection officer needs to tell "the control ran and saw very little" from "the control
    is not running", and an absent report cannot make that distinction.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    period: str = Field(pattern=PERIOD_PATTERN)
    #: None when below the cohort threshold.
    total: int | None = None
    #: None unless every non-empty bucket is independently above the threshold.
    by_topic: dict[SensitiveTopic, int] | None = None
    suppressed: bool


class InterceptionTally(BaseModel):
    """A month's interceptions, counted and nothing else.

    No principal, no trace id, no timestamp finer than the month, and no question. The
    counts are the whole of the evidence that the control is running, and they are as much
    as can be published without the report becoming the disclosure.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    period: str = Field(pattern=PERIOD_PATTERN)
    counts: dict[SensitiveTopic, int] = Field(default_factory=dict)

    @field_validator("counts")
    @classmethod
    def _non_negative(cls, v: dict[SensitiveTopic, int]) -> dict[SensitiveTopic, int]:
        if any(count < 0 for count in v.values()):
            msg = "a tally cannot count backwards"
            raise ValueError(msg)
        return v

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def report(self) -> TallyReport:
        """The publishable form, with complementary suppression.

        Two thresholds, and the second is the one that is easy to get wrong. Publishing the
        total alongside a breakdown with one bucket hidden discloses the hidden bucket
        exactly: it is the total minus the rest. So the breakdown is all-or-nothing, and it
        is released only when every non-empty bucket clears the threshold on its own.

        The total is released on a lower bar than the breakdown because it is a weaker
        statement: "eleven sensitive questions this month" identifies nobody, where "one
        whistleblowing report this month" in a company of a hundred and twenty is a search
        with a very small result set.
        """
        if self.total < MIN_DISCLOSABLE_COHORT:
            return TallyReport(period=self.period, suppressed=True)
        buckets = {topic: count for topic, count in self.counts.items() if count > 0}
        if buckets and all(count >= MIN_DISCLOSABLE_COHORT for count in buckets.values()):
            return TallyReport(
                period=self.period, total=self.total, by_topic=dict(buckets), suppressed=False
            )
        return TallyReport(period=self.period, total=self.total, suppressed=True)


# =====================================================================================
# M24.2.3  Data processing record per connector
# =====================================================================================


class Transport(enum.StrEnum):
    """The four transports a connector can use. Section 12 of the architecture."""

    MCP = "mcp"
    REST = "rest"
    DATABASE = "database"
    CUSTOM = "custom"


class DataTier(enum.StrEnum):
    """Where the data ends up. Section 8 of the architecture, and the register's spine.

    The tier answers "how long is it kept" almost by itself, which is why the register keys
    retention off it rather than asking each connector to restate a policy.
    """

    #: We are the source. Identity, capabilities, audit, knowledge, memory, the registry.
    LOCAL = "local"
    #: A pointer, not the payload: ids, join keys, status enums, timestamps, short labels.
    PROJECTED = "projected"
    #: Fetched live and never stored. Ticket bodies, invoice lines, contracts, CRM notes.
    FEDERATED = "federated"


class LawfulBasis(enum.StrEnum):
    """A picklist, not advice.

    These name the bases a person selects from when they record their assessment. This
    module does not decide which applies, does not check the choice against the categories
    of data, and must never be read as having done either: `basis_reference` points at the
    written assessment, and `basis_decided_by` names who made it.

    The reason for an enum rather than free text is the same reason `LegalHold.reason_code`
    is a token. A free-text lawful basis field on a per-connector register is where somebody
    eventually writes the name of the client whose contract the basis rests on.
    """

    CONSENT = "consent"
    DEEMED_CONSENT_BY_CONDUCT = "deemed_consent_by_conduct"
    DEEMED_CONSENT_BY_CONTRACTUAL_NECESSITY = "deemed_consent_by_contractual_necessity"
    DEEMED_CONSENT_BY_NOTIFICATION = "deemed_consent_by_notification"
    LEGITIMATE_INTERESTS = "legitimate_interests"
    BUSINESS_IMPROVEMENT = "business_improvement"
    LEGAL_OR_REGULATORY_REQUIREMENT = "legal_or_regulatory_requirement"
    EMPLOYMENT_MANAGEMENT = "employment_management"


#: Categories that may never be stored in any tier we control, from section 8: "Email,
#: phone, NRIC, bank details and salary are on a permanent deny list with no exception."
#:
#: Enforced here rather than trusted, because a register entry is written by whoever
#: installed the connector and the deny list is the kind of rule that is remembered
#: right up until the week somebody needs an email address for a mail merge.
DENIED_CATEGORIES: Final[frozenset[str]] = frozenset(
    {"email", "phone", "nric", "bank_details", "salary"}
)

#: Section 8: "Max 12 fields per entity type."
MAX_PROJECTED_FIELDS: Final = 12

#: Section 21: "Region. Singapore, for PDPA comfort and for latency to source APIs."
#: Anything else is a transfer out of Singapore and needs its own recorded safeguard.
HOME_REGION: Final = "sg"

#: A register entry older than this is a description of a system that has since changed.
REGISTER_REVIEW_DAYS: Final = 365


class ProcessingRecord(BaseModel):
    """What one connector does with personal data, in the form a regulator asks for.

    Every field is an enumerated value, an identifier or a field-name token. There is no
    free-text field anywhere in this model, and that is not tidiness: a data processing
    record is a document *about* personal data which is circulated more widely than the data
    itself, and every free-text box on such a document eventually contains an example.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    connector_id: str = Field(pattern=IDENTIFIER)
    #: The system behind the connector: `freshdesk`, `xero`, `lark_base`.
    source_system: str = Field(pattern=FIELD_NAME, max_length=80)
    transport: Transport
    #: Why, as a token: `ticket_status_lookup`, `invoice_ageing`.
    purpose: str = Field(pattern=FIELD_NAME, max_length=80)

    #: Whose data: `employee`, `client_contact`, `supplier_contact`, `candidate`.
    data_subjects: frozenset[str] = frozenset()
    #: What kinds: `contact_name`, `ticket_body`, `invoice_line`. Names, never examples.
    categories: frozenset[str] = frozenset()

    tier: DataTier
    #: Only for the projected tier, and capped at twelve.
    projected_fields: tuple[str, ...] = ()
    #: None means not stored, which is the only correct answer for the federated tier.
    retention_days: int | None = None

    write_capable: bool = False
    #: Where the data physically goes. `sg` alone means it never leaves.
    egress_regions: frozenset[str] = frozenset({HOME_REGION})
    #: Required when anything other than `sg` appears above: a token naming the mechanism
    #: (`contractual_clauses`, `intra_group_policy`), with the reasoning at the reference.
    transfer_safeguard: str | None = None
    transfer_safeguard_reference: str | None = None

    basis: LawfulBasis
    #: Where the person's written assessment lives. Never the assessment itself.
    basis_reference: str = Field(pattern=IDENTIFIER)
    basis_decided_by: str = Field(pattern=IDENTIFIER)
    basis_decided_at: datetime
    reviewed_at: datetime

    @field_validator("data_subjects", "categories", "egress_regions")
    @classmethod
    def _tokens_only(cls, v: frozenset[str]) -> frozenset[str]:
        bad = sorted(item for item in v if not re.match(FIELD_NAME, item))
        if bad:
            msg = f"a register entry would carry a value rather than a name: {bad}"
            raise ValueError(msg)
        return v

    @field_validator("projected_fields")
    @classmethod
    def _projected_names(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        bad = sorted(item for item in v if not re.match(FIELD_NAME, item))
        if bad:
            msg = f"projected field names are names: {bad}"
            raise ValueError(msg)
        return v

    @field_validator("basis_decided_at", "reviewed_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            msg = "register timestamps must be timezone-aware; a naive one is a silent bug"
            raise ValueError(msg)
        return v

    def model_post_init(self, _context: object, /) -> None:
        problems: list[str] = []
        if not self.data_subjects:
            problems.append("names no category of data subject")
        if not self.categories:
            problems.append("names no category of personal data")

        denied = sorted(self.categories & DENIED_CATEGORIES)
        if denied and self.tier is not DataTier.FEDERATED:
            problems.append(
                f"stores {denied} in the {self.tier.value} tier, and those are on the "
                "permanent deny list with no exception"
            )
        if self.tier is DataTier.FEDERATED and self.retention_days is not None:
            problems.append("is federated, so it is fetched live and never stored")
        if self.tier is not DataTier.FEDERATED and self.retention_days is None:
            problems.append("stores data and does not say for how long")
        if self.tier is DataTier.PROJECTED and len(self.projected_fields) > MAX_PROJECTED_FIELDS:
            problems.append(
                f"projects {len(self.projected_fields)} fields, above the "
                f"{MAX_PROJECTED_FIELDS} the projection allows per entity type"
            )
        if self.tier is not DataTier.PROJECTED and self.projected_fields:
            problems.append("lists projected fields but is not in the projected tier")

        offshore = sorted(self.egress_regions - {HOME_REGION})
        if offshore and not (self.transfer_safeguard and self.transfer_safeguard_reference):
            problems.append(f"sends data to {offshore} with no recorded transfer safeguard")
        for name in (self.transfer_safeguard, self.transfer_safeguard_reference):
            if name is not None and not re.match(IDENTIFIER, name):
                problems.append(f"transfer safeguard {name!r} is not a reference")

        if problems:
            msg = f"the record for {self.connector_id} " + "; ".join(problems)
            raise ValueError(msg)

    def review_due_before(self) -> datetime:
        return self.reviewed_at + timedelta(days=REGISTER_REVIEW_DAYS)

    def is_stale(self, now: datetime) -> bool:
        """A record nobody has looked at for a year describes a system that has changed."""
        return now >= self.review_due_before()


class RegisterGap(BaseModel):
    """A connector the register does not describe, or describes out of date.

    A gap is a finding rather than an exception, because the register is asked for at
    exactly the moment when raising is least useful: a data protection officer needs the
    complete list of what is missing, not the first item.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    connector_id: str = Field(pattern=IDENTIFIER)
    #: `unregistered` or `stale`. A token, so the gap list is machine-readable.
    kind: str = Field(pattern=FIELD_NAME)


class ProcessingRegister(BaseModel):
    """Every connector's record, and the ability to say which connectors have none.

    The second half is the half that matters. A register is judged by its gaps, and a
    register that can only list what it contains cannot find them. `gaps` takes the live
    connector list and reports the difference, which is why it is a method taking an
    argument rather than a property: the truth about which connectors exist lives in the
    connector layer, not here, and a register that believed its own contents were complete
    would be a register that could never be wrong.

    Default-deny follows, in the same shape as `brain.core.field_policy`: a connector with
    no record is not a connector with an unwritten record, it is a connector that has not
    been assessed, and `permits` says so.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    records: tuple[ProcessingRecord, ...] = ()

    @field_validator("records")
    @classmethod
    def _one_record_each(cls, v: tuple[ProcessingRecord, ...]) -> tuple[ProcessingRecord, ...]:
        seen: set[str] = set()
        duplicates: set[str] = set()
        for record in v:
            if record.connector_id in seen:
                duplicates.add(record.connector_id)
            seen.add(record.connector_id)
        if duplicates:
            msg = f"two records for {sorted(duplicates)}; a register with two answers has none"
            raise ValueError(msg)
        return v

    def for_connector(self, connector_id: str) -> ProcessingRecord | None:
        return next((r for r in self.records if r.connector_id == connector_id), None)

    def permits(self, connector_id: str, *, now: datetime) -> bool:
        """Whether this connector may process personal data at all.

        Default-deny. An unregistered connector is refused, and so is one whose record has
        gone stale, because a record that has not been reviewed in a year is a description
        of a system that has since been reconfigured by somebody who did not read it.
        """
        record = self.for_connector(connector_id)
        return record is not None and not record.is_stale(now)

    def gaps(self, connector_ids: Iterable[str], *, now: datetime) -> tuple[RegisterGap, ...]:
        """Every connector without a current record, sorted so two runs agree."""
        found: list[RegisterGap] = []
        for connector_id in sorted(set(connector_ids)):
            record = self.for_connector(connector_id)
            if record is None:
                found.append(RegisterGap(connector_id=connector_id, kind="unregistered"))
            elif record.is_stale(now):
                found.append(RegisterGap(connector_id=connector_id, kind="stale"))
        return tuple(found)


# =====================================================================================
# M24.2.4  PDPA breach assessment and the clock
# =====================================================================================

#: Singapore Standard Time, as a fixed offset rather than a named zone.
#:
#: A calendar day in the PDPA is a calendar day where the regulator is, so the day boundary
#: has to be Singapore's and not UTC's: a breach assessed at 09:00 on the 1st in Singapore
#: is assessed on 31 August in UTC, and the deadline computed in UTC is a day early. Early
#: is the safe direction and it is still wrong, and the same arithmetic run the other way
#: around for a notification timestamp is a day late.
#:
#: A fixed offset rather than `ZoneInfo("Asia/Singapore")` because Singapore has observed
#: no daylight saving since 1935 and has been on +08:00 continuously since 1982, so the
#: offset is not an approximation; and because `zoneinfo` needs a system tz database that
#: Windows does not ship, which would make the deadline depend on the developer's laptop.
SINGAPORE: Final = timezone(timedelta(hours=8))

#: Notification to the Commission: no later than three calendar days after the day the
#: organisation assesses the breach to be notifiable. Statutory.
COMMISSION_NOTIFICATION_DAYS: Final = 3

#: The assessment itself must be prompt. **Not statutory**: the Act requires a reasonable
#: and expeditious assessment, and thirty days is the regulator's published expectation of
#: what that means. Every obligation this module produces from it is labelled
#: `ObligationBasis.GUIDELINE` so that nobody reads a missed benchmark as a missed deadline,
#: or a met one as compliance.
ASSESSMENT_GUIDELINE_DAYS: Final = 30

#: Significant scale. Five hundred or more affected individuals, and the only limb of
#: notifiability that is arithmetic rather than judgement.
SIGNIFICANT_SCALE_THRESHOLD: Final = 500


class AwarenessBasis(enum.StrEnum):
    """How well we know when we became aware. The distinction the clock turns on."""

    #: A timestamped event we hold: an alert, a log line, a received notice.
    OBSERVED = "observed"
    #: A person's best recollection, with a recorded earliest moment it could have been.
    ESTIMATED = "estimated"


class AwarenessSource(enum.StrEnum):
    """Where the reason to believe came from. Closed, because it drives no logic and is
    asked about constantly: "how did you find out" is the first question in every
    post-incident review, and a free-text answer cannot be counted."""

    INTERNAL_DETECTION = "internal_detection"
    STAFF_REPORT = "staff_report"
    DATA_INTERMEDIARY_NOTICE = "data_intermediary_notice"
    THIRD_PARTY_REPORT = "third_party_report"
    REGULATOR_NOTICE = "regulator_notice"


class Awareness(BaseModel):
    """When the organisation first had reason to believe a breach had occurred.

    Modelled as its own recorded fact, with its own evidence and its own basis, because the
    alternative is the failure this leaf exists to prevent. A workflow that starts the clock
    when the case is opened is measuring its own responsiveness, not its obligation: an
    alert that fired on Monday and was triaged on Thursday has already spent three of the
    thirty days, and a case record dated Thursday reports twenty-seven days of headroom that
    do not exist.

    There is deliberately no constructor that defaults `became_aware_at` to now, and none
    that derives it from `recorded_at`. A missing awareness time must be an error a person
    resolves, not a field a helper fills in.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    became_aware_at: datetime
    basis: AwarenessBasis
    source: AwarenessSource
    #: Required when the basis is an estimate: the earliest moment it could have been. The
    #: clock runs from here, not from the estimate.
    earliest_possible_at: datetime | None = None
    recorded_at: datetime
    recorded_by: str = Field(pattern=IDENTIFIER)
    #: Where the evidence is: the alert id, the ticket, the received notice.
    evidence_reference: str = Field(pattern=IDENTIFIER)

    @field_validator("became_aware_at", "earliest_possible_at", "recorded_at")
    @classmethod
    def _tz_aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            msg = "awareness timestamps must be timezone-aware; a naive one is a silent bug"
            raise ValueError(msg)
        return v

    def model_post_init(self, _context: object, /) -> None:
        if self.recorded_at < self.became_aware_at:
            msg = "recorded before it was known; one of the two timestamps is wrong"
            raise ValueError(msg)
        if self.basis is AwarenessBasis.ESTIMATED:
            if self.earliest_possible_at is None:
                # The whole point of calling it an estimate. An estimate with no lower bound
                # is a guess the clock would then treat as a fact.
                msg = "an estimated awareness time must record the earliest it could have been"
                raise ValueError(msg)
            if self.earliest_possible_at > self.became_aware_at:
                msg = "the earliest possible moment is later than the estimate itself"
                raise ValueError(msg)
        elif self.earliest_possible_at is not None:
            # Two candidate start times, one of which is unused, is how a later refactor
            # picks the wrong one.
            msg = "an observed awareness time has no earliest-possible bound; it is observed"
            raise ValueError(msg)

    @property
    def clock_starts_at(self) -> datetime:
        """The moment every deadline is measured from.

        The earliest moment we could have known, whenever that is recorded, rather than the
        estimate. Choosing the estimate would move the deadline later by exactly the amount
        of the uncertainty, which is the wrong direction: the cost of being early is
        urgency, and the cost of being late falls on the people in the breach.
        """
        return self.earliest_possible_at or self.became_aware_at


class HarmDetermination(BaseModel):
    """A person's judgement on the significant-harm limb, and never a computed one.

    Whether a breach is likely to result in significant harm turns on the categories of data
    involved, read against a schedule, in the circumstances. That is a legal assessment. A
    function returning True or False here would be advice, it would be wrong in the cases
    that matter, and it would be relied on precisely because it looked like arithmetic.

    So this model records the decision and the trail to it, and `rationale_reference` points
    at where the reasoning is written rather than holding it: a rationale field on a breach
    record is a free-text box that will be filled with the details of the breach.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    significant_harm: bool
    decided_by: str = Field(pattern=IDENTIFIER)
    decided_at: datetime
    rationale_reference: str = Field(pattern=IDENTIFIER)

    @field_validator("decided_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            msg = "a determination timestamp must be timezone-aware"
            raise ValueError(msg)
        return v


class Notifiability(enum.StrEnum):
    """The outcome of the assessment, including the honest third answer."""

    NOTIFIABLE = "notifiable"
    NOT_NOTIFIABLE = "not_notifiable"
    #: Neither limb is settled: the harm judgement says no and the number of affected
    #: individuals is unknown, so the scale limb cannot be evaluated either way. This is not
    #: a completed assessment, and the three-day clock has not started.
    UNDETERMINED = "undetermined"


class Assessment(BaseModel):
    """The assessment, and the day it was made, which is what the three days run from.

    `affected_count` is optional and its absence means unknown, never zero. Defaulting it to
    zero would make every unfinished assessment come out as not notifiable on the scale
    limb, which is the most expensive rounding error available here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    assessed_at: datetime
    harm: HarmDetermination
    #: None means not yet established. Not zero.
    affected_count: int | None = Field(default=None, ge=0)

    @field_validator("assessed_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            msg = "assessed_at must be timezone-aware; a naive timestamp is a silent bug"
            raise ValueError(msg)
        return v

    @property
    def scale_is_significant(self) -> bool | None:
        """The arithmetic limb. None when the count is unknown, which is not False."""
        if self.affected_count is None:
            return None
        return self.affected_count >= SIGNIFICANT_SCALE_THRESHOLD

    @property
    def outcome(self) -> Notifiability:
        """Notifiable on either limb; undetermined when neither limb has settled.

        Either-limb rather than both, deliberately: a breach of significant scale is
        notifiable whatever the harm judgement says, and a breach causing significant harm
        to one person is notifiable whatever the count is. Reading the two as a conjunction
        is the single most likely way to under-notify.
        """
        if self.harm.significant_harm or self.scale_is_significant:
            return Notifiability.NOTIFIABLE
        if self.scale_is_significant is None:
            return Notifiability.UNDETERMINED
        return Notifiability.NOT_NOTIFIABLE


class ExceptionGround(enum.StrEnum):
    """The label a decision not to notify individuals is filed under.

    Labels for filing a human's decision, not tests this module applies. Nothing here
    evaluates whether remedial action was sufficient or whether protection was adequate;
    those are the judgement, and `NotificationException` records who made it.
    """

    REMEDIAL_ACTION = "remedial_action"
    TECHNOLOGICAL_PROTECTION = "technological_protection"
    COMMISSION_DIRECTION = "commission_direction"


class NotificationException(BaseModel):
    """A recorded decision not to notify affected individuals."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ground: ExceptionGround
    decided_by: str = Field(pattern=IDENTIFIER)
    decided_at: datetime
    rationale_reference: str = Field(pattern=IDENTIFIER)

    @field_validator("decided_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            msg = "a determination timestamp must be timezone-aware"
            raise ValueError(msg)
        return v


class ObligationKind(enum.StrEnum):
    ASSESS = "assess"
    NOTIFY_COMMISSION = "notify_commission"
    NOTIFY_INDIVIDUALS = "notify_individuals"


class ObligationBasis(enum.StrEnum):
    """Whether an obligation comes from the Act or from the regulator's guidance.

    Carried on every obligation because the two are not the same thing and a console that
    renders them identically teaches people that they are. Missing a statutory deadline and
    missing a published expectation have different consequences, and conflating them makes
    the statutory one look negotiable.
    """

    STATUTORY = "statutory"
    GUIDELINE = "guideline"


class Obligation(BaseModel):
    """One thing owed, when it is owed by, and whether it was done in time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ObligationKind
    basis: ObligationBasis
    #: None when the deadline cannot yet be computed, which is itself the finding: a case
    #: whose assessment is undetermined has no notification deadline because the event the
    #: deadline runs from has not happened.
    due_before: datetime | None = None
    satisfied: bool = False
    #: True only when we can say so: the deadline exists, and it passed unsatisfied.
    overdue: bool = False
    #: Satisfied, but after the deadline. Recorded rather than hidden, in the same spirit as
    #: an export that carries the break it found.
    satisfied_late: bool = False
    #: A finding that is not about time: the individuals were told before the Commission.
    out_of_order: bool = False


def _end_of_day_after(moment: datetime, days: int) -> datetime:
    """The exclusive instant a deadline of `days` calendar days from `moment` expires.

    Calendar days counted in Singapore, and the day of the triggering event is day zero. So
    three days from an assessment made at any time on the 1st expires at the start of the
    5th: the 2nd, 3rd and 4th are the three days, and the whole of the 4th is available.

    An exclusive bound rather than "the last microsecond of the 4th", because a comparison
    against an exclusive bound has no rounding to get wrong and reads the same whatever
    precision the timestamps carry.
    """
    local_date = moment.astimezone(SINGAPORE).date()
    return datetime.combine(local_date + timedelta(days=days + 1), time.min, tzinfo=SINGAPORE)


class BreachCase(BaseModel):
    """One suspected breach, from the moment we had reason to believe it.

    The model records what happened and reports what is wrong with it. It does not refuse to
    record a late notification, a notification made in the wrong order, or an assessment
    that has not concluded, because a workflow that will not accept the truth about an
    incident is a workflow people keep the real dates out of.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str = Field(pattern=IDENTIFIER)
    awareness: Awareness

    #: Recorded because a post-incident review asks for it, and used by nothing.
    #:
    #: This field is deliberately inert. Confirmation is the date the breach was established
    #: as real, and it is always later than the date there was reason to believe: using it
    #: would move every deadline later by the length of the investigation, which is the
    #: exact error this leaf names. It is a field with no reader, and the invariant suite
    #: asserts that moving it moves nothing.
    confirmed_at: datetime | None = None

    assessment: Assessment | None = None
    commission_notified_at: datetime | None = None
    individuals_notified_at: datetime | None = None
    individuals_exception: NotificationException | None = None

    @field_validator("confirmed_at", "commission_notified_at", "individuals_notified_at")
    @classmethod
    def _tz_aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            msg = "case timestamps must be timezone-aware; a naive one is a silent bug"
            raise ValueError(msg)
        return v

    @property
    def assessment_due_before(self) -> datetime:
        """When the assessment should be complete. A guideline benchmark, not a deadline."""
        return _end_of_day_after(self.awareness.clock_starts_at, ASSESSMENT_GUIDELINE_DAYS)

    @property
    def commission_due_before(self) -> datetime | None:
        """Three calendar days after the day of the assessment, or None.

        None in two different situations that are worth telling apart when reading a case:
        no assessment yet, and an assessment that concluded the breach was not notifiable or
        could not be determined. Both mean there is no deadline *yet*; neither means there
        is nothing to do, which is what `outstanding` is for.
        """
        if self.assessment is None or self.assessment.outcome is not Notifiability.NOTIFIABLE:
            return None
        return _end_of_day_after(self.assessment.assessed_at, COMMISSION_NOTIFICATION_DAYS)

    def outstanding(self, now: datetime) -> tuple[Obligation, ...]:
        """Everything owed on this case as at `now`, satisfied or not.

        Returns the full set rather than only the unmet ones, because the question asked of
        a breach case is not "what is left" but "show me the whole clock": a satisfied
        obligation that was satisfied late is a finding, and it disappears from a list of
        outstanding work.

        `now` is a parameter with no default for the reason the ledger gives about its own
        timestamps. A deadline report that reads the container's clock is a report that says
        something different depending on which worker rendered it.
        """
        return (
            self._assess_obligation(now),
            self._commission_obligation(now),
            self._individuals_obligation(),
        )

    def _assess_obligation(self, now: datetime) -> Obligation:
        due = self.assessment_due_before
        # An assessment that came out UNDETERMINED has not been made. Treating it as done is
        # how a case sits forever with nothing overdue and nothing decided.
        done = self.assessment is not None and self.assessment.outcome is not (
            Notifiability.UNDETERMINED
        )
        assessed_at = self.assessment.assessed_at if done and self.assessment else None
        return Obligation(
            kind=ObligationKind.ASSESS,
            basis=ObligationBasis.GUIDELINE,
            due_before=due,
            satisfied=done,
            overdue=not done and now >= due,
            satisfied_late=assessed_at is not None and assessed_at >= due,
        )

    def _commission_obligation(self, now: datetime) -> Obligation:
        due = self.commission_due_before
        done = self.commission_notified_at is not None
        return Obligation(
            kind=ObligationKind.NOTIFY_COMMISSION,
            basis=ObligationBasis.STATUTORY,
            due_before=due,
            satisfied=done,
            overdue=due is not None and not done and now >= due,
            satisfied_late=(
                due is not None
                and self.commission_notified_at is not None
                and self.commission_notified_at >= due
            ),
        )

    def _individuals_obligation(self) -> Obligation:
        """No deadline of its own: the constraint is ordering, not elapsed time.

        The duty is to notify affected individuals at the same time as, or after, the
        Commission. There is therefore nothing to be overdue against, and inventing a
        deadline here (three days again, say) would put a number in a console that no
        instrument supports. What can go wrong is the order, and that is what is reported.
        """
        excused = self.individuals_exception is not None
        told = self.individuals_notified_at is not None
        out_of_order = (
            self.individuals_notified_at is not None
            and self.commission_notified_at is not None
            and self.individuals_notified_at < self.commission_notified_at
        )
        return Obligation(
            kind=ObligationKind.NOTIFY_INDIVIDUALS,
            basis=ObligationBasis.STATUTORY,
            due_before=None,
            satisfied=told or excused,
            out_of_order=out_of_order,
        )

    def findings(self, now: datetime) -> tuple[str, ...]:
        """Everything wrong with this case, in a form a person can read.

        Fixed sentences assembled from the case's own state, with no detail of the breach in
        any of them, so the summary can be circulated to people who are handling the clock
        rather than the incident.
        """
        found: list[str] = []
        for obligation in self.outstanding(now):
            if obligation.overdue:
                found.append(f"{obligation.kind.value} is overdue ({obligation.basis.value})")
            if obligation.satisfied_late:
                found.append(f"{obligation.kind.value} was done late ({obligation.basis.value})")
            if obligation.out_of_order:
                found.append("affected individuals were notified before the Commission")
        if self.assessment is not None and self.assessment.outcome is Notifiability.UNDETERMINED:
            found.append("the assessment is undetermined: the number affected is not established")
        if self.assessment is None and now >= self.assessment_due_before:
            found.append("no assessment has been recorded")
        return tuple(found)


def open_case(
    *,
    case_id: str,
    became_aware_at: datetime,
    basis: AwarenessBasis,
    source: AwarenessSource,
    recorded_at: datetime,
    recorded_by: str,
    evidence_reference: str,
    earliest_possible_at: datetime | None = None,
) -> BreachCase:
    """Open a case from an awareness that must be stated in full.

    Every argument is keyword-only and none has a default except the bound that only applies
    to an estimate. That is the whole of the function's value: there is no shorter way to
    open a case, so there is no path on which the awareness time is inferred, and a caller
    who does not know when the organisation became aware has to go and find out rather than
    accept whatever a default would have given them.
    """
    return BreachCase(
        case_id=case_id,
        awareness=Awareness(
            became_aware_at=became_aware_at,
            basis=basis,
            source=source,
            earliest_possible_at=earliest_possible_at,
            recorded_at=recorded_at,
            recorded_by=recorded_by,
            evidence_reference=evidence_reference,
        ),
    )


def deadline_summary(cases: Iterable[BreachCase], now: datetime) -> Mapping[str, tuple[str, ...]]:
    """Findings per case, for the one screen somebody looks at on a bad week.

    Keyed by case id and sorted, so two renderings of the same set agree. Cases with nothing
    wrong are present with an empty tuple rather than absent: a case missing from this map
    should mean it was not passed in, never that it is fine.
    """
    return {case.case_id: case.findings(now) for case in sorted(cases, key=lambda c: c.case_id)}
