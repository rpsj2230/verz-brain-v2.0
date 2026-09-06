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
M32.2.1.2 exists; no pattern here claims them, and pretending otherwise would be the exact
failure this paragraph is about. What claims them is the model leg below, which is declared
here and has no weights behind it on any install today.

Over-redaction is the accepted cost throughout. An eight-digit number followed by a letter
is a UEN and is also an order reference; a capital letter, a full stop and a surname is an
initialled name and is also the end of one sentence before a proper noun. Both are
redacted. The asymmetry is not close: a missed identifier is in a third party's logs
permanently, and an over-redacted order number costs a little context in one prompt.

**The model leg finds more, which means it removes more, and the two ways that is wrong are
not symmetric.** M32.2.1.2 adds the names no pattern describes, and a detector that finds
more is a detector that takes more text out of a question before anybody answers it. A
missed identifier is in somebody else's log for ever and nobody sees it go. An
over-redacted question produces a worse answer and nobody sees that either, because the
answer that would have been given does not exist to compare against. Both failures are
invisible; only one of them is permanent.

So the direction is chosen twice rather than once, and differently in the two layers. The
deterministic layer errs towards redacting, as the paragraph above says, and does not
change. **The model layer errs the other way**, and `GlinerLabel.score_threshold` is where
that is written down. A regex over-redacts by a rule somebody can read and predict; a model
over-redacts an arbitrary span for a reason nobody can reconstruct afterwards, and a
scrubber that sometimes removes half a sentence is one people learn to work around, which
protects nothing at all. The model layer can afford to be the cautious one precisely
because it is not the floor: `detect_with_model` runs the deterministic recognisers first
and unconditionally, so a threshold set too high costs what the model would have added and
never what the patterns already found.

**A model span never displaces a deterministic one; it fills in around it.** This is the
overlap case M32.2.1.3 makes real rather than hypothetical, because an NRIC sits next to a
name in the sentences this runs on. Where a model span and a pattern span cover some of the
same characters, `merge_detections` keeps the pattern span exactly as it is and keeps only
the parts of the model span that fall outside it. The pattern wins because its extent comes
from a format with a fixed length and a lookaround on each side, and it is the leg that
still runs when the network is down; a model's extent is an opinion that moves by a
character between revisions, and `SERVED_MODELS` already refuses a quantised copy of these
weights for exactly that reason. Rejected: letting the longer span win, which is what
`_prefer` does inside one leg. A model span reading "Tan Wei Ling S1234567D" as one person
would then take the label off a checksummed identifier, and one identifier would be counted
as a name in the telemetry that M32.2.2.3 is about.

**No resolution here may lose a character, and that is a stronger claim than it sounds.**
Deciding an overlap by keeping one span and dropping the other silently loses whatever the
loser reached and the winner does not, which is the half-redacted name again by a different
route. It was reachable here before this was written, with nothing to do with any model:
"A. R. Rahman a/l Segaran" matches an initialled name from 0 and a patronymic from 6, the
patronymic is longer so it won, and the scrub emitted "A. R. [person_name_patronymic]" with
the initials still in it. So `_resolve` walks the findings in order and gives each one the
characters that are left rather than choosing between them. It tiles what was covered
instead of selecting from it, `_prefer` decides the label of a shared run and no longer
decides its extent, and the covered set of a resolution is now exactly the union of what
went in. It reads slightly worse in the two-labels case and it cannot leak.

**What was measured, on what, and what was not.** M32.2.2.4 asks for a budget measured on
the client's CPU. That machine is not reachable from here and inventing a figure for it
would be worse than having none, so what exists is the harness and a recorded absence:
`measure_scrub` times the real scrubber against a clock the caller supplies,
`SCRUB_COST_ON_THE_BUILD_MACHINE` records one run of it with the machine named and the date
on it, and `budget_gaps` reports for as long as it is true that nothing has been timed on
the machine this will be installed on. What that run covers is the whole of `scrub` on this
laptop and nothing else. What it excludes is every part of the cost that is not this
process: the GLiNER leg is a network call to another container and is bounded by a timeout
rather than by a rate per kibibyte, and folding it into the same figure would produce a
budget that is mostly somebody else's scheduler. See
`THE_BUDGET_IS_NOT_MEASURED_ON_THE_MACHINE_IT_IS_ABOUT`.

**The measurement found something, which is the point of taking one.** Overlap resolution
compared each candidate against every span kept so far, so its cost grew with the square of
the number of findings while the budget it is checked against is a rate per kibibyte. Plain
prose never showed it, because plain prose has almost no findings in it: the rate there is
flat at about 0.5 ms/KiB from 336 characters to 43008. `BENCHMARK_PARAGRAPH` did. Timed on
one machine in one sitting, the old resolution cost 0.26 ms/KiB over 1.2 KiB, 1.65 over 26
KiB and 4.13 over 100 KiB, so it crossed the 2.0 budget somewhere between 26 KiB and 64 KiB
of identifier-dense text. The sweep that replaced it is one pass, and the same four sizes
cost 0.24, 0.71, 0.69 and 0.67, which is flat with length and is what a rate per kibibyte
was assuming all along. A budget nobody measures is a budget that holds until the day
somebody pastes a long document into a question.

**GLiNER is not a dependency of this project and must not become one.** It is not in
`pyproject.toml` and not in `uv.lock`, and adding it there would undo the decision it exists
under. Item 31 put entity recognition behind the inference server precisely so that the
machine-learning stack lives in one image instead of in every container: `gliner` pulls
torch and transformers, which `brain.ops.inference` measured at about 1.5 GB installed, into
containers budgeted at 512 MiB, and `sweep_dependencies` would then be checking the licences
of that whole tree on behalf of a service that does not run here. So what this module holds
is the half that has no weights in it - which labels are asked for, how sure is sure enough,
what a response may say, and how its spans merge with the deterministic ones - and the model
itself stays behind `InferenceTask.ENTITY_RECOGNITION` in an image that has never been built.

**What has no caller, stated plainly rather than implied.** Nothing in this module is called
from anywhere in `src` today, and that was already true before this commit: `detect` and
`scrub` are reached only from tests, and the one place in this system that masks text before
it leaves the process is `brain.ops.tracing`, which argues at length that a detector is the
wrong tool for a trace payload and uses an allowlist instead. What is new here does not
change that. `entity_request` and `decode_entity_spans` are the two halves of a wire
contract with nothing on the other end of it, in the same state as
`brain.ops.inference.embedding_request` and for the same reason: there is no image, and a
client for this leg is deliberately not written here rather than being a fourth copy of the
post-and-classify loop in `brain.ops.inference_client` pointed at a service that does not
exist. `budget_gaps` is called by tests only.

Task ids: M32.2.1.1, M32.2.1.2, M32.2.1.3, M32.2.1.4, M32.2.2.1, M32.2.2.2, M32.2.2.3

Not claimed: M32.2.2.4. The budget is declared, the harness exists and one measurement has
been taken, and the leaf asks for a measurement on the client's CPU, which has not happened
and cannot happen from here. `budget_gaps` says so on every call.
"""

from __future__ import annotations

import bisect
import enum
import functools
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Final

#: Written down where a reader of the code meets it, not only in the docstring. This
#: string is asserted by the test suite, so deleting the rule deletes a test.
NEVER_AN_AUTHORISATION_BOUNDARY: Final = (
    "Detection is telemetry. Nothing in this module decides what a principal may see; "
    "that is brain.core.entitlement and brain.core.redaction, which work from grants "
    "rather than from what a string resembles."
)

#: Why a probabilistic detector may add to a scrub and may never subtract from one.
A_MODEL_MAY_ONLY_EVER_ADD_TO_A_SCRUB: Final = (
    "The deterministic recognisers are the floor and the model is the layer above them. "
    "detect_with_model runs the patterns first and unconditionally, merge_detections keeps "
    "every pattern span at its own extent, and a model span contributes only the characters "
    "no pattern already covers. Three things follow and each of them is the reason for one "
    "of those. A model that is absent, unreachable, differently weighted or badly prompted "
    "produces a thinner scrub and never an unscrubbed string, so the leg that needs no "
    "network is the leg the guarantee rests on. A model can never take the label off a "
    "checksummed identifier, so a count of sg_nric detections stays a count of things shaped "
    "like an NRIC rather than becoming a report about how a model felt that week. And no "
    "resolution anywhere in this module drops a character that something covered, because "
    "the loser of an overlap keeps whatever the winner does not reach."
)

#: What the per-kibibyte budget has behind it, and what it does not.
THE_BUDGET_IS_NOT_MEASURED_ON_THE_MACHINE_IT_IS_ABOUT: Final = (
    "M32.2.2.4 asks for a performance budget measured on the client CPU. This system is "
    "single-tenant and client-hosted, so that machine is one nobody here has, and no figure "
    "taken anywhere else is a measurement of it. What has been taken is one run of "
    "measure_scrub over the whole of scrub on the build machine, which is recorded with the "
    "processor named and the date on it and is not the same claim. What has not been taken "
    "is anything at all on the target hardware, and anything at all about the GLiNER leg, "
    "which is a call to another container and is bounded by a timeout rather than by a rate "
    "per kibibyte. budget_gaps reports both absences on every call and will keep reporting "
    "the first of them until somebody runs the harness where the software is installed."
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
    #: A name with no written form a pattern can describe: "Tan Wei Ling". Only the model
    #: produces this one, which means it is the one kind that disappears when the inference
    #: server is unreachable. The value is the bare `person_name` rather than something
    #: naming the model or claiming a form, because the label is what a model downstream
    #: reads in place of the value and neither of those is true about the person.
    UNPATTERNED_NAME = "person_name"


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


# ----------------------------------------------------------------- GLiNER configuration
class PiiError(Exception):
    """A response from the entity model cannot be read, so nothing is taken from it.

    Deliberately not one of `brain.core.errors`, and the reason is the boundary this module
    opens by refusing. Those five are outcomes a person is eventually told about, and a
    scrub that could not use a model's answer is not an outcome anybody is told about: the
    deterministic recognisers already ran, the text is still scrubbed, and what was lost is
    the extra the model would have added. A caller that catches this and falls back to
    `detect` has degraded correctly, which is the same shape
    `brain.knowledge.embedding` uses when the vector leg is unavailable and the lexical leg
    is not.

    Raising rather than silently dropping the bad spans, because the two failures need
    different people. A malformed response is the far side and this repository disagreeing
    about a contract, which somebody has to read; a span below its threshold is the model
    being unsure, which is ordinary and is dropped without a word.
    """


@dataclass(frozen=True)
class GlinerLabel:
    """One label the entity model is asked for, and why nothing cheaper can find it.

    `why_the_default_recognisers_miss_it` is required prose, in the shape
    `BuiltIn.why_not_local` uses and answering a harder question. A built-in has to justify
    not being a regex here. A label has to justify not being a regex *and* not being one of
    the built-ins, because the leaf this serves is named for exactly that: entities the
    default recognisers miss. A label that Presidio already finds is a second network call
    for an answer somebody already has.

    `score_threshold` is the whole of this module's caution about the model layer. See the
    module docstring: a regex over-redacts predictably and a model over-redacts arbitrarily,
    so the threshold sits where a wrong span costs a reader more than a missed one, which is
    the opposite of the direction the deterministic layer errs in and is only safe because
    that layer is still underneath.
    """

    label: str
    kind: EntityKind
    score_threshold: float
    why_the_default_recognisers_miss_it: str

    def __post_init__(self) -> None:
        if not self.label.strip():
            msg = "a label with no text asks the model for nothing and matches everything"
            raise ValueError(msg)
        if not 0.0 < self.score_threshold <= 1.0:
            msg = f"{self.label} has threshold {self.score_threshold}, which is not a confidence"
            raise ValueError(msg)
        if not self.why_the_default_recognisers_miss_it.strip():
            msg = f"{self.label} does not say what the default recognisers miss about it"
            raise ValueError(msg)


#: Which model answers the entity leg. The same string as
#: `brain.ops.inference.served_model(InferenceTask.ENTITY_RECOGNITION).name`, and held equal
#: to it by test rather than imported from it: this module is the one that must still work
#: with no network and nothing loaded, and reaching into `brain.ops.inference` for a name
#: would pull `brain.knowledge.embed_queue`, `brain.knowledge.embedding` and
#: `brain.ops.wiring` into the import graph of a scrubber whose whole argument is that the
#: leg that must never fail has no dependency that can.
GLINER_MODEL_NAME: Final = "gliner"

#: One label, and the count is the argument rather than an accident. `brain.ops.inference`
#: sized this model on the strength of one sentence in this file: romanised names with no
#: connector are not detectable by pattern. Everything else a general entity model will
#: happily label is declined below, because a model asked for more labels returns more spans
#: and each extra one is a span of a question removed for a reason nobody can read back.
GLINER_LABELS: Final[tuple[GlinerLabel, ...]] = (
    GlinerLabel(
        label="person",
        kind=EntityKind.UNPATTERNED_NAME,
        # The same figure `PRESIDIO_BUILT_INS` uses for `PERSON`, and held equal to it by
        # test. Two detectors answering one question at two thresholds means the same name
        # is redacted or not depending on which leg happened to see it, and the rate that
        # M32.2.2.3 watches then moves whenever either is redeployed.
        score_threshold=0.6,
        why_the_default_recognisers_miss_it=(
            "a romanised Chinese name is three ordinary capitalised words - 'Tan Wei Ling' - "
            "with no connector, no initial and no script to key on, so there is no pattern to "
            "write that does not also match the start of every sentence naming a product. "
            "Presidio's English NER is the other thing that would find it and does not "
            "reliably: it is trained on Western newswire, and the failure it produces on these "
            "names is the one this module spends a paragraph on, which is recognising the last "
            "token and reporting success. Both are enabled and both feed the same merge, "
            "deliberately, because they miss different names and merge_detections cannot lose "
            "what either of them found"
        ),
    ),
)

#: Labels a general entity model would answer and that are deliberately not asked for.
#: Recorded for the reason `PRESIDIO_DECLINED` is: the next reader cannot tell a decision
#: from an oversight, and the cheapest edit in the world is adding one more label.
GLINER_DECLINED: Final[dict[str, str]] = {
    "organisation": (
        "every ticket, contract and thread in this system names the client it is about; "
        "redacting that removes the thing the answer is about, which is the argument "
        "PRESIDIO_DECLINED already makes about DATE_TIME"
    ),
    "address": (
        "a client's address is governed by the field policy in brain.core.redaction, which "
        "works from grants; a text scrubber deciding it is the same refusal as LOCATION"
    ),
    "job title": (
        "a role identifies nobody on its own and is most of the vocabulary a question about "
        "a company is asked in; 'what did the finance manager approve' would not survive it"
    ),
    "passport number, bank account number": (
        "an identifier with no fixed form is the case a model answers by naming whichever "
        "reference number is nearest, and a reference number taken out of a question is the "
        "context the answer needed. The local formats that do have a shape are already "
        "patterns here, and a wrong span costs more than the ones they miss"
    ),
}


def gliner_label_for(label: str) -> GlinerLabel:
    """The declared label by name, or a refusal naming the ones that exist.

    Refuses rather than returning None, matching `brain.ops.inference.served_model`. A
    caller handed None writes `if declared is None: continue` and a label the model answered
    with that nobody asked for is silently accepted, which is precisely the closed-vocabulary
    rule `EntityKind` exists for.
    """
    for declared in GLINER_LABELS:
        if declared.label == label:
            return declared
    msg = f"no declared label {label!r}; asked for: {[d.label for d in GLINER_LABELS]}"
    raise PiiError(msg)


def configuration_gaps() -> tuple[str, ...]:
    """Every entity kind with nowhere to be found, and every duplicated responsibility.

    Two directions, because both have happened elsewhere in this codebase. A kind with no
    recogniser is a label that can never be produced, so a caller checking for it waits
    forever. A kind covered both locally and by a built-in is two answers to one question,
    and the loser is whichever one the merge happens to drop.

    The model's labels count as somewhere for a kind to come from, which is what lets
    `UNPATTERNED_NAME` exist with no regex behind it. That is a real weakening of the check
    and it is taken knowingly: it is the one kind that stops being produced when the
    inference server is unreachable, and the thing standing behind it is
    `A_MODEL_MAY_ONLY_EVER_ADD_TO_A_SCRUB` rather than this function.

    Presidio's `PERSON` and the model's `person` are not reported as a duplicate, and that
    is a decision rather than a gap in the check. They are two models that miss different
    names, both of their spans go through `merge_detections`, and a merge that cannot lose a
    character makes two overlapping answers additive instead of a race. The duplication this
    reports is between a *pattern* and a model, where one of the two is genuinely dead code.
    """
    findings: list[str] = []
    model_kinds = {declared.kind for declared in GLINER_LABELS}
    have = {r.kind for r in RECOGNISERS} | _KINDS_WITHOUT_OWN_RECOGNISER | model_kinds
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

    seen: set[str] = set()
    pattern_kinds = {r.kind for r in RECOGNISERS}
    for declared in GLINER_LABELS:
        if declared.label in seen:
            findings.append(
                f"{declared.label}: asked for twice, so its spans arrive twice and one "
                "threshold silently governs both"
            )
        seen.add(declared.label)
        if declared.label in GLINER_DECLINED:
            findings.append(f"{declared.label}: both asked for and declined")
        if declared.kind in pattern_kinds:
            findings.append(
                f"{declared.label}: produces {declared.kind.value}, which a local pattern "
                "already produces; the model call buys nothing a regex has not already found"
            )
    return tuple(findings)


# ----------------------------------------------------------------- detection and scrubbing
def _prefer(a: Detection, b: Detection) -> Detection:
    """The one whose label wins where two detections start together: longer, then surer.

    Length before confidence, deliberately. Where an eight-digit UEN and a nine-digit one
    both match, keeping the confident short span leaves a digit and a check letter outside
    the redaction, and a partially redacted identifier is worse than a wrongly labelled
    whole one - the label is cosmetic, the leftover characters are not.

    It decides a label and no longer decides coverage, which is the change `_resolve`
    describes. Under the old rule the loser vanished, so preferring a longer span that
    started later dropped whatever the shorter one covered before it.
    """
    if (a.end - a.start) != (b.end - b.start):
        return a if (a.end - a.start) > (b.end - b.start) else b
    return a if a.confidence >= b.confidence else b


def _order(a: Detection, b: Detection) -> int:
    """Sweep order: earlier start first, and `_prefer` decides a shared start.

    A comparison function rather than a sort key so that `_prefer` stays the one place the
    tie is broken. Spelling the same rule a second time as `(start, -length, -confidence)`
    would make reversing `_prefer` a change that breaks nothing, which is exactly what the
    test written about that ordering exists to catch.
    """
    if a.start != b.start:
        return -1 if a.start < b.start else 1
    return -1 if _prefer(a, b) is a else 1


def _clip(span: Detection, start: int, end: int) -> Detection:
    """The same finding narrowed to a shorter range, keeping its kind and its confidence."""
    return Detection(kind=span.kind, start=start, end=end, confidence=span.confidence)


def _covered(spans: Iterable[Detection]) -> tuple[tuple[int, int], ...]:
    """The character ranges these spans reach, merged, in order. Touching ranges are one."""
    merged: list[list[int]] = []
    for start, end in sorted((s.start, s.end) for s in spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return tuple((start, end) for start, end in merged)


def _outside(
    span: Detection, covered: Sequence[tuple[int, int]], ends: Sequence[int]
) -> Iterable[Detection]:
    """The parts of `span` that `covered` does not already reach, in order.

    `ends` is the covered ranges' end offsets, passed in rather than rebuilt per span so
    that a caller merging many spans against one floor does not walk the floor once per
    span. The bisect is why: the first range that can matter is the first whose end is past
    this span's start, and everything before it is behind us for every later span too.
    """
    cursor = span.start
    index = bisect.bisect_right(ends, cursor)
    while index < len(covered) and covered[index][0] < span.end:
        block_start, block_end = covered[index]
        if block_start > cursor:
            yield _clip(span, cursor, block_start)
        cursor = max(cursor, block_end)
        if cursor >= span.end:
            return
        index += 1
    if cursor < span.end:
        yield _clip(span, cursor, span.end)


def _resolve(found: Iterable[Detection]) -> tuple[Detection, ...]:
    """Overlapping findings turned into a tiling of the characters they cover.

    **It resolves labels and never coverage**, which is the property the old rule did not
    have. That one kept the preferred span and dropped the other outright, so two spans that
    overlapped in part rather than nesting lost whatever the loser reached and the winner did
    not. It was reachable: "A. R. Rahman a/l Segaran" is an initialled name from 0 and a
    patronymic from 6, the patronymic is longer, and the scrub emitted the initials followed
    by one redaction. A half-redacted name is the failure this module is mostly about, so the
    resolution now walks the findings in order and gives each one whatever characters are
    left rather than choosing between them. Nothing that any recogniser reached comes out
    uncovered, and the label of a shared run is `_prefer`'s.

    **And it is a single pass rather than a search**, which is not a tidying. The rule it
    replaces compared each candidate against every span kept so far, so its cost grew with
    the square of the number of findings while the budget it is checked against is a rate per
    kibibyte. On identifier-dense text that stayed inside the budget to about 25 KiB and then
    left it. See `THE_BUDGET_IS_NOT_MEASURED_ON_THE_MACHINE_IT_IS_ABOUT` for what was
    measured and `budget_gaps` for what still has not been.
    """
    kept: list[Detection] = []
    cursor = 0
    for candidate in sorted(found, key=functools.cmp_to_key(_order)):
        start = max(candidate.start, cursor)
        if candidate.end <= start:
            continue
        kept.append(
            candidate if start == candidate.start else _clip(candidate, start, candidate.end)
        )
        cursor = candidate.end
    return tuple(kept)


def detect(text: str) -> tuple[Detection, ...]:
    """Everything the patterns found, ordered by position, overlaps resolved.

    Returns detections and nothing else. There is no second return value saying whether the
    text is acceptable, because there is no question here whose answer is yes or no.

    The deterministic leg on its own, and the floor everything else is added to. It reads no
    configuration, opens nothing and cannot fail for a reason outside this process, which is
    what lets `A_MODEL_MAY_ONLY_EVER_ADD_TO_A_SCRUB` be a guarantee rather than a hope.
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
    return _resolve(found)


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


# ----------------------------------------------------------------- the model leg (M32.2.1.2)
#: The keys one entity request carries, and the whole of them. Asserted by test rather than
#: left to review, in the shape `brain.ops.inference.REQUEST_KEYS` is: a request that cannot
#: grow a key cannot grow one naming a department, an owner or a visibility, and that is the
#: structural half of this module never being an authorisation boundary. It is worth saying
#: which direction that protects. The text sent here is not the egress this module guards:
#: the inference server is ours, on an internal network with no route off the host, and it
#: is handed the text precisely so that what leaves afterwards is scrubbed. What must not
#: happen is the request growing a field the far side could answer a permission question
#: from, because then a detector would be sitting where the entitlement layer sits.
ENTITY_REQUEST_KEYS: Final[tuple[str, ...]] = ("model", "labels", "text")

#: Where the spans live in a response, and what one span states.
ENTITY_SPANS_KEY: Final = "spans"
ENTITY_SPAN_KEYS: Final[tuple[str, ...]] = ("label", "start", "end", "score")


def entity_request(text: str) -> Mapping[str, object]:
    """What one piece of text looks like on the way to the entity model. Read-only.

    `MappingProxyType` for the reason `embedding_request` uses one: a mapping handed out of
    a builder is a mapping somebody mutates, and the mutation to guard against is the one
    that adds a scope "so the server can filter". `brain.ops.inference_client.jsonable` is
    what turns it into something `json.dumps` will take, at the last moment, in the module
    that owns a socket.

    The labels are sent rather than assumed because that is how this model is asked
    anything: GLiNER takes the entity types in the request, so the declared set is the
    prompt. `decode_entity_spans` refuses a label that was not in it, which is what keeps
    `EntityKind` closed across a network.
    """
    return MappingProxyType(
        {
            "model": GLINER_MODEL_NAME,
            "labels": tuple(declared.label for declared in GLINER_LABELS),
            "text": text,
        }
    )


def _offset(span: Mapping[str, object], key: str, text: str) -> int:
    """One end of a span, refused for everything that is not an offset into this text.

    Offsets are checked against the text rather than trusted, and this is the one place the
    checking matters more than it looks. A span one character out is the half-redacted name
    the module docstring is mostly about; a span past the end of the text is a response
    computed against a different string, which means the far side and this process disagree
    about what was sent, and every other span in that answer is then suspect too.
    """
    value = span.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"a span gives {value!r} as its {key}, which is not an offset"
        raise PiiError(msg)
    if not 0 <= value <= len(text):
        msg = (
            f"a span gives {value} as its {key} in {len(text)} characters of text; the "
            "answer was computed against a different string from the one that was sent"
        )
        raise PiiError(msg)
    return value


def decode_entity_spans(text: str, payload: Mapping[str, object]) -> tuple[Detection, ...]:
    """One response from the entity model, read into detections. Refuses rather than guesses.

    Refusal and silence are separated deliberately, because the two need different people.
    A span scoring below its declared threshold is the model being unsure, which is ordinary
    and is dropped without a word. A malformed span, an offset that is not one, or a label
    nobody asked for is this repository and the far side disagreeing about a contract, and
    that is raised. The caller's fallback for a raise is `detect`, which is why raising here
    is safe: see `A_MODEL_MAY_ONLY_EVER_ADD_TO_A_SCRUB`.

    **A label that was not asked for is refused rather than ignored**, which is the closed
    vocabulary of `EntityKind` surviving a network. The label becomes the token a downstream
    model reads in place of a value, so an unknown one is either a token nobody chose or a
    span dropped in silence, and the second is the quieter of two bad answers.

    Rejected: reading the model's identity back off the response and checking it, which is
    what `brain.ops.inference.decode_embeddings` does and is right to do. The reason it does
    is that a vector from the wrong weights is written to a corpus under a stable name and
    poisons every later query. Nothing here writes a row. A response from the wrong weights
    degrades one scrub, which the merge cannot turn into an unscrubbed one, so a check with
    no failure worth naming would be a check somebody trusts for more than it does.
    """
    raw = payload.get(ENTITY_SPANS_KEY)
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        msg = (
            f"the response carries no {ENTITY_SPANS_KEY!r} list; an answer that cannot be "
            "read is not an answer that found nothing, and only one of those two may be "
            "recorded as a text the model had no names in"
        )
        raise PiiError(msg)

    found: list[Detection] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            msg = f"the response holds {entry!r} where one span was due"
            raise PiiError(msg)
        missing = [key for key in ENTITY_SPAN_KEYS if key not in entry]
        if missing:
            msg = f"a span states {missing} nowhere, so there is no way to place or weigh it"
            raise PiiError(msg)
        label = entry["label"]
        if not isinstance(label, str):
            msg = f"a span gives {label!r} as its label, which is not one"
            raise PiiError(msg)
        declared = gliner_label_for(label)
        start = _offset(entry, "start", text)
        end = _offset(entry, "end", text)
        if end <= start:
            msg = f"a {label!r} span runs {start}..{end}, which covers no characters"
            raise PiiError(msg)
        score = entry["score"]
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            msg = f"a {label!r} span gives {score!r} as its score, which is not one"
            raise PiiError(msg)
        confidence = float(score)
        if not 0.0 < confidence <= 1.0:
            msg = f"a {label!r} span scores {confidence}, which is not a confidence"
            raise PiiError(msg)
        if confidence < declared.score_threshold:
            continue
        found.append(Detection(kind=declared.kind, start=start, end=end, confidence=confidence))
    return _resolve(found)


def merge_detections(
    deterministic: Sequence[Detection], model: Sequence[Detection]
) -> tuple[Detection, ...]:
    """The patterns' findings, kept whole, plus whatever the model found outside them.

    **Not a competition between two answers**, which is the shape `_resolve` has and this one
    deliberately does not. Every deterministic span comes through at its own extent with its
    own label, and a model span contributes only the characters no pattern already reached.
    The reason is in the module docstring: a pattern's extent is a format with a fixed length
    and a lookaround on each side, a model's extent moves between revisions, and a model that
    could take the label off a checksummed identifier would make a count of `sg_nric`
    detections a report about the model rather than about the text.

    It cannot lose a character in either direction. The floor is kept whole, the model's
    contribution is what a subtraction leaves, and neither step drops a covered offset. So
    the covered set of the result is the union of the covered sets that went in, which is
    what makes "a detector that finds more redacts more" a property rather than a hope.

    Both arguments are resolved before anything is subtracted, so that a caller handing in
    two overlapping model spans, or a deterministic sequence somebody assembled by hand, gets
    a well-formed answer rather than a scrub with two labels over one run of characters.
    """
    floor = _resolve(deterministic)
    covered = _covered(floor)
    ends = [block[1] for block in covered]
    residue = [piece for span in _resolve(model) for piece in _outside(span, covered, ends)]
    return tuple(sorted([*floor, *residue], key=lambda d: d.start))


def detect_with_model(
    text: str, payload: Mapping[str, object] | None = None
) -> tuple[Detection, ...]:
    """Everything found, with the model's answer folded in when there is one.

    `None` is the ordinary case rather than an error case, and it is the whole of how this
    module degrades. The inference server is absent on every install today, unreachable
    whenever it restarts, and skipped entirely on a profile that deploys none; each of those
    arrives here as no payload, and each produces the deterministic scrub. There is no branch
    in which the model's absence produces fewer detections than `detect` alone, because the
    floor is computed first and unconditionally.

    Returns detections and nothing else, exactly as `detect` does. A second return value
    saying whether the model leg ran would be the first thing a caller read as a verdict
    about the text, and this module has no verdict to give.
    """
    deterministic = detect(text)
    if payload is None:
        return deterministic
    return merge_detections(deterministic, decode_entity_spans(text, payload))


# ----------------------------------------------------------------- cost (M32.2.2.4)
#: What a scrub may cost per kibibyte of text on the client's own hardware. Declared here
#: so that a measurement has something to fail against; the measurement itself is
#: M32.2.2.4 and has not been taken on their machine, so this number is a budget and not a
#: result, and saying otherwise would make a guess look like evidence.
BUDGET_MS_PER_KIB: Final = 2.0

#: Which point of the distribution a cost is quoted at. The ninety-fifth rather than the
#: mean, because a scrub happens once per outbound request and the request that waits is the
#: one somebody notices; a mean hides exactly the tail a budget exists to bound.
SCRUB_PERCENTILE: Final = 0.95

#: The fewest timings a figure at that percentile may be computed from. **Derived rather
#: than chosen**, in the shape `brain.knowledge.quality.MINIMUM_JUDGED_CASES` is: at nearest
#: rank the ninety-fifth percentile of nineteen samples is the nineteenth of them, which is
#: the maximum wearing a percentile's name, and a maximum over a handful of runs is a
#: measurement of whatever else the machine was doing at the time.
MINIMUM_TIMED_SAMPLES: Final = math.ceil(1 / (1 - SCRUB_PERCENTILE))

#: The text a timing run is taken over, repeated to the size asked for. Fixed here rather
#: than built at the call site because a measurement over text nobody can reproduce is not a
#: measurement, and the cost of this scrubber depends on what is in the text rather than only
#: on how much of it there is: prose with nothing identifying in it costs about a third of
#: what this costs, so timing against prose would produce a comfortable figure about a case
#: that is not the one the budget is for. Every identifier below is synthetic and several of
#: them are the worked examples the test suite already uses.
BENCHMARK_PARAGRAPH: Final = (
    "Following up on ticket 44821 for Nur Aisyah binti Abdullah, NRIC S1234567D, on "
    "9123 4567 or nur.aisyah@example.com.sg. The entity is UEN 201512345K and the older "
    "reference is 53112233B. R. Kumar in accounts and Tan Wei Ling both reviewed it. "
    "Please confirm by Monday whether the credit was raised against the correct company, "
    "and copy Ravi s/o Muthusamy on the reply so that the thread stays in one place. "
)


def benchmark_text(chars: int) -> str:
    """`BENCHMARK_PARAGRAPH` repeated until it is at least this long, never truncated.

    Never truncated because a cut through an identifier changes how many things are found,
    and the count of findings is the thing the resolution's cost used to scale with. A
    benchmark whose input shape depends on the size asked for cannot be compared across
    sizes, which is the comparison that found the problem in the first place.
    """
    if chars < 1:
        msg = f"a benchmark over {chars} characters times nothing"
        raise ValueError(msg)
    repeats = math.ceil(chars / len(BENCHMARK_PARAGRAPH))
    return BENCHMARK_PARAGRAPH * repeats


def _percentile(samples: Sequence[float], fraction: float) -> float:
    """Nearest rank, never interpolated.

    Restated rather than imported from `brain.knowledge.quality.percentile_ms`, which is
    itself restated from `brain.connectors.throttle`, and for the reason given there: this
    module is the one that has to work with nothing loaded and no network, and reaching into
    the knowledge package for four lines of arithmetic would put a dependency in it.

    Interpolating between two samples returns a duration nobody waited, and a budget is a
    claim about what happened rather than about what the numbers average to.
    """
    ordered = sorted(samples)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


@dataclass(frozen=True)
class ScrubCost:
    """One timing run: what it cost, on what machine, over what, and what it left out.

    Three pieces of required prose for the three questions a number like this has to answer
    before anybody may act on it, and `ServedModel.sizing_basis` is the precedent for making
    them fields rather than a comment. `hardware` is the machine, because a rate per kibibyte
    is a claim about a processor. `basis` is what was timed and over what text. `excludes` is
    what the figure does not cover, which is the field that stops it being read as the cost
    of a scrub rather than the cost of this half of one.

    `on_the_client_cpu` is separate from all three and defaults to False, in the shape
    `ServedModel.measured` uses: saying that a figure came from the machine the software will
    run on has to be a deliberate edit rather than a sentence somebody wrote loosely, because
    that flag is the whole of the difference between M32.2.2.4 being done and not.
    """

    taken_on: date
    hardware: str
    basis: str
    excludes: str
    chars: int
    samples: int
    ms_per_kib: float
    on_the_client_cpu: bool = False

    def __post_init__(self) -> None:
        for name in ("hardware", "basis", "excludes"):
            if not str(getattr(self, name)).strip():
                msg = f"a timing run states its {name}; without it the figure cannot be checked"
                raise ValueError(msg)
        if self.chars < 1:
            msg = "a timing run over no characters has no rate per kibibyte in it"
            raise ValueError(msg)
        if self.samples < MINIMUM_TIMED_SAMPLES:
            msg = (
                f"{self.samples} sample(s) is below {MINIMUM_TIMED_SAMPLES}, at which the "
                f"{SCRUB_PERCENTILE} percentile at nearest rank is the maximum by another name"
            )
            raise ValueError(msg)
        if self.ms_per_kib <= 0.0:
            msg = (
                f"a scrub measured at {self.ms_per_kib} ms/KiB is a clock that did not tick "
                "rather than work that did not happen; report the clock, not the scrubber"
            )
            raise ValueError(msg)


def measure_scrub(
    text: str,
    *,
    clock: Callable[[], float],
    hardware: str,
    basis: str,
    excludes: str,
    taken_on: date,
    samples: int = MINIMUM_TIMED_SAMPLES,
    on_the_client_cpu: bool = False,
) -> ScrubCost:
    """Time the real scrubber over this text and report what it cost per kibibyte.

    **The clock is a required parameter and this module never reads one.** Everything else
    here is arithmetic over patterns, and the split the layout table describes is what keeps
    it testable: a harness that called `time.perf_counter` itself could only be checked by
    running it and hoping, and the arithmetic it does with the readings could not be checked
    at all. A caller passes `time.perf_counter`; a test passes a clock that returns a
    prepared sequence and can then assert that a percentile is a percentile.

    Seconds in, milliseconds out, because `perf_counter` is the clock this is written for and
    a budget in milliseconds is the unit the number is compared in.

    It measures `scrub` rather than `detect`, which is the whole of what an outbound request
    pays: the replacement walk is short but it is not free, and a budget over half of a
    function is a budget somebody will breach with the other half.
    """
    timings: list[float] = []
    for _ in range(max(samples, 0)):
        started = clock()
        scrub(text)
        elapsed = (clock() - started) * 1000.0
        if elapsed < 0.0:
            msg = f"the clock went backwards by {-elapsed} ms, so nothing here was timed"
            raise ValueError(msg)
        timings.append(elapsed)
    if not timings:
        msg = "a timing run with no samples in it measured nothing"
        raise ValueError(msg)
    return ScrubCost(
        taken_on=taken_on,
        hardware=hardware,
        basis=basis,
        excludes=excludes,
        chars=len(text),
        samples=len(timings),
        ms_per_kib=_percentile(timings, SCRUB_PERCENTILE) / (len(text) / 1024),
        on_the_client_cpu=on_the_client_cpu,
    )


#: The one run that has been taken, recorded so that `BUDGET_MS_PER_KIB` has something
#: behind it rather than a paragraph. Produced by `measure_scrub` and copied here rather
#: than recomputed at import, because a constant that re-times itself every time anything
#: imports this module would put a benchmark in the path of an application starting.
#:
#: **It is not what M32.2.2.4 asks for and is not being passed off as it.** The leaf asks for
#: the client CPU; this is the machine the code was written on. `budget_gaps` says so on
#: every call and will keep saying it until somebody runs `measure_scrub` where the software
#: is installed and edits this record.
SCRUB_COST_ON_THE_BUILD_MACHINE: Final = ScrubCost(
    taken_on=date(2026, 9, 7),
    hardware=(
        "AMD Ryzen 7 6800U, 8 cores and 16 threads at a 2.7 GHz base, Windows 11, CPython "
        "3.13.15 on an unloaded laptop running on mains power. A mobile part with a boost "
        "range this wide is not a server: a figure from it is indicative and the same code "
        "on a throttled or shared host will be slower"
    ),
    basis=(
        "measure_scrub over benchmark_text(65536), which is BENCHMARK_PARAGRAPH repeated to "
        "65772 characters of identifier-dense text holding 1296 findings, timed with "
        "time.perf_counter. The figure is the worst of nine runs of 200 samples, which "
        "ranged from 0.67 to 0.70 with one faster outlier at 0.30. 64 KiB is far above "
        "anything this system scrubs in one go and is the size chosen for that reason: the "
        "cost per kibibyte is flat with length since the resolution became a single pass, so "
        "a figure taken where it used to be worst is the one that will not turn out to be "
        "optimistic"
    ),
    excludes=(
        "everything that is not this process. The GLiNER leg is a call to another container "
        "and is bounded by a timeout rather than by a rate per kibibyte, and there is no "
        "server to call, so nothing about it is in this figure. Neither is the cost of "
        "whatever produced the text, nor any Presidio call, which is also a container that "
        "does not run here"
    ),
    chars=65772,
    samples=200,
    ms_per_kib=0.70,
)


def budget_gaps(
    cost: ScrubCost | None = SCRUB_COST_ON_THE_BUILD_MACHINE,
    *,
    budget_ms_per_kib: float = BUDGET_MS_PER_KIB,
) -> tuple[str, ...]:
    """Every reason `BUDGET_MS_PER_KIB` is not yet a measured figure, in words.

    Both arguments have defaults so that the check can be run against something other than
    the two constants beside it, which is the argument `brain.ops.inference.weights_mib`
    makes about its own parameter: a check that can only ever be run against the values it
    sits next to cannot be shown to fail, and a check nobody has seen fail is a check nobody
    knows works.

    **It does not return empty today and is not meant to.** The second finding below is true
    for as long as nobody has run the harness on the machine the software is installed on,
    which is the honest state of M32.2.2.4 and the reason this leaf is not claimed. It is
    kept out of `configuration_gaps`, which must be empty, precisely so that a permanent and
    truthful finding cannot be tidied away by somebody making a different check pass.

    Nothing calls this outside the test suite. Its natural caller is a printed note on a
    sweep, in the shape `sweep_traceability` prints the leaves that have no test, so that an
    unmeasured budget is visible on every run rather than only to whoever opens this file.
    """
    findings: list[str] = []
    if cost is None:
        findings.append(
            f"nothing has been timed at all, so {budget_ms_per_kib} ms/KiB is a number with "
            "an argument and no evidence; run measure_scrub and record what it says"
        )
        return tuple(findings)
    if not cost.on_the_client_cpu:
        findings.append(
            f"the budget was measured on {cost.hardware.split(',')[0]} and not on the machine "
            "this is installed on, which is what M32.2.2.4 asks for; this system is "
            "single-tenant and client-hosted, so no figure taken anywhere else is a "
            "measurement of it"
        )
    if cost.ms_per_kib > budget_ms_per_kib:
        findings.append(
            f"the measured cost is {cost.ms_per_kib:.2f} ms/KiB against a budget of "
            f"{budget_ms_per_kib} ms/KiB, so the budget is already known to be breached on "
            "hardware nobody is worried about"
        )
    return tuple(findings)


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
