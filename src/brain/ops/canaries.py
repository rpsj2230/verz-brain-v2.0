"""Asking the same question as three people and checking the answers differ correctly.

Every other guard in this system is a rule applied at a boundary. This is the one that
checks the rules were applied at all, by asking a real question as a real principal and
looking at what came back. A permission model is a claim about what somebody sees, and the
only way to test a claim of that shape is to look.

**A canary is a value nobody would ever type.** `CANARY-CONTRACT-7Q4XZ` sitting in a
restricted field means an answer containing that string did not get it from the model's
imagination, from a plausible-looking fixture row, or from a coincidence. It leaked. That is
the whole reason the fixtures use improbable strings rather than realistic ones: a realistic
value in an answer is an argument, and an improbable one is a fact.

**This module holds none of the canaries, and that is the split the repository keeps.** The
canary map and the golden questions live in `tests/fixtures`, because they are fixtures, and
`src` importing from `tests` would be a package that cannot be installed without its own test
suite. So everything here takes the map as an argument. The same split `brain.ops.limits` and
`brain.ops.limit_store` make, for the same reason: the case that is always wrong is the one
you cannot reach through the module that owns the data.

**A finding names the field and never the value.** This is the decision in the module that
took the longest and it goes the other way from `brain.ops.denial_alerts`, deliberately. An
alert there names a shape and never a thing, because its reader is somebody who may not be
entitled to what was denied. A canary finding's reader is whoever runs the canary suite, and
a finding that would not say which field leaked is a finding nobody can act on. So it names
`client.contract_value`.

What it must never carry is the token itself, and the reason is not the reader: it is
everywhere the finding goes afterwards. A finding is pasted into a ticket, printed in CI
output, attached to an alert and read by whoever is on call. A token in any of those is the
leak happening a second time, through the report about the leak, into places with weaker
access control than the system that leaked, so a token in a finding is the leak happening a
second time. See `A_FINDING_REPEATS_THE_LEAK_IF_IT_CARRIES_THE_VALUE`.

**A refusal is compared byte for byte, and that is not fussiness.** DENIED and ABSENT must be
indistinguishable, and a refusal that differs from a genuine absence by one character is
distinguishable to anybody who can send two questions. Trailing whitespace, a full stop, a
capital: each of those is a bit of information about whether the thing exists, and a caller
who can ask repeatedly can spend that bit on every field in the system. So the comparison is
equality, not similarity, and the finding does not report how they differed. Reporting the
difference would put the distinguishing text into the report.

**Nothing here runs anything.** No model call, no HTTP, no database. Every function takes
what was observed and returns what is wrong with it, so the whole module is exercised without
a socket and a scheduled run is a caller's job. `brain.models.health.due_probes` is shaped the
same way and says why: something else owns the schedule, and that something can be a worker
tick, a cron entry, or a test advancing a clock by hand, and the third is why.

**The production run is deliberately narrower than the fixture run, and this is a refusal
rather than a limitation.** Running the full suite in production would mean writing canary
values into a client's records, and there is no version of that which is safe: an improbable
string in a live table is a value somebody will eventually see in a report, an export or a
backup, and the one thing worse than a leak is a leak the system created on purpose. So the
production run asserts the shape of refusals and the tool projection, both of which need no
planted value, and `PRODUCTION_NEVER_PLANTS_A_CANARY` says so where somebody will read it
before deciding the coverage looks thin.

Task ids: M28.2.1, M28.2.2, M28.2.3, M28.2.4
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

#: Why a finding names the field and refuses to carry the value that proved it.
A_FINDING_REPEATS_THE_LEAK_IF_IT_CARRIES_THE_VALUE = (
    "A canary finding names the field, because a report that will not say which field "
    "leaked is a report nobody can act on. It never carries the token. The reason is not "
    "who reads the finding, it is where the finding goes: into a ticket, into CI output, "
    "into an alert, into whatever the person on call pastes it in to. Every one of those "
    "has weaker access control than the system that leaked, so a token in a finding is the "
    "leak happening a second time through the report about the leak."
)

#: Why a refusal is compared for equality rather than for resemblance.
A_REFUSAL_THAT_DIFFERS_BY_ONE_CHARACTER_IS_DISTINGUISHABLE = (
    "DENIED and ABSENT are indistinguishable or they are not. A refusal differing from a "
    "genuine absence by a full stop, a capital or a trailing space is one bit about whether "
    "the thing exists, and a caller who can ask twice can spend that bit on every field in "
    "the system. So the check is equality and the finding does not report how the two "
    "differed: printing the difference would put the distinguishing text into the report."
)

#: Why the scheduled production run does less than the fixture run, on purpose.
PRODUCTION_NEVER_PLANTS_A_CANARY = (
    "The fixture suite works because restricted fields hold improbable strings. Doing that "
    "in production means writing those strings into a client's records, where they will "
    "eventually surface in a report, an export or a backup, and a leak the system created "
    "deliberately is worse than the one it was guarding against. So the production run "
    "asserts what needs no planted value: that a refusal is byte-identical to an absence, "
    "and that the tool catalogue projected for a principal is exactly what their "
    "entitlements admit. That is narrower and it is the part that can run against real "
    "data without adding a hazard."
)

#: How often a production canary run is worth making, in seconds.
#:
#: Twelve hours rather than hourly, and the figure follows from what the run can detect
#: rather than from how often anybody would like reassurance. What it catches is a change in
#: what the gate admits, and those arrive with a deployment. Running between deployments
#: measures the same unchanged system repeatedly, at the cost of synthetic requests against
#: production on every one.
#:
#: A caller that runs it on every deployment as well is doing the right thing; this is the
#: floor for an estate nobody has deployed to in a while, not a substitute for that.
CANARY_INTERVAL_SECONDS: int = 12 * 60 * 60


class Finding(enum.StrEnum):
    """What a canary run found. One member per thing that can be wrong."""

    #: A canary value reached somewhere it should not have.
    LEAKED = "leaked"
    #: A refusal was not byte-identical to a genuine absence.
    REFUSAL_DISTINGUISHABLE = "refusal_distinguishable"
    #: A field the caller may not read came back present rather than locked or absent.
    UNLOCKED = "unlocked"
    #: The projected catalogue offered a tool this principal's entitlements do not admit.
    PROJECTION_TOO_WIDE = "projection_too_wide"
    #: The projected catalogue withheld a tool this principal's entitlements do admit.
    PROJECTION_TOO_NARROW = "projection_too_narrow"


@dataclass(frozen=True)
class CanaryFinding:
    """One thing that was wrong, in terms an operator can act on and a log can carry.

    `field` is a name like `client.contract_value` and `token` is deliberately not a member
    of this class. There is no attribute to put a canary value in, which is the structural
    half of `A_FINDING_REPEATS_THE_LEAK_IF_IT_CARRIES_THE_VALUE`: a future edit that wanted
    to include the value for debugging would have to add a field, and adding a field to a
    frozen dataclass in a module whose docstring argues against it is a decision rather than
    a slip.
    """

    kind: Finding
    #: Who was asking. A principal id, not a name.
    asker: str
    #: Which question, by its id in the corpus.
    question_id: str
    #: The field or tool the finding is about. A name, never a value.
    subject: str

    def __post_init__(self) -> None:
        for name in ("asker", "question_id", "subject"):
            if not getattr(self, name):
                msg = f"a canary finding with no {name} names nothing anybody can act on"
                raise ValueError(msg)

    def line(self) -> str:
        """One line, safe to paste anywhere. Carries no value by construction."""
        return f"{self.kind.value}: {self.asker} on {self.question_id}, {self.subject}"


def leaked_fields(text: str, canaries: Mapping[str, str]) -> frozenset[str]:
    """Which fields' canaries appear in this text. Field names out, never tokens.

    The inversion is the point rather than a convenience. A function returning the tokens it
    found would put them in the caller's hands and then into whatever the caller logged, and
    every caller here logs. Returning the field name means the value has already been dropped
    by the time anybody can be careless with it.

    Substring rather than word boundary, because a canary appearing inside a larger string is
    still a canary that reached the reader, and the tokens are improbable enough that a false
    positive would itself be worth investigating.
    """
    return frozenset(field for field, token in canaries.items() if token in text)


def scan(
    *,
    asker: str,
    question_id: str,
    text: str,
    canaries: Mapping[str, str],
    permitted: Iterable[str] = (),
) -> tuple[CanaryFinding, ...]:
    """Every canary in one piece of text that this asker was not entitled to.

    `permitted` is the fields this principal may genuinely read, and it exists so the check
    has a positive side. A canary suite that only ever asserts absence is satisfied by a
    system that answers nothing at all, which is the failure this repository names in
    CLAUDE.md: a guard tested only by its refusals is satisfied by a function that refuses
    everything. A caller entitled to the contract value should see the canary, and finding it
    there is correct.
    """
    allowed = set(permitted)
    return tuple(
        CanaryFinding(kind=Finding.LEAKED, asker=asker, question_id=question_id, subject=field)
        for field in sorted(leaked_fields(text, canaries))
        if field not in allowed
    )


def scan_stores(
    *,
    asker: str,
    question_id: str,
    stores: Mapping[str, Sequence[str]],
    canaries: Mapping[str, str],
    permitted: Iterable[str] = (),
) -> tuple[CanaryFinding, ...]:
    """The same check across every place a run leaves text behind.

    **The answer is the least likely of these to leak, which is why the others are here.**
    The answer is what the redactor is pointed at, so it is the one surface with a guard in
    front of it. A trace span, a stored payload and a log line are written by code that was
    thinking about debugging, and `brain.ops.tracing` masks `payload_in` and `payload_out`
    precisely because somebody noticed that once. This is what checks the masking happened.

    The store's name travels into the finding's subject alongside the field, so a leak into
    the payload store and a leak into the answer are different findings rather than one
    reported twice.
    """
    findings: list[CanaryFinding] = []
    for store in sorted(stores):
        for text in stores[store]:
            findings.extend(
                CanaryFinding(
                    kind=one.kind,
                    asker=one.asker,
                    question_id=one.question_id,
                    subject=f"{store}:{one.subject}",
                )
                for one in scan(
                    asker=asker,
                    question_id=question_id,
                    text=text,
                    canaries=canaries,
                    permitted=permitted,
                )
            )
    return tuple(findings)


def compare_askers(
    *,
    question_id: str,
    answers: Mapping[str, str],
    refused: Iterable[str],
    absence_text: str,
) -> tuple[CanaryFinding, ...]:
    """One question, several askers, and whether the refusals are indistinguishable.

    The interesting comparison is not between two answers, which differ for every legitimate
    reason under the sun. It is between a refusal and a genuine absence, which must not
    differ at all.

    `absence_text` is passed in rather than imported, because the sentence a refusal produces
    belongs to whatever answered, and a copy of it here would be a second opinion about the
    most load-bearing string in the system. The corpus holds it and hands it over.

    Nothing is reported about the answers of askers who were not refused. A finding saying
    two permitted answers differed would be a fact about the difference between two people's
    entitlements, in a report that travels.
    """
    findings: list[CanaryFinding] = []
    for asker in sorted(set(refused)):
        answer = answers.get(asker)
        if answer is None:
            findings.append(
                CanaryFinding(
                    kind=Finding.REFUSAL_DISTINGUISHABLE,
                    asker=asker,
                    question_id=question_id,
                    subject="no answer was recorded",
                )
            )
        elif answer != absence_text:
            findings.append(
                CanaryFinding(
                    kind=Finding.REFUSAL_DISTINGUISHABLE,
                    asker=asker,
                    question_id=question_id,
                    subject="refusal is not the absence sentence",
                )
            )
    return tuple(findings)


def locked_findings(
    *,
    asker: str,
    question_id: str,
    expected_locked: Iterable[str],
    reported_locked: Iterable[str],
) -> tuple[CanaryFinding, ...]:
    """Fields that should have come back locked and did not.

    Only one direction is a finding. A field locked that nobody expected to be locked is the
    system withholding more than the corpus predicted, which is a difference worth knowing
    about and is not a permission failure, and reporting it here would put "this field was
    available to you and now is not" into a travelling report.
    """
    missing = set(expected_locked) - set(reported_locked)
    return tuple(
        CanaryFinding(kind=Finding.UNLOCKED, asker=asker, question_id=question_id, subject=field)
        for field in sorted(missing)
    )


def projection_findings(
    *,
    asker: str,
    offered: Iterable[str],
    admissible: Iterable[str],
) -> tuple[CanaryFinding, ...]:
    """Whether the tool catalogue projected for a principal is exactly what they may invoke.

    **Both directions are findings here, and that is the difference from `locked_findings`
    one function up.** A catalogue that is too wide names a tool this principal cannot
    invoke, and a tool name is a fact about what the installation does: `read_invoice_ledger`
    in a list tells its reader there is an invoice ledger. A catalogue that is too narrow
    withholds something they are entitled to, which is not a disclosure but is a broken
    product, and both are the projection failing to be a function of the entitlements.

    `question_id` is the constant below rather than a real question, because a projection is
    computed before any question is asked. Recorded rather than left blank so a finding still
    says where it came from when it is read next to the others.
    """
    offered_set, admissible_set = set(offered), set(admissible)
    findings = [
        CanaryFinding(
            kind=Finding.PROJECTION_TOO_WIDE,
            asker=asker,
            question_id=PROJECTION_CHECK,
            subject=name,
        )
        for name in sorted(offered_set - admissible_set)
    ]
    findings.extend(
        CanaryFinding(
            kind=Finding.PROJECTION_TOO_NARROW,
            asker=asker,
            question_id=PROJECTION_CHECK,
            subject=name,
        )
        for name in sorted(admissible_set - offered_set)
    )
    return tuple(findings)


#: What a projection finding names instead of a question. A projection is computed before
#: anything is asked, so there is no question id to carry and a blank one would fail the
#: finding's own constructor.
PROJECTION_CHECK = "projection"


def due(
    *, last_run: datetime | None, now: datetime, interval_seconds: int = CANARY_INTERVAL_SECONDS
) -> bool:
    """Whether a scheduled run is owed.

    A function rather than a loop or a thread, following `brain.models.health.due_probes`:
    something else owns the schedule and calls this, and that something can be a worker tick,
    a cron entry or a test moving a clock, and the third is why it is shaped this way.

    A run that has never happened is always owed, so a fresh install checks itself rather
    than waiting half a day to find out whether its gate works.
    """
    if last_run is None:
        return True
    return now - last_run >= timedelta(seconds=interval_seconds)


def alert_lines(findings: Sequence[CanaryFinding]) -> tuple[str, ...]:
    """What a scheduled run says when it found something, one line per finding.

    Sorted and de-duplicated so a run that asked the same question as several askers does not
    report one defect several times, and so two runs of an unchanged system produce the same
    text: an alert whose wording moves on every run is one people stop reading.

    There is no count anywhere in the output, and no summary line. "Seven findings" is a
    number about hidden things when the recipient is not entitled to all seven subjects, and
    the recipient of an operational alert generally is not. Each line stands alone, which is
    also what makes them safe to route separately.
    """
    return tuple(sorted({one.line() for one in findings}))
