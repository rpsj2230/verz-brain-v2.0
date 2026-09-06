"""Saving a configured agent as a template, which is a data-leak problem wearing a feature.

`brain.agents.template` says what a template is and `brain.agents.install` says how one is
installed. This module says where a *client-authored* one comes from, and it starts from the
observation that makes it different from both: an agent configured against one company's real
data is being offered to somebody else. Its persona may quote a client. Its scope predicate
may name a department that exists only here. A skill reference, a connector name, a golden
question or a placeholder prompt may hold an email address, a price, a URL or an internal
identifier.

**Every literal in a configured agent is a candidate leak, and the ones that matter are the
ones nobody classified.** A value drawn from a closed vocabulary this codebase defines is a
value somebody already thought about: a `Tier`, a `SideEffect`, a `Scope` operator. A content
digest identifies without disclosing. Everything else is a string a person typed while
thinking about their own company, and `scan` puts each of those in front of the author as an
item to accept or redact. See `EVERY_LITERAL_IS_A_CANDIDATE_LEAK`.

**Publishing is blocked until every item is dispositioned, and there is no bulk accept.**
`publish_authored` refuses while `AuthoredDraft.undecided` is non-empty, `decide` takes one
item id at a time, and no function here takes a disposition that applies to more than one
item. A default would be a decision nobody made about data they did not look at, which is
precisely the failure this leaf exists to prevent. See `A_DEFAULT_IS_A_DECISION_NOBODY_MADE`.

**The report names what it passed as well as what it asks about.** `LeakReport.passed`
carries every classified literal with the reason it was classified. A report that showed only
the questions would hide the answers it gave itself, and the reader would have no way to
notice that a whole field had been quietly waved through. See
`A_REPORT_THAT_HIDES_WHAT_IT_PASSED_IS_A_DEFAULT_IN_DISGUISE`.

**Memory is never exported, and that is structural rather than a setting.** There is no flag
here, defaulting to off or otherwise, because a flag is flipped by somebody who wants their
template to work properly for the next person. Four things close the routes instead:

- `author` takes an `EffectiveAgent` and reads its `TemplateManifest`. It does not take an
  `Installation`, so `placeholder_answers` -- the price list, the escalation contact, the
  values a company typed into somebody else's questions -- is not an attribute away. **The
  questions travel and the answers do not, and they live in two different objects.**
- `author` names the nine manifest fields it carries in `CARRIED_FIELDS` and refuses to run
  at all when `TemplateManifest` declares a field that tuple does not mention. A `memory`
  field added to the manifest tomorrow stops authoring dead rather than travelling with it.
- The body that is signed is `TemplateManifest.document()`, whose keys are the seventeen
  `MANIFEST_PATHS` and nothing else, and the manifest is rebuilt through `model_validate`
  with `extra="forbid"` before it is signed.
- A decision can remove text and cannot add any. The one thing an author types after the
  report is a hoisted question, and `publish_authored` re-scans the resulting body and
  refuses any shaped literal in it that no accepted item accounts for. See
  `NOTHING_ENTERS_THE_BODY_AFTER_THE_REPORT` and
  `A_HOISTED_QUESTION_MUST_NOT_CONTAIN_THE_ANSWER`.

Said plainly, because the honest version is worth more than the confident one: none of this
stops an author typing a client's name into a persona and accepting it. It is not meant to.
The leak report is a review surface, in the register `brain.tools.sop_import` uses for its own
patterns: a flag is honest and a silent edit is not, and the defence is that a person looked
rather than that a pattern caught it.

**Hoisting is a redaction with a question attached.** `Disposition.HOIST` replaces the value
with a `{{key}}` token and declares a `Placeholder`, so the install wizard that already exists
asks the next company for their own value. Nothing substitutes the token back in:
`materialise` does not interpolate, and inventing an interpolation step here would be a second
templating language for `brain.agents.install` to be reconciled with. What hoisting buys today
is that the value is gone and the question is asked, and the installer sets `persona` or
`authority.scope` in the overlay, both of which are settable paths.

**A hoisted scope value fails closed, which is why hoisting a predicate is safe and dropping
one is not.** Redaction never removes a clause: `Scope` composes by conjunction only, so
deleting a clause widens the predicate, and a redaction that widens a ceiling is the worst
possible reading of the word. Both redaction and hoisting replace the *value*, leaving a
predicate that matches no row until somebody sets it. See `A_REDACTION_MAY_NOT_WIDEN_A_SCOPE`.

**Three visibilities, and they are not the authority axis.** `AUDIENCE_IS_NOT_AUTHORITY` in
`brain.agents.model` makes the argument for agents and it holds here one layer up and one
layer out. Private, organisation and catalogue answer how far a template may travel. They
narrow nothing and reach nothing: a template published to the catalogue confers no capability,
no tool and no row, because what a run reaches is still `E(caller) n agent_ceiling` computed
by `EntitlementSet.intersect`. `offer_audience` turns a visibility into who may install it
here, and organisation and catalogue produce the same local answer deliberately: the third
level is about leaving this installation, not about who in it may press install.

**A save is private and there is no field on a draft that could say otherwise.**
`AuthoredDraft.visibility` is a property returning `SAVED_VISIBILITY`, so M13.6.1's "private"
is a shape rather than a default somebody can pass over.

**The dispositions must be complete before the first signature, at every visibility,
including private.** This is the one ordering decision in the module and it reads as
over-strict until you follow it through: visibility is a property of the *offer* and the body
is a property of the *signature*. `set_visibility` returns a record carrying the same
`SignedManifest` object, so raising a private template to the catalogue a month later never
re-opens the leak question. If private publishing were allowed to skip the report, the
catalogue would be reachable in two steps with the report skipped in the first one.

**Authoring copies supervision and never relaxes it.** The source's `max_side_effect` and
leash travel unchanged into the new manifest, and there is no parameter here that could raise
either. Lowering them silently was rejected: it produces a template that looks like the agent
it was saved from and behaves differently, and the person installing it would have no way to
see that the thing they were promised had been quietly detuned.

**Nobody may author a template called `blank`.** The blank template is the floor every
hand-built agent installs from, and a client-authored template shadowing that id would let a
hand-built agent start from somebody's chosen guardrails instead of `SideEffect.NONE` and an
empty leash. That is a privilege escalation dressed as a naming collision.

Three designs were rejected.

*A `carry_memory` flag defaulting to off.* It is the obvious shape and it is the one the leaf
exists to refuse. A flag is a request, and the request is made by whoever is annoyed that
their template does not work as well for the next person as it did for them.

*Per-occurrence dispositions.* An item is one distinct string, and its decision applies at
every location it occurs. Keying by location as well would let an author redact an email in
the persona and accept the same email in a golden question, and the value ships anyway from
the place they were not looking at.

*A table for drafts and their dispositions.* Nothing in `src` writes `agent.template_version`
or `agent.template_instance` either, so a third table would be storage for a writer that does
not exist, and the migration would be another mechanism nothing calls. This is the same
refusal `brain.agents.install` made about a `placeholder_answers` column, for the same
reason, and it has the same cost: a draft does not survive a restart and the author redoes
the report. When somebody builds the writer, the record it needs is `AuthoredTemplate`.

**What consults this, and what does not.** `offer_for` returns a real
`brain.agents.install.Offer`, so an authored template goes into the real
`TemplateCatalogue`, through `open_for`, `begin`, `answer`, `provide` and `complete`, and out
as a real `AgentRecord`: `tests/unit/test_agent_authoring.py` drives that whole loop and
asserts a redacted literal is absent from the installed agent while an accepted one is
present. `publish` and `TemplateManifest` are the domain's own rather than restated here.

No HTTP route calls any of it, and the reason is not this leaf: there is no route behind the
gate anywhere in this repository, and `brain.agents.model`, `brain.agents.template` and
`brain.agents.install` each refused to invent one because a request pipeline invented in a
domain module is a second pipeline for the real one to be reconciled with. Nothing persists
an `AuthoredDraft`, an `AuthoredTemplate` or a `Decision`, for the reason given above. So the
flow is callable and tested and there is no console button, and saying which of those is
missing is worth more than a paragraph that implies neither is.

Task ids: M13.6.1, M13.6.2, M13.6.3, M13.6.4, M13.6.5, M13.6.6
"""

from __future__ import annotations

import enum
import hashlib
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, field_validator

from brain.agents.install import Offer
from brain.agents.model import OWNER_ID_CHARS, AgentAudience
from brain.agents.template import (
    BLANK_TEMPLATE_ID,
    MANIFEST_PATHS,
    PROMPT_CHARS,
    EffectiveAgent,
    ManifestIdentity,
    Placeholder,
    SignedManifest,
    TemplateError,
    TemplateManifest,
    publish,
)
from brain.audit.ledger import DIGEST
from brain.core.envelope import OBJECT_NAME_PATTERN, SideEffect
from brain.core.scope import Op
from brain.knowledge.visibility import Visibility
from brain.models.routing import Tier

# ------------------------------------------------------------------ written-down reasons

#: Why the report asks about so much, and why the quiet fields are the dangerous ones.
EVERY_LITERAL_IS_A_CANDIDATE_LEAK: Final = (
    "An agent saved as a template was configured against one company's real data, so every "
    "string in it was typed by somebody thinking about that company. A literal drawn from a "
    "closed vocabulary this codebase defines is one somebody already thought about, and a "
    "digest identifies without disclosing. Everything else is unclassified, and the "
    "unclassified ones are exactly the ones nobody has looked at, which is why they are the "
    "ones the report asks about rather than the ones it passes."
)

#: Why publishing is blocked rather than defaulted.
A_DEFAULT_IS_A_DECISION_NOBODY_MADE: Final = (
    "A default disposition is a decision taken on somebody's behalf about data they did not "
    "look at, and the reader of the published template cannot tell it apart from a decision "
    "somebody took. So there is no default, no accept-all and no disposition that applies to "
    "more than one item: publish is refused while any item is undecided, and each decision "
    "carries the person who took it and the moment they took it."
)

#: Why the classified literals are shown too.
A_REPORT_THAT_HIDES_WHAT_IT_PASSED_IS_A_DEFAULT_IN_DISGUISE: Final = (
    "A report listing only what it wants a decision on has taken every other decision "
    "itself, silently, and the author has no way to notice that a whole field was waved "
    "through. So the classified literals are carried beside the items, each with the reason "
    "it was classified, and a reader who disagrees with a classification can see it to "
    "disagree with it."
)

#: The rule M13.6.5 asks for, and the shape that makes it structural.
MEMORY_HAS_NO_ROUTE_INTO_A_MANIFEST: Final = (
    "Memory is never exported, and there is no setting that changes that because a setting "
    "is flipped by whoever wants their template to work properly for the next person. The "
    "closure is structural: author takes an EffectiveAgent and never an Installation, so "
    "placeholder answers are not an attribute away; it carries the nine fields named in "
    "CARRIED_FIELDS and refuses to run when the manifest declares one that tuple does not "
    "mention; the body signed is the seventeen manifest paths and is rebuilt through a model "
    "that forbids extras; and a disposition can remove text and cannot add any."
)

#: Why nothing may be typed into the body after the report has been read.
NOTHING_ENTERS_THE_BODY_AFTER_THE_REPORT: Final = (
    "The report is the record of what somebody looked at, so a value arriving in the body "
    "after it was written has been reviewed by nobody. The only text an author types after "
    "the report is a hoisted question, so the published body is scanned again and every "
    "shaped literal in it must be accounted for by an item the author accepted or by the "
    "mark a disposition left behind."
)

#: Why a hoisted question is checked against the value it replaces.
A_HOISTED_QUESTION_MUST_NOT_CONTAIN_THE_ANSWER: Final = (
    "Hoisting removes a value and asks the next company for their own. A question reading "
    "'who replaces bob@acme.example?' has put the value back, one line further down, in a "
    "field nobody thinks of as configuration. So a hoisted prompt may not contain the text "
    "of the item it replaces, and the re-scan refuses a shaped literal inside it that no "
    "accepted item accounts for."
)

#: Why a redaction replaces a predicate's value rather than dropping its clause.
A_REDACTION_MAY_NOT_WIDEN_A_SCOPE: Final = (
    "A Scope composes by conjunction only, so removing a clause removes a narrowing and the "
    "predicate reaches more rows than it did. A redaction that widened a ceiling would be "
    "the worst available reading of the word. Both redaction and hoisting therefore replace "
    "the value and leave the clause standing, which matches no row at all until somebody "
    "sets it: the failure direction is closed rather than open."
)

#: How far a template travels is not what a run through it reaches.
A_TEMPLATE_VISIBILITY_IS_NOT_A_REACH: Final = (
    "Private, organisation and catalogue answer how far a template may travel. They confer "
    "no capability, no tool and no row: what a run reaches is E(caller) intersected with the "
    "agent ceiling, computed by EntitlementSet.intersect, and no visibility is an input to "
    "it. A template published to the catalogue thereby reaches nothing, which is "
    "AUDIENCE_IS_NOT_AUTHORITY restated for a thing that travels between installations."
)

#: Why the guardrails travel unchanged.
AUTHORING_COPIES_SUPERVISION_AND_NEVER_RELAXES_IT: Final = (
    "The source agent's largest side effect and its leash travel into the authored manifest "
    "exactly as they stand, and no parameter here can raise either. Lowering them silently "
    "was rejected: it would produce a template that looks like the agent it was saved from "
    "and behaves differently, and the installer would have no way to see the difference."
)

#: Why one template id is refused outright.
NOBODY_MAY_AUTHOR_A_TEMPLATE_THAT_SHADOWS_THE_BLANK_ONE: Final = (
    "The blank template is the floor every hand-built agent installs from, and its sealed "
    "values are SideEffect.NONE and an empty leash. A client-authored template holding that "
    "id would let a hand-built agent start from somebody's chosen guardrails instead of the "
    "floor, which is a privilege escalation wearing the clothes of a naming collision."
)

#: Why a new manifest field stops authoring rather than travelling with it.
A_NEW_MANIFEST_FIELD_DOES_NOT_TRAVEL_UNTIL_SOMEBODY_SAYS_SO: Final = (
    "The fields an authored template carries are written out rather than copied wholesale, "
    "so a field added to TemplateManifest arrives in an authored template only when somebody "
    "adds it to CARRIED_FIELDS and thereby decides that it should travel. Copying the model "
    "would make that decision automatically, in the direction that exports more."
)

# ------------------------------------------------------------------ visibility (M13.6.6)


class TemplateVisibility(enum.StrEnum):
    """How far a template may travel. Three levels, and there is no fourth.

    Not `brain.knowledge.visibility.Visibility`, and the difference is the third level. That
    enum answers who inside one company may reach a document, and its widest member is the
    whole company. This one has a member *beyond* the company, because a template is a thing
    that leaves an installation, and a level meaning "may be shared outside" has nowhere to
    live in a per-company vocabulary. `offer_audience` maps this onto that one where the
    question really is who inside this company may install.
    """

    #: The author, and nobody else. What a save produces.
    PRIVATE = "private"
    #: Anybody in this installation may install it. It does not leave.
    ORGANISATION = "organisation"
    #: It may be offered beyond this installation.
    CATALOGUE = "catalogue"


#: Narrowest first. Written out rather than relying on declaration order, for the reason
#: `brain.knowledge.visibility.VISIBILITY_ORDER` gives: declaration order is not part of an
#: enum's contract and a reordering during a merge would silently invert a comparison.
VISIBILITY_ORDER: Final[tuple[TemplateVisibility, ...]] = (
    TemplateVisibility.PRIVATE,
    TemplateVisibility.ORGANISATION,
    TemplateVisibility.CATALOGUE,
)

#: What a save produces, and the only visibility a draft can hold (M13.6.1).
SAVED_VISIBILITY: Final = TemplateVisibility.PRIVATE

#: Every authored template starts a lineage of its own. A second version of one is a second
#: publish and that path is `brain.agents.template`'s, not this module's.
FIRST_VERSION: Final = 1


def may_leave_this_installation(visibility: TemplateVisibility) -> bool:
    """Whether this template may be offered beyond the company that authored it.

    True for the catalogue level alone. Nothing in `src` exports a template anywhere, so this
    is the predicate an exporter would have to consult rather than an exporter's behaviour,
    and it is written here because the level it reads is defined here.
    """
    return visibility is TemplateVisibility.CATALOGUE


def offer_audience(visibility: TemplateVisibility, *, author_id: str) -> AgentAudience:
    """Who may install this template *here* (M13.6.6).

    Organisation and catalogue give the same answer deliberately. The third level is about
    leaving this installation, and inside this installation both mean everybody, so a mapping
    that separated them would be inventing a distinction to make the function look complete.

    This is who may install, never who may see the agent afterwards. `complete` takes the
    agent's audience as its own argument and never reads an offer's, per
    `THE_OFFER_AUDIENCE_IS_NOT_THE_AGENT_AUDIENCE` in `brain.agents.install`.
    """
    if visibility is TemplateVisibility.PRIVATE:
        return AgentAudience(level=Visibility.PERSONAL, owner_id=author_id)
    return AgentAudience(level=Visibility.COMPANY, owner_id=author_id)


# ------------------------------------------------------------ what a literal is (M13.6.2)


class Classification(enum.StrEnum):
    """Whether somebody has already thought about a literal. Closed; there is no fifth.

    The three that are not `UNCLASSIFIED` are the whole of what the scanner is willing to
    decide on its own, and each is a claim it can defend from the value itself rather than
    from a guess about what the value means.
    """

    #: A member of a closed vocabulary this codebase defines, at a field that holds that
    #: vocabulary. Nobody's business data is inside a `SideEffect`.
    VOCABULARY = "vocabulary"
    #: A content hash. It identifies a skill without disclosing anything about it.
    DIGEST = "digest"
    #: The mark a disposition left behind: a redaction token or a hoisted placeholder token.
    DISPOSITIONED = "dispositioned"
    #: Nobody has said what this is, so somebody has to (M13.6.3).
    UNCLASSIFIED = "unclassified"


class LiteralKind(enum.StrEnum):
    """What a literal looks like. A hint for the reader, never a permission to skip one.

    The kind decides nothing about whether an item needs a decision: `Classification` does
    that. What the kind is for is the author's eye, so an email address in the middle of a
    persona is a row of its own rather than a sentence to be found by reading.
    """

    EMAIL = "email"
    URL = "url"
    MONEY = "money"
    PHONE = "phone"
    #: An identifier with letters and digits in it: an account number, a project code.
    CODE = "code"
    #: A structured scalar with no shape of its own: a tool name, a connector, a scope value.
    NAME = "name"
    #: A whole free-text field, offered as one item so the words a scanner cannot classify
    #: are still something somebody has to read and accept.
    PROSE = "prose"


#: Field names holding free text. The value is offered whole as one `PROSE` item and every
#: shaped span inside it is offered as an item of its own, so accepting the paragraph does
#: not thereby accept the email address in it.
PROSE_FIELDS: Final[frozenset[str]] = frozenset(
    {"persona", "summary", "display_name", "prompt", "question", "expectation"}
)

#: Field name to the closed vocabulary that field holds. Keyed by field as well as by value,
#: so a persona reading "none" is not classified as a side effect because it happens to spell
#: one. `LeashRung.rung` is absent on purpose: `AutonomyTier` is an `IntEnum`, so a dumped
#: rung is an integer and never reaches the scanner at all.
VOCABULARIES: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "op": frozenset(member.value for member in Op),
        "tier": frozenset(member.value for member in Tier),
        "max_side_effect": frozenset(member.value for member in SideEffect),
    }
)

#: What a redacted value becomes. One token rather than a per-kind one, because a template
#: whose redactions said `[email]` and `[price]` would leak the shape of what was removed.
REDACTION_MARK: Final = "[redacted]"

#: What a hoisted value becomes, and the grammar a reader can recognise it by. Nothing
#: substitutes it: see the module docstring.
HOIST_MARK_RE: Final = re.compile(r"^\{\{[a-z][a-z0-9_]*\}\}$")

DIGEST_RE: Final = re.compile(DIGEST)

#: How much of the digest of a literal names it. Sixteen hex characters is enough for a
#: report to be keyed without collision, and the id is over the text rather than over the
#: location so one distinct string is one decision wherever it occurs.
ITEM_ID_CHARS: Final = 16

#: The shapes worth pointing at inside prose, in the order they are tried. Deliberately
#: conservative: a false positive costs the author a click, and a false negative is caught by
#: the `PROSE` item covering the whole field, which nobody can accept without reading it.
SHAPES: Final[tuple[tuple[LiteralKind, re.Pattern[str]], ...]] = (
    (LiteralKind.EMAIL, re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")),
    (LiteralKind.URL, re.compile(r"(?:https?://|www\.)[^\s<>\"']+")),
    (
        LiteralKind.MONEY,
        re.compile(
            r"[$£€]\s?\d[\d,]*(?:\.\d{1,2})?"
            r"|\b\d[\d,]*(?:\.\d{1,2})?\s?(?:SGD|USD|GBP|EUR|MYR)\b"
        ),
    ),
    # A leading plus is required. Without it every four-digit year in a persona is a phone
    # number, and a report nobody can get to the end of is a report nobody reads.
    (LiteralKind.PHONE, re.compile(r"\+\d[\d ()-]{6,18}\d")),
    (LiteralKind.CODE, re.compile(r"\b[A-Za-z]{2,}[-_/]?\d{2,}[A-Za-z0-9_-]*\b")),
)

#: Which kind wins when one string is found in two shapes at two places. Strongest first, so
#: the row the author reads names the most specific thing that string ever was.
KIND_PRECEDENCE: Final[tuple[LiteralKind, ...]] = (
    LiteralKind.EMAIL,
    LiteralKind.URL,
    LiteralKind.MONEY,
    LiteralKind.PHONE,
    LiteralKind.CODE,
    LiteralKind.NAME,
    LiteralKind.PROSE,
)


class AuthoringError(TemplateError):
    """A refusal to save or publish a client-authored template.

    Inside the template taxonomy rather than beside it: a caller catching `TemplateError`
    around a publish is asking "did this template get made", and the answer is no whether the
    refusal came from the seal, the signature or the leak report.
    """


class UndispositionedError(AuthoringError):
    """A publish attempted while some literal has no decision against it (M13.6.4).

    Its own type, because a console has to say something specific: this is not a malformed
    template, it is a finished one waiting on a person, and the list of what it is waiting on
    is the whole of what the reader needs.
    """


@dataclass(frozen=True)
class FoundLiteral:
    """One string found in the candidate body, and what the scanner is willing to say.

    Carries the location it was found at rather than a summary of where, for the reason
    `brain.tools.sop_import.Finding` gives about carrying the line: an author deciding
    whether a value may travel needs to see where it sits, and a report saying "an email
    somewhere in the persona" makes them go and find it themselves.
    """

    kind: LiteralKind
    classification: Classification
    #: A dotted manifest path, with `/` separated keys and indices for anything inside it:
    #: `persona`, `authority.scope/clauses/0/value`, `golden_set/1/question`.
    location: str
    text: str
    #: Why this classification, in words. Empty for an unclassified literal: the reason it is
    #: unclassified is that there is no reason to give.
    reason: str = ""


@dataclass(frozen=True)
class LeakItem:
    """One distinct string an author has to accept or redact (M13.6.3).

    Keyed by the text and not by where it was found, so one string is one decision however
    many places it occurs in. Keying by location as well would let somebody redact an email
    in the persona, accept the same email in a golden question, and ship it anyway from the
    field they were not looking at.
    """

    item_id: str
    kind: LiteralKind
    text: str
    #: Every place this string was found, sorted. Plural because the decision covers all of
    #: them, and a reader deciding needs to see the whole spread rather than one example.
    locations: tuple[str, ...]


@dataclass(frozen=True)
class LeakReport:
    """What has to be decided, and what was decided for you and why (M13.6.3)."""

    items: tuple[LeakItem, ...]
    #: Every classified literal, with its reason. See
    #: `A_REPORT_THAT_HIDES_WHAT_IT_PASSED_IS_A_DEFAULT_IN_DISGUISE`.
    passed: tuple[FoundLiteral, ...]

    @property
    def item_ids(self) -> frozenset[str]:
        return frozenset(item.item_id for item in self.items)

    def item(self, item_id: str) -> LeakItem:
        """One item by id, or a refusal.

        Raises rather than returning `None`, for the reason `brain.agents.template.verify`
        gives: a caller who forgets to check a missing value records a decision against
        nothing, and the item it was meant for stays undecided while the author believes they
        have dealt with it.
        """
        for entry in self.items:
            if entry.item_id == item_id:
                return entry
        msg = (
            f"{item_id!r} is not an item on this report; a decision against an item that is "
            "not there leaves the item it was meant for undecided"
        )
        raise AuthoringError(msg)


class Disposition(enum.StrEnum):
    """What is to happen to one literal. Three members, and the third is not a compromise.

    `ACCEPT` and `REDACT` are the two the work breakdown names. `HOIST` is a redaction with a
    question attached, and it is a member rather than a flag on `REDACT` because the two
    produce different templates: a redacted value is gone, and a hoisted one is gone and
    asked about. Collapsing them would lose the question, which is the half the next company
    needs.
    """

    ACCEPT = "accept"
    REDACT = "redact"
    HOIST = "hoist"


class Decision(BaseModel):
    """One disposition, the person who took it and the moment they took it (M13.6.4).

    `at` is passed in rather than read from a clock here, for the reason
    `brain.agents.template.FieldOwner` gives: a rule about times that reads the clock itself
    cannot be tested at its own boundary.

    A decision is a person's act, so `by` is required and there is no system principal that
    could stand in for one. That is the whole of what stops a caller writing an accept nobody
    took.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    disposition: Disposition
    by: str = Field(min_length=1, max_length=OWNER_ID_CHARS)
    at: datetime
    #: The placeholder a hoist declares. Required for a hoist and refused for anything else.
    placeholder_key: str = Field(default="", max_length=60)
    #: The question the next company is asked. Bounded like `Placeholder.prompt`, because it
    #: becomes one.
    prompt: str = Field(default="", max_length=PROMPT_CHARS)

    @field_validator("at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        """A naive decision time is a silent bug, as it is on `FieldOwner.set_at`."""
        if v.tzinfo is None:
            msg = "a decision time must be timezone-aware; a naive one is a silent bug"
            raise ValueError(msg)
        return v

    def model_post_init(self, _context: object, /) -> None:
        if self.disposition is Disposition.HOIST:
            if not re.match(OBJECT_NAME_PATTERN, self.placeholder_key):
                msg = (
                    f"a hoist needs a placeholder key matching {OBJECT_NAME_PATTERN}, and "
                    f"{self.placeholder_key!r} is not one; the key becomes a Placeholder the "
                    "install wizard asks about"
                )
                raise ValueError(msg)
            if not self.prompt.strip():
                msg = (
                    "a hoist needs a question; hoisting without one removes the value and "
                    "asks the next company nothing, which is a redaction with extra steps"
                )
                raise ValueError(msg)
            return
        if self.placeholder_key or self.prompt:
            msg = (
                f"{self.disposition} carries no placeholder and no question; a question "
                "attached to an accept or a redact is one nobody will ever be asked"
            )
            raise ValueError(msg)


# ------------------------------------------------------------------- the scanner (M13.6.2)
def _leaves(value: JsonValue, location: str) -> Iterator[tuple[str, str]]:
    """Every string leaf under one manifest path, with the location it sits at.

    Strings only, and that is a statement about the manifest rather than a shortcut. The only
    non-string scalars a manifest document holds are `identity.version`, which is an integer,
    `placeholders[].required`, which is a boolean, and `guardrails.leash[].rung`, which dumps
    as an integer because `AutonomyTier` is an `IntEnum`. None of the three can carry a value
    about a company. If a manifest ever gains a numeric field that can, this function is
    where that stops being true, and `CARRIED_FIELDS` is what makes somebody look.
    """
    if isinstance(value, str):
        yield location, value
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _leaves(item, f"{location}/{index}")
    elif isinstance(value, dict):
        for name in sorted(value):
            yield from _leaves(value[name], f"{location}/{name}")


def _field_of(location: str) -> str:
    """The last name in a location, whichever separator it arrived under."""
    return location.replace("/", ".").rsplit(".", 1)[-1]


def _spans(text: str) -> tuple[tuple[LiteralKind, str], ...]:
    """The shaped stretches inside one piece of prose, without overlaps.

    Overlaps are dropped rather than reported, because a code found inside an email address
    is one value and two rows, and the second row's decision would silently do nothing once
    the first was applied.
    """
    found: list[tuple[LiteralKind, str]] = []
    taken: list[tuple[int, int]] = []
    for kind, pattern in SHAPES:
        for match in pattern.finditer(text):
            start, end = match.span()
            if any(start < seen_end and seen_start < end for seen_start, seen_end in taken):
                continue
            taken.append((start, end))
            found.append((kind, match.group(0)))
    return tuple(found)


def _shape_of(text: str) -> LiteralKind:
    """The kind of a whole structured scalar: its shape if it has one, otherwise a name."""
    for kind, pattern in SHAPES:
        if pattern.fullmatch(text):
            return kind
    return LiteralKind.NAME


def _classify(location: str, text: str) -> tuple[FoundLiteral, ...]:
    """One leaf, as the literals it holds.

    The order of the tests is the argument. A vocabulary member is checked at its own field,
    a digest by its shape, a disposition mark by its shape, and only then is the leaf read as
    prose or as a scalar. Reading prose first would offer a whole `Scope` operator as a
    paragraph to accept.
    """
    if not text.strip():
        return ()
    vocabulary = VOCABULARIES.get(_field_of(location))
    if vocabulary is not None and text in vocabulary:
        return (
            FoundLiteral(
                kind=LiteralKind.NAME,
                classification=Classification.VOCABULARY,
                location=location,
                text=text,
                reason=(
                    "a member of a closed vocabulary this system defines, at the field that "
                    "holds it; no company's data is inside it"
                ),
            ),
        )
    if DIGEST_RE.match(text):
        return (
            FoundLiteral(
                kind=LiteralKind.CODE,
                classification=Classification.DIGEST,
                location=location,
                text=text,
                reason="a content hash: it identifies without disclosing what it identifies",
            ),
        )
    if text == REDACTION_MARK or HOIST_MARK_RE.match(text):
        return (
            FoundLiteral(
                kind=LiteralKind.NAME,
                classification=Classification.DISPOSITIONED,
                location=location,
                text=text,
                reason="the mark a disposition left behind rather than a value",
            ),
        )
    if _field_of(location) in PROSE_FIELDS:
        return (
            FoundLiteral(
                kind=LiteralKind.PROSE,
                classification=Classification.UNCLASSIFIED,
                location=location,
                text=text,
            ),
            *(
                FoundLiteral(
                    kind=kind,
                    classification=Classification.UNCLASSIFIED,
                    location=location,
                    text=span,
                )
                for kind, span in _spans(text)
            ),
        )
    return (
        FoundLiteral(
            kind=_shape_of(text),
            classification=Classification.UNCLASSIFIED,
            location=location,
            text=text,
        ),
    )


def _identifier(text: str) -> str:
    """A stable id for one distinct string.

    Length-prefixed before hashing, for the reason `brain.audit.ledger` gives about joining:
    without it two different strings can be assembled into one input. Over the text alone, so
    the same value found in two fields is one item and one decision.
    """
    return hashlib.sha256(f"{len(text)}:{text}".encode()).hexdigest()[:ITEM_ID_CHARS]


def _strongest(kinds: Iterable[LiteralKind]) -> LiteralKind:
    """The most specific kind one string was ever found as."""
    seen = set(kinds)
    for kind in KIND_PRECEDENCE:
        if kind in seen:
            return kind
    return LiteralKind.NAME


def scan(document: Mapping[str, JsonValue]) -> LeakReport:
    """Every literal in a candidate body, split into what needs a decision and what does not.

    Takes the flat document rather than a `TemplateManifest`, because that is the shape the
    seal, the overlay, the ownership map and M13.4's diff are all keyed by, and a second
    traversal over the nested form would be a second idea of what a path is.
    """
    found: list[FoundLiteral] = []
    for path in sorted(document):
        for location, text in _leaves(document[path], path):
            found.extend(_classify(location, text))

    grouped: dict[str, list[FoundLiteral]] = {}
    for literal in found:
        if literal.classification is Classification.UNCLASSIFIED:
            grouped.setdefault(literal.text, []).append(literal)

    items = sorted(
        (
            LeakItem(
                item_id=_identifier(text),
                kind=_strongest(entry.kind for entry in group),
                text=text,
                locations=tuple(sorted({entry.location for entry in group})),
            )
            for text, group in grouped.items()
        ),
        key=lambda item: (item.locations[0], item.text),
    )
    return LeakReport(
        items=tuple(items),
        passed=tuple(
            literal
            for literal in found
            if literal.classification is not Classification.UNCLASSIFIED
        ),
    )


# --------------------------------------------------------------- saving an agent (M13.6.1)
#: The manifest fields an authored template carries, written out. See
#: `A_NEW_MANIFEST_FIELD_DOES_NOT_TRAVEL_UNTIL_SOMEBODY_SAYS_SO`. `identity` is here because
#: it is a declared field of the model, and it is the one field `carried` does not copy: the
#: authored template's identity is its own.
CARRIED_FIELDS: Final[tuple[str, ...]] = (
    "identity",
    "persona",
    "tier",
    "skills",
    "authority",
    "connectors",
    "guardrails",
    "golden_set",
    "placeholders",
)


def carried(
    manifest: TemplateManifest, *, fields: Sequence[str] = CARRIED_FIELDS
) -> dict[str, Any]:
    """The parts of a source manifest that travel, and a refusal when something new appeared.

    Refuses when `TemplateManifest` declares a field `fields` does not name. That refusal is
    the mechanism rather than a tidiness check: a field added to the manifest would otherwise
    be carried by a wholesale copy with nobody deciding that it should be, and the direction
    the accident runs in is the one that exports more. See
    `A_NEW_MANIFEST_FIELD_DOES_NOT_TRAVEL_UNTIL_SOMEBODY_SAYS_SO` and
    `MEMORY_HAS_NO_ROUTE_INTO_A_MANIFEST`.

    `fields` is a parameter with the module's own tuple as its default, for the reason
    `brain.agents.install.plan` gives about `step_fields`: without it the only way to prove
    the refusal works would be to edit the constant, which is a mutation rather than a test.
    """
    undeclared = sorted(set(TemplateManifest.model_fields) - set(fields))
    if undeclared:
        msg = (
            f"the manifest declares {undeclared} and nobody has said whether they travel "
            f"into an authored template; add them to CARRIED_FIELDS deliberately. "
            f"{A_NEW_MANIFEST_FIELD_DOES_NOT_TRAVEL_UNTIL_SOMEBODY_SAYS_SO}"
        )
        raise AuthoringError(msg)
    return {name: getattr(manifest, name) for name in fields if name != "identity"}


@dataclass(frozen=True)
class AuthoredDraft:
    """An agent saved as a template, before anybody has decided anything (M13.6.1).

    **There is no visibility field and no way to set one.** `visibility` is a property
    answering `SAVED_VISIBILITY`, so "saved as a *private* template" is the shape of the type
    rather than a default a caller can pass over.

    Frozen, and `decide` returns a new draft, so the state before a decision is still
    holdable while a console renders the state after it. That is the arrangement
    `TemplateInstance` and `InstallDraft` both have and for the same reason.

    `report` is recomputed from the manifest rather than stored beside it. The manifest never
    changes on a draft, so the two cannot be made to disagree by construction, and storing
    the report would be a second fact about one document.
    """

    author_id: str
    #: The candidate body: the source's manifest with an identity of its own.
    manifest: TemplateManifest
    #: One entry per decided item, keyed by `LeakItem.item_id`.
    decisions: Mapping[str, Decision] = field(default_factory=dict)

    @property
    def visibility(self) -> TemplateVisibility:
        return SAVED_VISIBILITY

    @property
    def document(self) -> dict[str, JsonValue]:
        return self.manifest.document()

    @property
    def report(self) -> LeakReport:
        return scan(self.document)

    @property
    def undecided(self) -> tuple[LeakItem, ...]:
        """The items still waiting on a person, in the order the report lists them."""
        return tuple(item for item in self.report.items if item.item_id not in self.decisions)


def author(
    source: EffectiveAgent,
    *,
    template_id: str,
    display_name: str,
    author_id: str,
    summary: str = "",
) -> AuthoredDraft:
    """Save any agent as a private template (M13.6.1).

    Takes an `EffectiveAgent`, which is what `materialise` produces for an installed template
    and for a hand-built agent alike, so "any agent" is one code path rather than a branch.

    **It does not take an `Installation`, and that is the load-bearing part of M13.6.5.** An
    `Installation` carries `placeholder_answers`: the price list, the escalation contact, the
    values this company typed into somebody else's questions. Reaching them would be one
    attribute access from here. The questions travel, in `placeholders`, and the answers do
    not, and the two live in different objects so the difference is structural rather than
    remembered.

    The identity is replaced rather than inherited. An authored template carrying its
    source's `template_id`, `version` and `published_by` would claim to be an install of
    somebody else's lineage, and M13.4's upgrade diff would compare it against that lineage.

    The guardrails are copied unchanged and there is no parameter that could raise them; see
    `AUTHORING_COPIES_SUPERVISION_AND_NEVER_RELAXES_IT`.
    """
    if template_id == BLANK_TEMPLATE_ID:
        msg = (
            f"{BLANK_TEMPLATE_ID!r} is the template every hand-built agent installs from and "
            f"it cannot be authored over. {NOBODY_MAY_AUTHOR_A_TEMPLATE_THAT_SHADOWS_THE_BLANK_ONE}"
        )
        raise AuthoringError(msg)
    identity = ManifestIdentity(
        template_id=template_id,
        version=FIRST_VERSION,
        published_by=author_id,
        display_name=display_name,
        summary=summary,
    )
    manifest = TemplateManifest(identity=identity, **carried(source.manifest))
    return AuthoredDraft(author_id=author_id, manifest=manifest)


# ------------------------------------------------------- deciding, one item at a time (M13.6.3)
def _hoisted_keys(draft: AuthoredDraft) -> list[str]:
    """Every placeholder key this draft's decisions already declare."""
    return [
        decision.placeholder_key
        for decision in draft.decisions.values()
        if decision.disposition is Disposition.HOIST
    ]


def _check_hoist(draft: AuthoredDraft, item: LeakItem, decision: Decision) -> None:
    """Refuse a hoist that collides with a question already asked, or that answers its own.

    Two refusals and they are different failures. A duplicate key produces a manifest asking
    one question twice, and `provide` would record one answer against whichever the reader of
    the form happened to see. A question containing the value it replaces has put the value
    back one line further down, in a field nobody thinks of as configuration; see
    `A_HOISTED_QUESTION_MUST_NOT_CONTAIN_THE_ANSWER`.
    """
    if decision.disposition is not Disposition.HOIST:
        return
    declared = {existing.key for existing in draft.manifest.placeholders}
    if decision.placeholder_key in declared or decision.placeholder_key in _hoisted_keys(draft):
        msg = (
            f"{decision.placeholder_key!r} is already a question this template asks; a "
            "template asking one question twice records one answer against whichever field "
            "the person filling it in happened to see"
        )
        raise AuthoringError(msg)
    if item.text.casefold() in decision.prompt.casefold():
        msg = (
            f"the question for {decision.placeholder_key!r} contains the value it replaces. "
            f"{A_HOISTED_QUESTION_MUST_NOT_CONTAIN_THE_ANSWER}"
        )
        raise AuthoringError(msg)


def decide(draft: AuthoredDraft, item_id: str, decision: Decision) -> AuthoredDraft:
    """Accept, redact or hoist one item (M13.6.3).

    One item at a time, and the id is the argument rather than the text. There is no function
    here that disposes of more than one item, and that absence is M13.6.4 rather than an
    omission: see `A_DEFAULT_IS_A_DECISION_NOBODY_MADE`.

    Returns a new draft. Deciding again over the same item replaces the earlier decision,
    which is what changing your mind is; the record of what was decided in the end is what
    `AuthoredTemplate` carries.
    """
    item = draft.report.item(item_id)
    _check_hoist(draft, item, decision)
    return AuthoredDraft(
        author_id=draft.author_id,
        manifest=draft.manifest,
        decisions={**draft.decisions, item_id: decision},
    )


# ------------------------------------------------------------------ publishing (M13.6.4)
def _is_word_character(character: str) -> bool:
    return bool(character) and (character.isalnum() or character == "_")


def _one_replacement(text: str, original: str, replacement: str) -> str:
    """Replace one value inside one string, on word boundaries where the value has them.

    The boundaries matter and their absence is a real bug rather than a nicety: redacting the
    department `web` from a persona without them turns every `website` into `[redacted]site`,
    and the author accepted a paragraph that no longer says what they read.

    The replacement is supplied through a function so `re.sub` never reads a backslash or a
    group reference inside it as syntax.
    """
    if text == original:
        return replacement
    pattern = re.escape(original)
    if _is_word_character(original[:1]):
        pattern = r"(?<![A-Za-z0-9_])" + pattern
    if _is_word_character(original[-1:]):
        pattern = pattern + r"(?![A-Za-z0-9_])"
    return re.sub(pattern, lambda _match: replacement, text)


def _replace(text: str, substitutions: Mapping[str, str]) -> str:
    """Every substitution, longest original first.

    Longest first so a decision about a whole paragraph subsumes the decisions about the
    spans inside it rather than racing them.
    """
    out = text
    for original in sorted(substitutions, key=len, reverse=True):
        out = _one_replacement(out, original, substitutions[original])
    return out


def _substituted(
    value: JsonValue,
    location: str,
    substitutions: Mapping[str, str],
    untouched: frozenset[str],
) -> JsonValue:
    """One manifest path with every redaction and hoist applied.

    `untouched` holds the locations of classified literals, which are left exactly as they
    are. Without it a redaction of the word `main` would rewrite the model tier, and a
    template would arrive routed to a pool that classifies to nothing.
    """
    if isinstance(value, str):
        return value if location in untouched else _replace(value, substitutions)
    if isinstance(value, list):
        return [
            _substituted(item, f"{location}/{index}", substitutions, untouched)
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        return {
            name: _substituted(item, f"{location}/{name}", substitutions, untouched)
            for name, item in value.items()
        }
    return value


def _decided(draft: AuthoredDraft) -> tuple[dict[str, str], list[Placeholder]]:
    """The substitutions the decisions imply, and the questions the hoists declare."""
    substitutions: dict[str, str] = {}
    hoisted: list[Placeholder] = []
    for item in draft.report.items:
        decision = draft.decisions[item.item_id]
        if decision.disposition is Disposition.ACCEPT:
            continue
        if decision.disposition is Disposition.REDACT:
            substitutions[item.text] = REDACTION_MARK
            continue
        substitutions[item.text] = f"{{{{{decision.placeholder_key}}}}}"
        hoisted.append(Placeholder(key=decision.placeholder_key, prompt=decision.prompt))
    return substitutions, hoisted


def _check_nothing_new(
    document: Mapping[str, JsonValue], draft: AuthoredDraft, hoisted: Sequence[Placeholder]
) -> None:
    """Re-scan the published body and refuse a value no accepted item accounts for.

    This is what closes the one route text has into a body after the report was read: the
    question an author types when hoisting. A prompt reading "who replaces bob@acme.example?"
    is prose, and the email inside it is a shaped literal that no accepted item accounts for.

    `PROSE` items are exempt, and the exemption is forced rather than chosen. Redacting a
    value inside a paragraph changes the paragraph, so its text is not the text the author
    accepted and it never can be. What is checked is every shaped literal and every
    structured scalar, which is where a value that means something to a company actually
    sits. See `NOTHING_ENTERS_THE_BODY_AFTER_THE_REPORT`.
    """
    accepted = {
        item.text
        for item in draft.report.items
        if draft.decisions[item.item_id].disposition is Disposition.ACCEPT
    }
    # A hoisted key is a label the author typed as a question's name, and it is a string in
    # the body like any other. It is allowed because it is the one thing a hoist has to add.
    allowed = accepted | {placeholder.key for placeholder in hoisted}
    for item in scan(document).items:
        if item.kind is LiteralKind.PROSE or item.text in allowed:
            continue
        msg = (
            f"{item.text!r} is in the published body at {item.locations[0]} and no accepted "
            f"item accounts for it. {NOTHING_ENTERS_THE_BODY_AFTER_THE_REPORT}"
        )
        raise AuthoringError(msg)


@dataclass(frozen=True)
class AuthoredTemplate:
    """A published client-authored template, and the record of what was decided about it.

    `report` and `decisions` are carried rather than recomputed. The report belongs to the
    body *before* the redactions, so scanning the published manifest would produce a
    different one, and the question a reviewer asks afterwards is what was in front of the
    author when they pressed accept.

    `visibility` sits beside `signed` rather than inside it, which is what makes M13.6.4's
    ordering argument true: the body is fixed by the signature and the visibility is not, so
    raising a private template to the catalogue never re-opens the leak question and the leak
    question therefore has to be closed before the first signature.
    """

    signed: SignedManifest
    visibility: TemplateVisibility
    author_id: str
    report: LeakReport
    decisions: Mapping[str, Decision]

    @property
    def template_id(self) -> str:
        return self.signed.manifest.identity.template_id

    @property
    def may_leave_this_installation(self) -> bool:
        return may_leave_this_installation(self.visibility)


def publish_authored(
    draft: AuthoredDraft,
    *,
    key: str,
    at: datetime,
    visibility: TemplateVisibility = SAVED_VISIBILITY,
) -> AuthoredTemplate:
    """Sign an authored template, once every item has been dispositioned (M13.6.4).

    The order is the argument.

    **Every item is checked before anything is applied.** A publish that redacted what it
    could and reported the rest would leave a half-processed body that looks published.

    **The same completeness is required at every visibility, private included.** Visibility
    is a property of the offer and the body is a property of the signature, so a private
    template raised to the catalogue a month later is never re-scanned. Allowing private to
    skip the report would put the catalogue two steps away with the report skipped in the
    first one.

    **The manifest is rebuilt and revalidated rather than patched.** A redaction can produce a
    value a grammar refuses: `[redacted]` is not a slug, not a capability and not a skill
    name. Rebuilding through `TemplateManifest.model_validate` means that arrives as a
    refusal naming the field rather than as a manifest nothing can install, which is the
    argument `brain.agents.template._with_overlay` makes about the same choice.

    **The body is scanned again afterwards**, per `NOTHING_ENTERS_THE_BODY_AFTER_THE_REPORT`.

    Signed by the author, and there is no parameter for a different signer. An authored
    template attributed to somebody who did not read the report would be provenance saying
    the wrong person looked.
    """
    undecided = draft.undecided
    if undecided:
        listed = ", ".join(f"{item.item_id} ({item.kind}: {item.text!r})" for item in undecided)
        msg = (
            f"{len(undecided)} literal(s) have no decision against them and this template "
            f"cannot be published: {listed}. {A_DEFAULT_IS_A_DECISION_NOBODY_MADE}"
        )
        raise UndispositionedError(msg)

    report = draft.report
    substitutions, hoisted = _decided(draft)
    untouched = frozenset(literal.location for literal in report.passed)
    # Keyed by `MANIFEST_PATHS` rather than by whatever the draft's document happens to
    # hold, so a path arriving from anywhere else is dropped rather than carried. Said
    # honestly: this is equivalent today and a mutation swapping it for `body.items()`
    # survives the suite, because `TemplateManifest.document()` walks the same tuple, so the
    # two key sets cannot differ. It is written this way for the day a document reaches here
    # from something other than that method, and the refusal that is actually load-bearing
    # is `extra="forbid"` on the model below.
    body = draft.document
    document: dict[str, JsonValue] = {
        path: _substituted(body[path], path, substitutions, untouched) for path in MANIFEST_PATHS
    }

    declared = document["placeholders"]
    if not isinstance(declared, list):
        msg = "placeholders must be a list; a manifest of another shape is not a manifest"
        raise AuthoringError(msg)
    document["placeholders"] = [*declared, *(p.model_dump(mode="json") for p in hoisted)]

    data: dict[str, Any] = draft.manifest.model_dump(mode="json")
    for path, value in document.items():
        head, _, tail = path.partition(".")
        if tail:
            section = dict(data[head])
            section[tail] = value
            data[head] = section
        else:
            data[head] = value
    try:
        manifest = TemplateManifest.model_validate(data)
    except ValidationError as error:
        msg = (
            "applying these decisions produces something that is not a manifest: a redacted "
            "or hoisted value cannot sit at a field whose shape is part of the grammar. "
            f"Accept it, or change the agent before saving it. {error}"
        )
        raise AuthoringError(msg) from error

    _check_nothing_new(manifest.document(), draft, hoisted)
    return AuthoredTemplate(
        signed=publish(manifest, key=key, signed_by=draft.author_id, at=at),
        visibility=visibility,
        author_id=draft.author_id,
        report=report,
        decisions=dict(draft.decisions),
    )


def set_visibility(authored: AuthoredTemplate, visibility: TemplateVisibility) -> AuthoredTemplate:
    """Move a published template between the three levels (M13.6.6).

    Carries the same `SignedManifest` object rather than re-signing, which is the property
    rather than an economy: the body a person read and dispositioned is the body every
    visibility of this template has, and there is no arrangement in which raising the level
    changes a value.

    Lowering is allowed and is not a special case. Withdrawing a template from the catalogue
    is the operation somebody needs at the moment they find something in it, and a level that
    could only be raised would make the discovery unactionable.

    Who moved it is not recorded here, and that is a gap said plainly rather than papered
    over: it belongs in the ledger, nothing in this module writes one, and a field nobody
    writes would read as a record that exists.
    """
    return AuthoredTemplate(
        signed=authored.signed,
        visibility=visibility,
        author_id=authored.author_id,
        report=authored.report,
        decisions=authored.decisions,
    )


def offer_for(authored: AuthoredTemplate) -> Offer:
    """The authored template as something `brain.agents.install` can put in front of somebody.

    A real `Offer`, so an authored template travels the same path a published one does:
    `TemplateCatalogue.offer`, `open_for`, `begin`, `answer`, `provide`, `complete`. There is
    no second install path here and there must not be one, for the reason
    `ONE_FLOW_FOR_A_TEMPLATE_AND_A_HAND_BUILT_AGENT` gives about the wizard.

    The audience is who may install it here and says nothing about what the agent it becomes
    may reach; see `A_TEMPLATE_VISIBILITY_IS_NOT_A_REACH`.
    """
    return Offer(
        signed=authored.signed,
        audience=offer_audience(authored.visibility, author_id=authored.author_id),
    )
