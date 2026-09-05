"""Finding personal data in free text so it can be scrubbed before it leaves the building.

This module exists for one moment: text is about to be sent to a model or a service that
is not ours, and whatever identifiers are in it are about to become somebody else's log
line. It scrubs that text. It does nothing else, and the things it deliberately does not do
are most of its design.

**It is never an authorisation boundary.** A detector decides what a string looks like, not
what a person may see. Permission in this system is `brain.core.entitlement` and
`brain.core.redaction`, both of which work from grants and a field policy and neither of
which asks what the text resembles. The failure mode of using a detector as a boundary is
specific and quiet: the detector misses one NRIC in one message, the message is treated as
clean, and a control that was never a control is discovered to have never been one. See
`NEVER_AN_AUTHORISATION_BOUNDARY`, and note that `Detection` has no field that could carry
a refusal - the same construction `brain.gate.injection` uses, and for the same reason. A
future caller cannot start blocking on a detection without adding somewhere to put the
block and being seen doing it in review.

**A detection is telemetry.** It is counted, it is worth alerting on when the rate moves,
and it never refuses a question. A scrubber that refused would teach people to rephrase
until they got through, which is training a workforce to defeat the thing protecting them.

**The checksum raises confidence and never decides.** A string shaped like an NRIC is
redacted whether or not its check letter validates, because a mistyped NRIC is still
personal data and a deliberately corrupted one is still a person's number with a digit
changed. This matters more than it looks: the NRIC check-letter algorithm was never
published by the government, the version below is the widely reproduced reverse-engineered
one, and only the S/T/F/G branches have a public worked example to check against. Making
redaction depend on it would make a leak depend on an algorithm nobody can verify. Making
confidence depend on it costs nothing if it is wrong.

**The deterministic recognisers run in this process, not in the Presidio container.** A
scrubber reached over the network fails open on a timeout, and failing open here means the
unscrubbed text goes to the third party. Regexes for the Singapore formats need no model
and no network, so the case that must never fail has no dependency that can. The container
is for what genuinely needs a model - English `PERSON`, and the entity types listed in
`PRESIDIO_BUILT_INS` - and a failure there degrades the scrub rather than removing it.

**Span completeness is the whole of the local name handling.** Presidio's English NER
recognises `Abdullah` in "Nur Aisyah binti Abdullah" and leaves the rest, recognises
`Kumar` in "R. Kumar" and leaves the initial, and reports success both times. A
half-redacted name identifies a person as well as a whole one does, and it looks handled,
which is worse than looking broken. So the local recognisers are built around the
connectors and initials that mark where a name really starts and ends. Romanised Chinese
names with no connector - "Tan Wei Ling" - are not detectable by pattern and are the reason
M32.2.1.2 exists; they are not claimed here, and pretending otherwise would be the exact
failure this paragraph is about.

Over-redaction is the accepted cost throughout. An eight-digit number followed by a letter
is a UEN and is also an order reference; a capital letter, a full stop and a surname is an
initialled name and is also the end of one sentence before a proper noun. Both are
redacted. The asymmetry is not close: a missed identifier is in a third party's logs
permanently, and an over-redacted order number costs a little context in one prompt.

Task ids: M32.2.1.1, M32.2.1.3, M32.2.1.4, M32.2.2.1, M32.2.2.2, M32.2.2.3
"""

from __future__ import annotations

import enum
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

#: Written down where a reader of the code meets it, not only in the docstring. This
#: string is asserted by the test suite, so deleting the rule deletes a test.
NEVER_AN_AUTHORISATION_BOUNDARY: Final = (
    "Detection is telemetry. Nothing in this module decides what a principal may see; "
    "that is brain.core.entitlement and brain.core.redaction, which work from grants "
    "rather than from what a string resembles."
)


class EntityKind(enum.StrEnum):
    """What was found. Closed, because the scrub label goes into the outgoing text.

    An open vocabulary would let a new recogniser invent a label, and the label is what a
    model downstream sees in place of the value; two names for one thing make the redacted
    text less readable than the thing it replaced.
    """

    NRIC = "sg_nric"
    FIN = "sg_fin"
    UEN = "sg_uen"
    SG_PHONE = "sg_phone"
    EMAIL = "email"
    #: A name joined by bin / binti / s/o / d/o / a/l / a/p.
    PATRONYMIC_NAME = "person_name_patronymic"
    #: An initialled name, the usual written form of a Tamil name.
    INITIALLED_NAME = "person_name_initialled"
    #: A run of Han characters of name length.
    CJK_NAME = "person_name_cjk"


@dataclass(frozen=True)
class Detection:
    """One thing found, where it was, and how sure the recogniser is.

    There is no field here that could hold a refusal, an allow, or a severity that a caller
    might read as one. That absence is the mechanism: a caller wanting to block on a
    detection has nowhere to put the decision and has to add one, in a diff.
    """

    kind: EntityKind
    start: int
    end: int
    confidence: float

    def __post_init__(self) -> None:
        if self.end <= self.start:
            msg = f"detection {self.kind} spans nothing ({self.start}..{self.end})"
            raise ValueError(msg)
        if not 0.0 < self.confidence <= 1.0:
            msg = (
                f"detection {self.kind} has confidence {self.confidence}, which is not a confidence"
            )
            raise ValueError(msg)


# ----------------------------------------------------------------- Singapore formats
#: NRIC and FIN: one prefix letter, seven digits, one check letter. S and T are citizens
#: and permanent residents by century of issue; F and G are foreign identification
#: numbers; M is the series issued from 2022 for foreigners.
_NRIC_RE: Final = re.compile(r"(?<![A-Za-z0-9])([STFGM])(\d{7})([A-Z])(?![A-Za-z0-9])")

_NRIC_WEIGHTS: Final = (2, 7, 6, 5, 4, 3, 2)
_ST_CHECK_LETTERS: Final = "JZIHGFEDCBA"
_FG_CHECK_LETTERS: Final = "XWUTRQPNMLK"


def nric_check_letter(prefix: str, digits: str) -> str:
    """The check letter the published derivation gives for this prefix and these digits.

    Worked example, which is the one the algorithm is usually quoted with: S1234567 weights
    to 106, 106 mod 11 is 7, and the eighth letter of the S/T table is D. `S1234567D`.

    The M branch reverses the index into the F/G table, per the same source. There is no
    public worked example for it, which is recorded here rather than hidden: it is why
    nothing in this module treats a failed checksum as a reason not to redact.
    """
    total = sum(w * int(d) for w, d in zip(_NRIC_WEIGHTS, digits, strict=True))
    if prefix in ("T", "G"):
        total += 4
    elif prefix == "M":
        total += 3
    remainder = total % 11
    if prefix in ("S", "T"):
        return _ST_CHECK_LETTERS[remainder]
    if prefix == "M":
        return _FG_CHECK_LETTERS[10 - remainder]
    return _FG_CHECK_LETTERS[remainder]


def nric_checksum_holds(value: str) -> bool:
    """Whether a candidate's check letter agrees with its digits. Confidence only."""
    match = _NRIC_RE.fullmatch(value.strip().upper())
    if match is None:
        return False
    prefix, digits, check = match.groups()
    return nric_check_letter(prefix, digits) == check


#: UEN, longest form first. Ordering is load-bearing: the nine-digit local-company form
#: starts with something the eight-digit business form matches, and a shorter match taken
#: first leaves a digit and a check letter outside the redacted span.
_UEN_LOCAL_COMPANY_RE: Final = re.compile(r"(?<![A-Za-z0-9])((?:19|20)\d{7})([A-Z])(?![A-Za-z0-9])")
_UEN_OTHER_ENTITY_RE: Final = re.compile(
    r"(?<![A-Za-z0-9])([TSR]\d{2}[A-Z]{2}\d{4})([A-Z])(?![A-Za-z0-9])"
)
_UEN_BUSINESS_RE: Final = re.compile(r"(?<![A-Za-z0-9])(\d{8})([A-Z])(?![A-Za-z0-9])")

#: Singapore numbers are eight digits beginning 3, 6, 8 or 9, optionally with the country
#: code. The lookaround is the point: without it the pattern matches the middle of a
#: longer reference number and redacts eight digits out of the centre of it, which reads
#: as a corrupted string rather than as a redaction.
_SG_PHONE_RE: Final = re.compile(r"(?<![\w+])(?:\+65[ -]?)?[3689]\d{3}[ -]?\d{4}(?!\d)")

_EMAIL_RE: Final = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+\.[\w.-]*[\w](?![\w-])")


# ----------------------------------------------------------------- name forms
#: The connectors that make a Malay or Indian Singaporean name one span rather than two.
#: Case-insensitive only for the connector; the surrounding tokens must be capitalised, or
#: "a/l" in a fraction would drag two ordinary words into a name.
_CONNECTOR: Final = r"(?i:bin|binte|binti|s/o|d/o|a/l|a/p)"
_NAME_WORD: Final = r"[A-Z][\w'-]*"

#: At most two extra tokens each side. Unbounded repetition swallows the capitalised word
#: after the name, and a redaction that eats the next sentence's first word is how people
#: conclude the scrubber is broken and ask for it to be turned off.
_PATRONYMIC_NAME_RE: Final = re.compile(
    rf"\b{_NAME_WORD}(?:\s+{_NAME_WORD}){{0,2}}\s+{_CONNECTOR}\s+{_NAME_WORD}(?:\s+{_NAME_WORD}){{0,2}}"
)

#: An initial, or several, then a surname. The written form of most Tamil names here.
_INITIALLED_NAME_RE: Final = re.compile(r"\b(?:[A-Z]\.\s*){1,3}[A-Z][a-z]+")

#: Two to four Han characters: the length a Chinese name is written in. This over-matches
#: in Chinese-language prose, where two-character words are ordinary, and it is kept
#: because the alternative is that a Chinese name written in Chinese passes through
#: untouched while the same person's romanised name does not.
_CJK_NAME_RE: Final = re.compile(r"[一-鿿]{2,4}")


@dataclass(frozen=True)
class Recogniser:
    """One pattern, what it finds, and how sure it is before any checksum runs.

    `confirm` raises confidence when it holds and never lowers it below the base. A
    recogniser whose confirmation could veto a detection would be a recogniser that decides
    whether to redact, which is the thing this module refuses to have.
    """

    kind: EntityKind
    pattern: re.Pattern[str]
    base_confidence: float
    confirm: Callable[[str], bool] | None = None
    confirmed_confidence: float = 1.0


RECOGNISERS: Final[tuple[Recogniser, ...]] = (
    Recogniser(EntityKind.NRIC, _NRIC_RE, 0.6, nric_checksum_holds, 0.95),
    Recogniser(EntityKind.UEN, _UEN_LOCAL_COMPANY_RE, 0.6),
    Recogniser(EntityKind.UEN, _UEN_OTHER_ENTITY_RE, 0.7),
    Recogniser(EntityKind.UEN, _UEN_BUSINESS_RE, 0.4),
    Recogniser(EntityKind.SG_PHONE, _SG_PHONE_RE, 0.6),
    Recogniser(EntityKind.EMAIL, _EMAIL_RE, 0.9),
    Recogniser(EntityKind.PATRONYMIC_NAME, _PATRONYMIC_NAME_RE, 0.85),
    Recogniser(EntityKind.INITIALLED_NAME, _INITIALLED_NAME_RE, 0.5),
    Recogniser(EntityKind.CJK_NAME, _CJK_NAME_RE, 0.3),
)

#: FIN has no recogniser of its own: F, G and M share the NRIC format exactly, so one
#: pattern finds both and the label would be a guess about a person's status that this
#: system has no business making in a redaction token.
_KINDS_WITHOUT_OWN_RECOGNISER: Final[frozenset[EntityKind]] = frozenset({EntityKind.FIN})


# ----------------------------------------------------------------- Presidio configuration
@dataclass(frozen=True)
class BuiltIn:
    """A Presidio recogniser we rely on, and the threshold below which we ignore it.

    `why_not_local` is required prose. Every entry here is a network dependency in the path
    of a scrub, so each one has to justify not being a regex in this file.
    """

    presidio_name: str
    score_threshold: float
    why_not_local: str

    def __post_init__(self) -> None:
        if not 0.0 < self.score_threshold <= 1.0:
            msg = f"{self.presidio_name} has threshold {self.score_threshold}"
            raise ValueError(msg)
        if not self.why_not_local.strip():
            msg = f"{self.presidio_name} does not say why it is not a local pattern"
            raise ValueError(msg)


#: The language the analyser is configured for. One, deliberately: Presidio loads a spaCy
#: model per language and three models is most of the container's memory limit. Chinese,
#: Malay and Tamil names in this system are handled by the local recognisers above and by
#: the model in M32.2.1.2, not by three more spaCy pipelines.
PRESIDIO_LANGUAGE: Final = "en"

PRESIDIO_BUILT_INS: Final[tuple[BuiltIn, ...]] = (
    BuiltIn("PERSON", 0.6, "an English name has no fixed shape; this is what a model is for"),
    BuiltIn("CREDIT_CARD", 0.7, "Luhn plus issuer prefixes, already implemented and tested there"),
    BuiltIn("IBAN_CODE", 0.7, "country-specific lengths and a mod-97 check per country"),
    BuiltIn("IP_ADDRESS", 0.6, "v6 forms are not worth reimplementing badly"),
    BuiltIn("URL", 0.5, "a URL frequently carries an identifier in a path segment"),
)

#: Entities Presidio ships that we deliberately do not enable. Recorded because an absence
#: is otherwise indistinguishable from an oversight the next time somebody reads the list.
PRESIDIO_DECLINED: Final[dict[str, str]] = {
    "DATE_TIME": "every ticket has dates; redacting them removes the thing the answer is about",
    "NRP": "nationality and religion appear in ordinary business text and identify nobody here",
    "LOCATION": "a client's address is governed by the field policy, not by a text scrubber",
}


def configuration_gaps() -> tuple[str, ...]:
    """Every entity kind with nowhere to be found, and every duplicated responsibility.

    Two directions, because both have happened elsewhere in this codebase. A kind with no
    recogniser is a label that can never be produced, so a caller checking for it waits
    forever. A kind covered both locally and by a built-in is two answers to one question,
    and the loser is whichever one the merge happens to drop.
    """
    findings: list[str] = []
    have = {r.kind for r in RECOGNISERS} | _KINDS_WITHOUT_OWN_RECOGNISER
    for kind in EntityKind:
        if kind not in have:
            findings.append(f"{kind.value}: declared as a kind with no recogniser and no exemption")
    local_names = {r.kind.name for r in RECOGNISERS}
    for built_in in PRESIDIO_BUILT_INS:
        if built_in.presidio_name in local_names:
            findings.append(
                f"{built_in.presidio_name}: covered locally and by Presidio; one of the two is dead"
            )
        if built_in.presidio_name in PRESIDIO_DECLINED:
            findings.append(f"{built_in.presidio_name}: both enabled and declined")
    return tuple(findings)


# ----------------------------------------------------------------- detection and scrubbing
def _overlaps(a: Detection, b: Detection) -> bool:
    return a.start < b.end and b.start < a.end


def _prefer(a: Detection, b: Detection) -> Detection:
    """The one to keep when two detections overlap: longer first, then more confident.

    Length before confidence, deliberately. Where an eight-digit UEN and a nine-digit one
    both match, keeping the confident short span leaves a digit and a check letter outside
    the redaction, and a partially redacted identifier is worse than a wrongly labelled
    whole one - the label is cosmetic, the leftover characters are not.
    """
    if (a.end - a.start) != (b.end - b.start):
        return a if (a.end - a.start) > (b.end - b.start) else b
    return a if a.confidence >= b.confidence else b


def detect(text: str) -> tuple[Detection, ...]:
    """Everything found, ordered by position, overlaps resolved.

    Returns detections and nothing else. There is no second return value saying whether the
    text is acceptable, because there is no question here whose answer is yes or no.
    """
    found: list[Detection] = []
    for recogniser in RECOGNISERS:
        for match in recogniser.pattern.finditer(text):
            confidence = recogniser.base_confidence
            if recogniser.confirm is not None and recogniser.confirm(match.group(0)):
                confidence = max(confidence, recogniser.confirmed_confidence)
            found.append(
                Detection(
                    kind=recogniser.kind,
                    start=match.start(),
                    end=match.end(),
                    confidence=confidence,
                )
            )

    kept: list[Detection] = []
    for candidate in sorted(found, key=lambda d: (d.start, -(d.end - d.start))):
        clash = next((k for k in kept if _overlaps(k, candidate)), None)
        if clash is None:
            kept.append(candidate)
            continue
        winner = _prefer(clash, candidate)
        if winner is not clash:
            kept[kept.index(clash)] = winner
    return tuple(sorted(kept, key=lambda d: d.start))


def scrub(text: str, detections: Sequence[Detection] | None = None) -> str:
    """The text with every detected span replaced by its kind.

    Replaced rather than removed. A removed span leaves two sentences joined into one that
    says something neither of them said, and a model downstream cannot tell that anything
    was there. `[sg_nric]` tells it something was, and what.
    """
    spans = tuple(detections) if detections is not None else detect(text)
    out: list[str] = []
    cursor = 0
    for span in sorted(spans, key=lambda d: d.start):
        if span.start < cursor:
            continue
        out.append(text[cursor : span.start])
        out.append(f"[{span.kind.value}]")
        cursor = span.end
    out.append(text[cursor:])
    return "".join(out)


# ----------------------------------------------------------------- cost
#: What a scrub may cost per kibibyte of text on the client's own hardware. Declared here
#: so that a measurement has something to fail against; the measurement itself is
#: M32.2.2.4 and has not been taken on their machine, so this number is a budget and not a
#: result, and saying otherwise would make a guess look like evidence.
BUDGET_MS_PER_KIB: Final = 2.0


def budget_breach(elapsed_ms: float, chars: int) -> str | None:
    """The overrun as a sentence, or None. Never raises: a slow scrub still scrubs.

    Turning a budget overrun into an exception would mean text that could not be scrubbed
    in time was sent unscrubbed or not at all, and both of those are worse than being slow.
    """
    if chars <= 0:
        return None
    kib = chars / 1024
    allowed = kib * BUDGET_MS_PER_KIB
    if elapsed_ms <= allowed:
        return None
    return (
        f"scrub took {elapsed_ms:.1f} ms for {chars} characters; "
        f"budget is {allowed:.1f} ms at {BUDGET_MS_PER_KIB} ms/KiB"
    )
