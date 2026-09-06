"""A template is a signed manifest; an agent is that manifest pinned, overlaid and hashed.

`brain.agents.model` says what an agent *is*: an audience, an authority, a persona and a
tier, with `AUDIENCE_IS_NOT_AUTHORITY` holding the two apart. This module says where an
agent *comes from*, and it adds exactly one idea to that record: an agent is a manifest
somebody published plus the local edits somebody made, and both halves are named.

**A manifest is signed, versioned and immutable, and only one of those three is enforced
here.** The version and the signature are values on `SignedManifest`; the immutability is a
grant. `agent.template_version` is granted SELECT and INSERT and nothing else, so a
published manifest cannot be amended by the application at all, and a new version is a new
row. A frozen pydantic model would be immutability inside one process, which is the half
that was never in doubt. See `A_PUBLISHED_MANIFEST_IS_INSERTED_AND_NEVER_AMENDED`.

**The digest is checked without a key and the signature is checked with one, and they
answer different questions.** `SignedManifest` recomputes `content_digest` from the manifest
body at construction and refuses a mismatch, so a row whose document was edited after
signing cannot be loaded at all. That is integrity against accident. `verify` compares the
signature with `hmac.compare_digest` and is what `install` calls, so a manifest from
somewhere else cannot be installed. That is integrity against a person. Neither is the
other: the digest check passes for a manifest signed with the wrong key, and the signature
check would pass for an edited body if the digest were not checked first.

**An instance is a pin plus an overlay, and the pin is three fields rather than one.**
`template_id`, `version` and `content_digest` are all carried, because each catches a
different failure. The id catches an instance materialised against another template
entirely; the version is what makes an upgrade detectable, which is the whole of M13.4; and
the digest catches the case the first two cannot see, a manifest republished under the same
version with a different body. `materialise` refuses when any of the three disagrees.

**Five paths are sealed, and the check constraint is the enforcement point.** `SEALED_PATHS`
names them and `check_overlay` refuses them, but that refusal is a message rather than a
fence: it runs only for a caller who came through this module, and an `UPDATE
agent.template_instance SET overlay = ...` in a psql session during an incident does not.
The fence is `sealed_paths_are_absent` in `brain.tables.template`, and the argument for
which five is below.

**The seal has to hold however the path is written**, which is the part that is easy to get
subtly wrong. Sealing `guardrails.leash` and checking for that exact key leaves three ways
through: `guardrails` sets the whole section, `guardrails.leash.0.rung` sets a leaf inside
it, and `guardrails.leash ` sets it with a space. None of those equals a sealed path. What
closes all three is not a longer deny list, it is the companion rule that an overlay key
must be one of the twelve settable paths and nothing else: every one of those spellings is
outside that set. Both constraints ship, and they are not two copies of one rule. The
settable-path rule admits sealed paths, because a sealed path is a real path; the sealed
rule admits unknown ones. The seal must not depend on the settable list being right, which
is exactly the mistake a single constraint would bake in.

**Which five, and why not the authority section.** The five are `identity.template_id`,
`identity.version`, `identity.published_by`, `guardrails.max_side_effect` and
`guardrails.leash`. The first three make the pin and the provenance mean something: an
overlay that could rewrite them would leave an instance claiming to be an install of a
template it is not, and M13.4's upgrade diff would compare against the wrong lineage. The
last two are the template author's supervision decision, which is the thing an installer
must not be able to relax locally: the largest side effect a run may have, and the rungs it
is held to.

Sealing the whole authority section instead was rejected, and the argument is worth keeping
because it reads as the safer choice. It buys less than it looks: a local admin can already
write any ceiling they like through `brain.agents.model`, so sealing `authority.scope` and
`authority.capabilities` stops nobody and only moves the same edit one screen away. And it
would make M13.2.7 impossible, because a hand-built agent starting from the blank template
would then be permanently unable to hold a capability or a tool. What sealing the two
guardrail paths buys is real and different: it cannot be reached from `brain.agents.model`
at all, because the leash is a separate object and `max_side_effect` on a hand-built agent
comes from the blank template's floor.

**The blank template is the strictest template there is, not the emptiest.** M13.2.7 asks
for one code path, and the way to get one is for a hand-built agent to be an install of
`BLANK_MANIFEST` with everything it needs in the overlay. That is only safe because the
sealed paths are sealed: the blank template's `max_side_effect` is `NONE` and its leash is
empty, so a hand-built agent reads and never sends, and `Leash.rung_for` returns SHADOW for
every target it is asked about. A hand-built agent is therefore the most supervised agent in
the estate by construction, and nothing in an overlay can change that. Had the authority
section been the sealed set, the blank template would have had to be the *widest* template
to be usable, which is the same mechanism failing in the opposite direction.

**The effective document is what the agent is, and its hash is what the cache believes.**
`materialise` applies the overlay to the manifest, revalidates the whole thing, and hashes
the flattened result. The hash is over the *effective* document and never over the manifest,
and the difference is the one that matters: hashing the manifest would give two instances of
one template the same key, so an answer computed under one instance's persona would be
served to the other and both would look fresh. The overlay is inside the digest for the same
reason. `A_CONFIG_HASH_THAT_IGNORES_THE_OVERLAY_SERVES_ANOTHER_AGENTS_ANSWER` states it and
`tests/unit/test_agent_template.py` drives the real `brain.gate.cache_key.key_for` with two
overlays rather than comparing digests to each other.

**Ownership is provenance and is deliberately outside the hash.** `ownership` answers "who
last set this field", per path, and two instances holding the same values set by different
people are the same agent: they answer identically and may share a cached answer. Putting
the owner into the digest would empty the cache on an ownership transfer that changed
nothing anybody can observe. It also means a forged `field_owners` row misreports who typed
a value and cannot change what the agent is, because `materialise` reads the manifest and
the overlay and never reads the ownership map.

**Every collection that is logically a set is stored sorted.** `frozenset` and `set`
serialise in an order that depends on the process's hash seed, so a digest taken over a
dumped set is stable within one worker and different in the next one, and the failure is a
cache that misses for half the fleet while every test on one machine passes. The tool
lists, the capabilities, the connectors and the skill references are normalised to sorted,
deduplicated tuples on the way in, and converted back to the frozensets `AgentAuthority`
takes at materialisation.

**A template carries no audience, and that is the same rule one layer up.** There is no
visibility level anywhere in a manifest and `materialise` is handed an `AgentAudience` by
its caller. A published template that carried one would be publishing an agent into a
company it has never seen, which is `AUDIENCE_IS_NOT_AUTHORITY` restated for a thing that
travels between installations. The instance does not store an audience either: it lives in
`agent.agent`, in one place, so there is no second copy to disagree.

Two further designs were rejected.

*Storing the effective document only, without the manifest and the overlay.* It is one
column instead of three and it loses the question everybody asks at review time, which is
which values came from the template and which somebody changed here. M13.4's three-column
diff cannot be computed from a materialised document alone.

*A `is_customised` boolean beside the instance.* It says one bit about a set of paths and
the first question after reading it is which ones, so it is a summary of the thing that
should have been recorded instead. `ownership` records the thing.

**What consults this, and what does not yet.** `materialise` builds a real `AgentRecord`
through its own constructor, so every validator in `brain.agents.model` runs on the result,
and the record it returns is what `visible_to`, `runnable_agent_ids`, `tool_ceiling` and
`entitlement_ceiling` already take. It builds a real `brain.gate.leash.Leash` out of real
`LeashEntry` objects, and real `brain.tools.skills.SkillPin` objects. `config_hash` is the
producer for `brain.gate.cache_key.key_for`'s `agent_config_hash` argument, which nothing in
`src` produced before this module: the parameter existed and every caller was a test.

No HTTP route calls `install`, and the reason is not this leaf. The install wizard is
M13.3, the upgrade path is M13.4, and there is no route behind the gate anywhere in this
repository. Inventing one here would be a second pipeline for the real one to be reconciled
with, which is what `brain.agents.model` refused for the same reason.

Task ids: M13.2.1, M13.2.2, M13.2.3, M13.2.4, M13.2.5, M13.2.6, M13.2.7
"""

from __future__ import annotations

import enum
import hashlib
import hmac
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from brain.agents.model import (
    AGENT_ID_CHARS,
    DISPLAY_NAME_CHARS,
    OWNER_ID_CHARS,
    PERSONA_CHARS,
    AgentAudience,
    AgentAuthority,
    AgentRecord,
)
from brain.audit.ledger import DIGEST
from brain.core.department import SLUG_PATTERN
from brain.core.entitlement import Capability
from brain.core.envelope import OBJECT_NAME_PATTERN, SideEffect
from brain.core.scope import Scope
from brain.gate.injection import AutonomyTier
from brain.gate.leash import TARGET, Leash, LeashEntry
from brain.models.routing import DEFAULT_TIER, Tier
from brain.tools.skills import SKILL_NAME_RE, SkillPin

# ------------------------------------------------------------------ written-down reasons

#: Why immutability is a grant rather than a frozen class.
A_PUBLISHED_MANIFEST_IS_INSERTED_AND_NEVER_AMENDED: Final = (
    "A published manifest is a promise somebody signed, and an amended one is a different "
    "promise wearing the same version number. Every instance pinned to that version would "
    "silently start materialising from the new body, with no upgrade badge, no diff and "
    "nobody asked. So the enforcement is the privilege: agent.template_version is granted "
    "SELECT and INSERT and never UPDATE, and no policy on it admits an update either. A "
    "frozen model would be immutability inside one process, which was never the half in "
    "doubt, and it would say nothing at all to a psql session."
)

#: Which of the two seal enforcement points is the real one.
THE_CHECK_CONSTRAINT_IS_THE_SEAL_AND_THE_VALIDATOR_IS_THE_MESSAGE: Final = (
    "check_overlay refuses a sealed path with a sentence a person can act on, and it runs "
    "only for a caller who came through this module. The seal itself is the check "
    "constraint on agent.template_instance, because the write that matters is the one "
    "nobody reviewed: a seed script, a console fix applied by hand at two in the morning, "
    "an UPDATE run to unblock a release. A seal that lives only in a validator is a seal "
    "that a direct write goes around, and the direct write is the one nobody remembers."
)

#: Why the seal is paired with a rule about what an overlay may mention at all.
A_SEALED_PATH_HAS_MORE_THAN_ONE_SPELLING: Final = (
    "An overlay keyed by dotted path can reach a sealed value without ever writing it: "
    "'guardrails' sets the whole section, 'guardrails.leash.0.rung' sets a leaf inside it, "
    "and a trailing space makes a third string that is not the sealed one. Checking the "
    "five exact keys catches none of those. What catches them is the companion rule that "
    "an overlay may only mention one of the settable paths, which every such spelling "
    "fails. Both constraints ship: the settable rule admits sealed paths because they are "
    "real paths, the sealed rule admits unknown ones, and neither implies the other."
)

#: Why the digest is taken over the effective document rather than over the manifest.
A_CONFIG_HASH_THAT_IGNORES_THE_OVERLAY_SERVES_ANOTHER_AGENTS_ANSWER: Final = (
    "The configuration hash is one of the five parts of a cache key, and it is the part "
    "that says which agent asked. Taken over the manifest, every instance of one template "
    "shares it, so an answer produced under one instance's persona, tools and tier is "
    "served to another instance under a key that says it is fresh. Nothing fails, nobody "
    "is told, and the answer is wrong for exactly the reason the reader cannot see. The "
    "digest is therefore taken over the materialised document, overlay included."
)

#: Why who set a field is not part of what the agent is.
OWNERSHIP_IS_PROVENANCE_AND_NEVER_CONFIGURATION: Final = (
    "Per-path ownership answers who last set each field. It does not answer what the agent "
    "does: two instances holding identical values set by different people answer "
    "identically and may share a cached answer, so the ownership map is outside the "
    "configuration hash. It is also never read by materialise, which computes the "
    "effective document from the manifest and the overlay alone, so a forged ownership "
    "row misreports who typed a value and cannot change what the agent is."
)

#: Why the blank template is safe to make the one code path.
THE_BLANK_TEMPLATE_IS_THE_STRICTEST_ONE: Final = (
    "A hand-built agent is an install of the blank template, so there is one code path "
    "from a manifest to an AgentRecord rather than two. That is only safe because the "
    "blank template's sealed values are the floor: max_side_effect is none and the leash "
    "is empty, so Leash.rung_for answers SHADOW for every target and no overlay can raise "
    "either. A hand-built agent is the most supervised agent in the estate, by "
    "construction, and it stays that way until somebody publishes a template that says "
    "otherwise."
)

# ------------------------------------------------------------------------ the vocabulary

#: Domain separation for the manifest digest. Changing the covered fields changes every
#: digest ever computed, so a signature raised before the change stops matching after it.
#: That is the correct failure, and it is a migration rather than an edit to this line.
MANIFEST_SCHEMA: Final = "brain.template.manifest.v1"

#: Domain separation for the configuration hash, and deliberately not the line above.
#: An instance with an empty overlay has an effective document identical to its manifest's,
#: so with one schema string the two digests would be the same 64 characters. A cache key
#: could then be built out of a content digest by mistake and look entirely correct.
CONFIG_SCHEMA: Final = "brain.template.effective.v1"

#: Who publishes the blank template. Not a person: nobody authored it and an id that reads
#: as one would put a name against a decision nobody made.
SYSTEM_PUBLISHER: Final = "system"

#: The template id every hand-built agent installs.
BLANK_TEMPLATE_ID: Final = "blank"

#: A summary is a sentence in a picker, not a description. The persona is the description.
SUMMARY_CHARS: Final = 240

#: A placeholder's question to the installing person: "which price list?", "who is the
#: escalation contact?". Bounded like a form label because that is what it is.
PROMPT_CHARS: Final = 200

#: A golden-set question and what a correct answer to it looks like, both bounded so a
#: manifest stays reviewable in one sitting.
GOLDEN_CHARS: Final = 500

#: Every path in a manifest document, sorted, and the only keys `document` produces.
#:
#: Written out rather than derived from the model's fields, and the difference is the whole
#: point: derived, this list would follow a new field automatically and the new field would
#: arrive already settable by an overlay, with no decision made and no migration written.
#: A path added here without a migration fails `tests/unit/test_template_tables.py`.
MANIFEST_PATHS: Final[tuple[str, ...]] = (
    "authority.allowed_tools",
    "authority.capabilities",
    "authority.required_tools",
    "authority.scope",
    "connectors",
    "golden_set",
    "guardrails.leash",
    "guardrails.max_side_effect",
    "identity.display_name",
    "identity.published_by",
    "identity.summary",
    "identity.template_id",
    "identity.version",
    "persona",
    "placeholders",
    "skills",
    "tier",
)

#: The five an overlay may not change, however it is written (M13.2.6).
#:
#: See the module docstring for why these five and not the authority section. Sorted, so
#: the rendered check constraint does not depend on the order they were argued in.
SEALED_PATHS: Final[tuple[str, ...]] = (
    "guardrails.leash",
    "guardrails.max_side_effect",
    "identity.published_by",
    "identity.template_id",
    "identity.version",
)

#: Everything else, derived rather than listed. Listing both would let a path be sealed and
#: settable at once, and the row that arrived while they disagreed would be admitted by
#: whichever constraint was checked and refused by the other, which reads as a database
#: fault rather than as the contradiction it is.
SETTABLE_PATHS: Final[tuple[str, ...]] = tuple(p for p in MANIFEST_PATHS if p not in SEALED_PATHS)


class TemplateError(Exception):
    """A refusal to publish, install or materialise.

    Outside the `brain.core.errors` taxonomy, like `AgentError` and `VisibilityError`:
    those five outcomes describe an answer given to a person, and this describes a refusal
    to store or assemble something. Nobody asking a question ever sees it.
    """


class SealedPathError(TemplateError):
    """An overlay that would change one of the five sealed paths.

    Its own type rather than a message on `TemplateError`, because a console has to say
    two different things: "this field cannot be changed here, it comes from the template"
    is an explanation, and "there is no such field" is a bug report.
    """


class FieldSource(enum.StrEnum):
    """Where a path's current value came from. Two members, and there is no third.

    A value is either the template's, meaning nobody here has touched it and an upgrade may
    move it, or this instance's, meaning somebody set it and an upgrade must ask. A third
    member for "the template's, but confirmed here" was rejected: it changes nothing about
    what the agent does and it is the kind of state that ends up meaning "somebody clicked
    a button once".
    """

    TEMPLATE = "template"
    INSTANCE = "instance"


class FieldOwner(BaseModel):
    """Who last set one path, and when (M13.2.4).

    `set_at` is passed in rather than read from a clock here, for the reason
    `brain.gate.provenance` gives: a rule about dates that reads the clock itself cannot be
    tested at its own boundary.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: FieldSource
    set_by: str = Field(min_length=1, max_length=OWNER_ID_CHARS)
    set_at: datetime

    @field_validator("set_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        """A naive ownership timestamp is a silent bug, as it is on `AgentRecord`.

        This is the timestamp an upgrade review reads to decide whether a local edit is
        older than the template version it is being compared against. Hours out in
        whichever direction the host sits, and neither direction announces itself.
        """
        if v.tzinfo is None:
            msg = "an ownership timestamp must be timezone-aware; a naive one is a silent bug"
            raise ValueError(msg)
        return v


# ---------------------------------------------------------------- the manifest (M13.2.2)
def _sorted_unique(values: Iterable[str]) -> tuple[str, ...]:
    """A logically unordered collection, stored in one order.

    Every one of these lists is a set in meaning and a sequence in JSON. Dumping a real
    `frozenset` produces an order that depends on the process's hash seed, so the digest a
    worker computes for a manifest differs from the digest the worker beside it computes
    for the same manifest, and the symptom is a cache that misses for part of the fleet
    while every test on one machine passes.
    """
    return tuple(sorted(set(values)))


class SkillRef(BaseModel):
    """One skill this template needs, pinned by digest rather than by version.

    By digest for the reason `brain.tools.skills.SkillPin` gives: a version is a number an
    author types, and a skill edited without one is the commonest way a reviewed procedure
    changes underneath the agent that was tested against it. `materialise` turns each of
    these into a real `SkillPin` bound to the instance's agent id.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    digest: str = Field(pattern=DIGEST)

    @field_validator("name")
    @classmethod
    def _a_skill_name(cls, v: str) -> str:
        """The skill layer's own grammar, so a name legal here is resolvable there."""
        if not SKILL_NAME_RE.match(v):
            msg = f"skill reference {v!r} is not a skill name"
            raise ValueError(msg)
        return v


class Placeholder(BaseModel):
    """Something the install has to be told, that the template cannot know (M13.3.3).

    Declared here and collected there. A template that hard-coded a price list, an SOP
    location or an escalation contact would be a template that only works in the company it
    was written in, and the failure is silent: the agent answers, confidently, from another
    company's numbers.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1, max_length=60, pattern=OBJECT_NAME_PATTERN)
    prompt: str = Field(min_length=1, max_length=PROMPT_CHARS)
    #: Whether the install may proceed without it. `False` is the exception and it is the
    #: one that needs a reason, because an optional placeholder nobody fills is a template
    #: quietly running with a blank where a number should be.
    required: bool = True


class GoldenCase(BaseModel):
    """One question this template must still answer correctly after it is installed.

    The run is M13.3.4 and M13.3.5, twice: once as the installing principal and once as a
    low-privilege fixture, because a golden set run only as an administrator proves the
    agent works for administrators. What is here is the case, not the runner.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    question: str = Field(min_length=1, max_length=GOLDEN_CHARS)
    #: What a correct answer looks like, in words. Not a regular expression and not an
    #: expected string: a golden set that matches on text is a golden set that fails when
    #: the model rephrases, and the failure reads as a regression when it is not one.
    expectation: str = Field(min_length=1, max_length=GOLDEN_CHARS)


class ManifestIdentity(BaseModel):
    """Which template this is, which version of it, and who published it.

    Three of these five fields are sealed. They are what makes an instance's pin a pin: an
    overlay that could rewrite them would leave a row claiming to be an install of
    something it is not, and the upgrade path would compare it against the wrong lineage.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: One namespace with agents, scopes and tool objects. The pattern is imported rather
    #: than restated so a template id cannot be legal here and illegal in the agent table.
    template_id: str = Field(min_length=2, max_length=AGENT_ID_CHARS, pattern=SLUG_PATTERN)
    #: Integers rather than semantic versions. A template version is an ordering and a
    #: pin, and nothing in the upgrade path asks whether a change was breaking: the diff is
    #: computed per path and shown, which answers that question with the change itself
    #: rather than with a number somebody chose while publishing.
    version: int = Field(ge=1)
    published_by: str = Field(min_length=1, max_length=OWNER_ID_CHARS)
    display_name: str = Field(min_length=1, max_length=DISPLAY_NAME_CHARS)
    summary: str = Field(default="", max_length=SUMMARY_CHARS)


class ManifestAuthority(BaseModel):
    """What a run through an agent built from this template may reach, at most.

    Not sealed, and the module docstring argues why: a local admin can already write any
    ceiling they like through `brain.agents.model`, so sealing this section moves the same
    edit one screen away rather than preventing it, and it would leave a hand-built agent
    permanently unable to hold a capability.

    `scope` defaults to the unrestricted scope, which is not a company-wide grant. It means
    this template narrows no rows of its own, and `E_run` is still an intersection with the
    caller's set. The dangerous default is the other way round.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: Scope = Field(default_factory=Scope)
    capabilities: tuple[Capability, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    required_tools: tuple[str, ...] = ()

    @field_validator("allowed_tools", "required_tools")
    @classmethod
    def _one_order(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        """Sorted and deduplicated. See `_sorted_unique` for what an unsorted set costs."""
        return _sorted_unique(v)

    @field_validator("capabilities")
    @classmethod
    def _one_order_for_capabilities(cls, v: tuple[Capability, ...]) -> tuple[Capability, ...]:
        """The same rule for a list of models, keyed on the value the capability is."""
        return tuple(sorted({c.value: c for c in v}.values(), key=lambda c: c.value))


class LeashRung(BaseModel):
    """One rung, for one target, within one scope, with no agent named yet.

    `brain.gate.leash.LeashEntry` minus its `agent_id`, and the grammar for `target` is
    imported from there so a target legal in a manifest is legal in a leash.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    target: str = Field(pattern=TARGET, max_length=120)
    scope: Scope = Field(default_factory=Scope)
    rung: AutonomyTier


class ManifestGuardrails(BaseModel):
    """The supervision the template's author committed to. Both paths are sealed.

    The leash carries no agent id, because a template does not know which agent it will
    become: `materialise` binds each rung to the instance's id and produces real
    `brain.gate.leash.LeashEntry` objects. Carrying an id here would mean a manifest that
    only works for one install, which is the opposite of a template.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_side_effect: SideEffect = SideEffect.NONE
    leash: tuple[LeashRung, ...] = ()


class TemplateManifest(BaseModel):
    """Everything a template declares, in one value that can be hashed and signed (M13.2.2).

    Identity, persona, skills, tools, connectors, scope, tier, leash, golden set and
    placeholders: the ten things the work breakdown names, in four sections plus six
    top-level fields, laid out so that the five sealed paths are two whole sections'
    worth of leaves and nothing else.

    `persona` may be empty here and may not be empty on an `AgentRecord`. That is the blank
    template: a manifest with nothing to say, whose install must supply the words. The
    refusal happens where it belongs, in `AgentRecord`'s own validator, so a hand-built
    agent with no persona is refused by the same check every other agent goes through.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: ManifestIdentity
    #: Prompt material, stored and never parsed. Anything in here that could decide a
    #: permission would be a permission decided by whoever last edited a text box.
    persona: str = Field(default="", max_length=PERSONA_CHARS)
    tier: Tier = DEFAULT_TIER
    skills: tuple[SkillRef, ...] = ()
    authority: ManifestAuthority = ManifestAuthority()
    #: The connectors an install has to bind before this template is complete. A
    #: declaration of readiness rather than of reach: what a run may touch is the authority
    #: section intersected with the caller's own entitlement, and adding a name here
    #: reaches nothing, it only makes the install ask for one more binding.
    connectors: tuple[str, ...] = ()
    guardrails: ManifestGuardrails = ManifestGuardrails()
    golden_set: tuple[GoldenCase, ...] = ()
    placeholders: tuple[Placeholder, ...] = ()

    @field_validator("connectors")
    @classmethod
    def _one_order(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(v)

    @field_validator("skills")
    @classmethod
    def _one_skill_order(cls, v: tuple[SkillRef, ...]) -> tuple[SkillRef, ...]:
        """Sorted by name and digest, so a manifest listing two skills in either order is
        one manifest with one digest."""
        return tuple(sorted(v, key=lambda s: (s.name, s.digest)))

    def document(self) -> dict[str, JsonValue]:
        """The manifest as a flat map from dotted path to JSON value (M13.2.2).

        Flat rather than nested, because every other mechanism in this module is keyed by
        path: the overlay, the seal, the ownership map and M13.4's per-path diff. One
        representation for all four means there is no place for a nested form and a dotted
        form to disagree about what `guardrails.leash` means.

        Built by walking `MANIFEST_PATHS` rather than by walking the dumped model, so a
        field added to the model without being added to that tuple produces a `KeyError`
        here rather than a document quietly missing a path that a check constraint then
        refuses.
        """
        data: dict[str, Any] = self.model_dump(mode="json")
        document: dict[str, JsonValue] = {}
        for path in MANIFEST_PATHS:
            head, _, tail = path.partition(".")
            section = data[head]
            document[path] = section[tail] if tail else section
        return document


# --------------------------------------------------------- signing and publishing (M13.2.1)
def _digest(parts: Iterable[str]) -> str:
    """Length-prefixed concatenation, then sha256.

    `brain.audit.ledger._digest`'s argument, and it applies here for the same reason:
    joining on a separator lets two different part lists produce one digest the moment any
    part can contain that separator, and here that would mean a signature raised over one
    manifest satisfying another.
    """
    joined = "".join(f"{len(part)}:{part}" for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def canonical(document: Mapping[str, JsonValue]) -> str:
    """One rendering of a document, so one document has one digest.

    Keys sorted, no whitespace, and no ASCII escaping. `sort_keys` is what makes the
    rendering independent of insertion order; `separators` removes the two spaces
    `json.dumps` adds by default, which are invisible in a diff and change every digest.
    """
    return json.dumps(dict(document), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_digest(manifest: TemplateManifest) -> str:
    """The digest a signature is taken over, and the third field of an instance's pin."""
    return _digest((MANIFEST_SCHEMA, canonical(manifest.document())))


def _signature(digest: str, key: str) -> str:
    """HMAC-SHA256 over the digest, domain-separated by the manifest schema.

    Symmetric rather than a public-key signature, because the question this answers is
    "did this installation's own publishing path produce this manifest", and both ends are
    this system. A catalogue shared between companies is a different question with a
    different answer, and it is not this leaf: signing it here with a key both sides hold
    would be a signature that proves nothing about which side wrote it.
    """
    return hmac.new(
        key.encode("utf-8"), f"{MANIFEST_SCHEMA}:{digest}".encode(), hashlib.sha256
    ).hexdigest()


class SignedManifest(BaseModel):
    """A manifest, its digest, and a signature over that digest (M13.2.1).

    The digest is recomputed from the body at construction and a mismatch is refused, so a
    row whose document was edited after signing cannot be loaded into this type at all.
    That check needs no key, which is what makes it worth having: it runs everywhere a
    manifest is read, including in a process that has no signing key to hand.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest: TemplateManifest
    content_digest: str = Field(pattern=DIGEST)
    signature: str = Field(pattern=DIGEST)
    signed_by: str = Field(min_length=1, max_length=OWNER_ID_CHARS)
    signed_at: datetime

    @field_validator("signed_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            msg = "signed_at must be timezone-aware; a naive signing time is a silent bug"
            raise ValueError(msg)
        return v

    def model_post_init(self, _context: object, /) -> None:
        """Refuse a manifest whose body does not produce the digest it carries.

        Recomputed rather than trusted. The alternative is a type that holds a digest
        somebody else calculated, which makes the digest documentation.
        """
        computed = content_digest(self.manifest)
        if not hmac.compare_digest(computed, self.content_digest):
            msg = (
                f"the manifest body digests to {computed} and this record carries "
                f"{self.content_digest}; the body was edited after it was signed"
            )
            raise ValueError(msg)


def publish(
    manifest: TemplateManifest, *, key: str, signed_by: str, at: datetime
) -> SignedManifest:
    """Sign a manifest (M13.2.1).

    Takes the key as an argument and reads nothing from configuration, for the reason
    `CLAUDE.md` gives about policy and clients: a module that fetches its own key cannot be
    tested against a wrong one, and the wrong-key case is the only one that matters.
    """
    if not key:
        msg = (
            "a manifest cannot be signed with an empty key; an empty HMAC key produces a "
            "digest anybody can reproduce, so the signature would be decoration"
        )
        raise TemplateError(msg)
    digest = content_digest(manifest)
    return SignedManifest(
        manifest=manifest,
        content_digest=digest,
        signature=_signature(digest, key),
        signed_by=signed_by,
        signed_at=at,
    )


def verify(signed: SignedManifest, *, key: str) -> None:
    """Refuse a manifest this installation did not sign (M13.2.1).

    `hmac.compare_digest` rather than `==`, which returns as soon as two characters differ
    and leaks the position of the first mismatch through timing. That is
    `brain.channels.slack`'s argument and it holds wherever a secret is compared.

    Raises rather than returning a bool. A caller who forgets to check a `False` installs
    the template anyway, and the failure is a signed catalogue that admits anything.
    """
    expected = _signature(signed.content_digest, key)
    if not hmac.compare_digest(expected, signed.signature):
        msg = (
            f"the signature on {signed.manifest.identity.template_id!r} version "
            f"{signed.manifest.identity.version} was not made by this installation's key"
        )
        raise TemplateError(msg)


# ---------------------------------------------------- the overlay and the seal (M13.2.6)
def check_overlay(overlay: Mapping[str, JsonValue]) -> None:
    """Refuse an overlay that names a sealed path or a path that does not exist.

    **This is the message and not the fence.** See
    `THE_CHECK_CONSTRAINT_IS_THE_SEAL_AND_THE_VALIDATOR_IS_THE_MESSAGE`: the enforcement
    that matters is `sealed_paths_are_absent` on `agent.template_instance`, because this
    function runs only for a caller who came through this module.

    Sealed is checked before unknown, so the sentence a person reads names the seal rather
    than telling them their path does not exist, which would be true of `guardrails` and
    misleading about why.
    """
    for path in sorted(overlay):
        if path in SEALED_PATHS:
            msg = (
                f"{path!r} is sealed by the template and an instance may not change it; "
                "the five sealed paths are the pin and the supervision the publisher signed"
            )
            raise SealedPathError(msg)
        if path not in SETTABLE_PATHS:
            msg = (
                f"{path!r} is not a path this manifest has; an overlay may only mention "
                f"one of {', '.join(SETTABLE_PATHS)}"
            )
            raise TemplateError(msg)


def _with_overlay(manifest: TemplateManifest, overlay: Mapping[str, JsonValue]) -> TemplateManifest:
    """The manifest as the overlay leaves it, revalidated in full.

    Revalidated rather than patched, and that is the load-bearing word. An overlay arrives
    as JSON from a form or from a JSONB column, so `{"tier": "gigantic"}` and
    `{"authority.capabilities": [{"value": "delete:everything"}]}` are both well-formed
    until a model looks at them. Rebuilding the whole manifest runs every validator this
    module has over the result, which is the same argument
    `brain.agents.lifecycle._revalidated` makes about `model_copy`.
    """
    data: dict[str, Any] = manifest.model_dump(mode="json")
    for path in sorted(overlay):
        head, _, tail = path.partition(".")
        if tail:
            section = dict(data[head])
            section[tail] = overlay[path]
            data[head] = section
        else:
            data[head] = overlay[path]
    return TemplateManifest.model_validate(data)


# ------------------------------------------------------------- the instance (M13.2.3)
class TemplateInstance(BaseModel):
    """One install: a pinned manifest reference, an overlay, and who set what (M13.2.3).

    Frozen. Every edit returns a new instance, so both sides of a change are holdable and
    the console can show a proposed overlay beside the live one, which is what M13.4's
    three-column diff needs.

    Carries no audience. The audience lives on `agent.agent` and `materialise` is handed
    one, so there is one visibility level per agent rather than two that can disagree, and
    a template never travels with a company's idea of who may see it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The agent slug this instance materialises into. The same grammar as
    #: `AgentRecord.agent_id`, because it becomes one.
    instance_id: str = Field(min_length=2, max_length=AGENT_ID_CHARS, pattern=SLUG_PATTERN)
    #: The pin, in three parts. Each catches a failure the other two cannot see: the wrong
    #: template, an upgrade nobody noticed, and a version republished with a different body.
    template_id: str = Field(min_length=2, max_length=AGENT_ID_CHARS, pattern=SLUG_PATTERN)
    template_version: int = Field(ge=1)
    content_digest: str = Field(pattern=DIGEST)
    overlay: Mapping[str, JsonValue] = Field(default_factory=dict)
    #: One entry per overlay path, and the constructor refuses any other shape. An overlaid
    #: value with no owner is a local change nobody can be asked about, which is precisely
    #: the row an upgrade review needs to resolve and cannot.
    overlay_owners: Mapping[str, FieldOwner] = Field(default_factory=dict)
    created_by: str = Field(min_length=1, max_length=OWNER_ID_CHARS)

    def model_post_init(self, _context: object, /) -> None:
        check_overlay(self.overlay)
        if set(self.overlay) != set(self.overlay_owners):
            missing = sorted(set(self.overlay) - set(self.overlay_owners))
            spare = sorted(set(self.overlay_owners) - set(self.overlay))
            msg = (
                f"every overlaid path needs an owner and every owner needs a path; "
                f"unowned: {missing}, owning nothing: {spare}"
            )
            raise ValueError(msg)


def install(
    signed: SignedManifest,
    *,
    key: str,
    instance_id: str,
    created_by: str,
    at: datetime,
    overlay: Mapping[str, JsonValue] | None = None,
) -> TemplateInstance:
    """Pin an instance to a manifest and record who set what (M13.2.3, M13.2.4).

    Verifies the signature first, and that is the only caller of `verify` in this module.
    Installing an unverified manifest is how a template that nobody published becomes an
    agent that everybody can start, and the check has to sit at the point where the
    manifest stops being data and starts being configuration.

    Every overlaid path is recorded as owned by `created_by` at `at`. There is no argument
    for setting an owner per path here: the install is one act by one person, and letting a
    caller name a different owner per path would let an install attribute local edits to
    somebody who was not there.
    """
    verify(signed, key=key)
    values = dict(overlay or {})
    check_overlay(values)
    owner = FieldOwner(source=FieldSource.INSTANCE, set_by=created_by, set_at=at)
    return TemplateInstance(
        instance_id=instance_id,
        template_id=signed.manifest.identity.template_id,
        template_version=signed.manifest.identity.version,
        content_digest=signed.content_digest,
        overlay=values,
        overlay_owners=dict.fromkeys(values, owner),
        created_by=created_by,
    )


def set_field(
    instance: TemplateInstance, path: str, value: JsonValue, *, by: str, at: datetime
) -> TemplateInstance:
    """Overlay one path and record who did it (M13.2.4).

    Returns a new instance rather than mutating one, so the value before the edit is still
    holdable when the console renders what changed.
    """
    check_overlay({path: value})
    owner = FieldOwner(source=FieldSource.INSTANCE, set_by=by, set_at=at)
    return instance.model_copy(
        update={
            "overlay": {**instance.overlay, path: value},
            "overlay_owners": {**instance.overlay_owners, path: owner},
        }
    )


def clear_field(instance: TemplateInstance, path: str) -> TemplateInstance:
    """Give one path back to the template (M13.2.4).

    Clearing a path nobody overlaid returns the same instance rather than raising, for the
    reason `brain.agents.lifecycle.enable` gives: the caller asked for a state the record
    is already in, and a retry after a timeout must not fail.

    No owner is recorded, and that is the point of the operation. The path goes back to
    being the template's, so `ownership` reports the publisher again, which is the honest
    answer: after a revert, nobody here is the one who set that value.
    """
    if path not in instance.overlay:
        return instance
    return instance.model_copy(
        update={
            "overlay": {k: v for k, v in instance.overlay.items() if k != path},
            "overlay_owners": {k: v for k, v in instance.overlay_owners.items() if k != path},
        }
    )


def ownership(signed: SignedManifest, instance: TemplateInstance) -> dict[str, FieldOwner]:
    """Who last set each path, for every path a manifest has (M13.2.4).

    Total over `MANIFEST_PATHS` rather than covering only the overlaid ones. A partial map
    would make "this field has no owner" and "this field belongs to the template" the same
    answer, and they are the two answers an upgrade review has to tell apart: the first is
    a gap in the record and the second is a value the upgrade may move without asking.

    The template's paths are attributed to whoever signed the manifest, at the moment they
    signed it, which is the only honest answer available: a manifest records no per-path
    authorship, and inventing one would put a name against a decision it cannot support.
    """
    published = FieldOwner(
        source=FieldSource.TEMPLATE, set_by=signed.signed_by, set_at=signed.signed_at
    )
    return {path: instance.overlay_owners.get(path, published) for path in MANIFEST_PATHS}


# ------------------------------------------------- materialisation and the hash (M13.2.5)
@dataclass(frozen=True)
class EffectiveAgent:
    """What an instance actually is, once the overlay has been applied.

    Every field here is derived from the manifest and the instance, and nothing is stored
    beside them that could disagree: `materialise` is the only constructor and it computes
    all of them in one pass. The two columns on `agent.template_instance` that hold
    `document` and `config_hash` are a cache of this, written together or not at all.
    """

    #: The manifest as the overlay leaves it. Kept, so the console can show what a value
    #: became without recomputing, and so M13.4 can diff it against a newer version.
    manifest: TemplateManifest
    document: Mapping[str, JsonValue]
    #: The 64-character digest that goes into `brain.gate.cache_key.key_for`'s
    #: `agent_config_hash` argument. See
    #: `A_CONFIG_HASH_THAT_IGNORES_THE_OVERLAY_SERVES_ANOTHER_AGENTS_ANSWER`.
    config_hash: str
    #: A real `AgentRecord`, built through its own constructor, so every validator in
    #: `brain.agents.model` has run on it.
    record: AgentRecord
    #: A real `brain.gate.leash.Leash`, bound to this instance's agent id.
    leash: Leash
    #: Real `brain.tools.skills.SkillPin` objects, one per skill the manifest names.
    skill_pins: tuple[SkillPin, ...]
    owners: Mapping[str, FieldOwner]


def config_hash(document: Mapping[str, JsonValue]) -> str:
    """The digest of an effective document.

    Domain-separated from `content_digest` by `CONFIG_SCHEMA`, and the reason is not
    theoretical: an instance with an empty overlay has an effective document identical to
    its manifest's, so with one schema string the two digests would be the same 64
    characters and a content digest could be passed to the cache as a configuration hash
    without anything looking wrong.
    """
    return _digest((CONFIG_SCHEMA, canonical(document)))


def _check_pin(signed: SignedManifest, instance: TemplateInstance) -> None:
    """Refuse to materialise an instance against a manifest it is not pinned to.

    Three comparisons because the pin is three fields, and each catches something the
    others cannot. Without the digest comparison in particular, a version republished with
    a different body would materialise silently against the new one, which is the failure
    the immutable table exists to prevent and this is the second place it is refused.
    """
    identity = signed.manifest.identity
    if identity.template_id != instance.template_id:
        msg = (
            f"{instance.instance_id!r} is pinned to template {instance.template_id!r} and "
            f"this manifest is {identity.template_id!r}"
        )
        raise TemplateError(msg)
    if identity.version != instance.template_version:
        msg = (
            f"{instance.instance_id!r} is pinned to version {instance.template_version} of "
            f"{instance.template_id!r} and this manifest is version {identity.version}; "
            "following a newer version is an upgrade somebody accepts, not a lookup"
        )
        raise TemplateError(msg)
    if not hmac.compare_digest(signed.content_digest, instance.content_digest):
        msg = (
            f"{instance.instance_id!r} is pinned to a body digesting to "
            f"{instance.content_digest} and this manifest digests to {signed.content_digest}; "
            "the version was republished with a different body"
        )
        raise TemplateError(msg)


def materialise(
    signed: SignedManifest, instance: TemplateInstance, *, audience: AgentAudience
) -> EffectiveAgent:
    """The effective agent: manifest, plus overlay, plus a hash over the result (M13.2.5).

    The order is the argument. The pin is checked before anything is read off the manifest,
    the overlay is checked before it is applied, and the document is flattened from the
    *revalidated* effective manifest rather than assembled by merging two dictionaries.
    That last point is what keeps the document and the `AgentRecord` in step: both come out
    of one object, so there is no arrangement in which the hash describes one configuration
    and the record is another.

    `audience` is a parameter and not a field on anything here, which is
    `AUDIENCE_IS_NOT_AUTHORITY` at this layer: there is no path in a manifest and no column
    on an instance that could carry a visibility level, so a template cannot publish itself
    to anybody and an overlay cannot widen who sees an agent.
    """
    _check_pin(signed, instance)
    check_overlay(instance.overlay)
    effective = _with_overlay(signed.manifest, instance.overlay)
    document = effective.document()
    record = AgentRecord(
        agent_id=instance.instance_id,
        display_name=effective.identity.display_name,
        persona=effective.persona,
        tier=effective.tier,
        audience=audience,
        authority=AgentAuthority(
            scope=effective.authority.scope,
            capabilities=effective.authority.capabilities,
            allowed_tools=frozenset(effective.authority.allowed_tools),
            required_tools=frozenset(effective.authority.required_tools),
            max_side_effect=effective.guardrails.max_side_effect,
        ),
        created_by=instance.created_by,
    )
    leash = Leash(
        entries=tuple(
            LeashEntry(agent_id=instance.instance_id, target=r.target, scope=r.scope, rung=r.rung)
            for r in effective.guardrails.leash
        )
    )
    return EffectiveAgent(
        manifest=effective,
        document=document,
        config_hash=config_hash(document),
        record=record,
        leash=leash,
        skill_pins=tuple(
            SkillPin(agent_id=instance.instance_id, skill_name=s.name, digest=s.digest)
            for s in effective.skills
        ),
        owners=ownership(signed, instance),
    )


# ------------------------------------------------------------- the blank template (M13.2.7)
#: The template a hand-built agent installs. Its sealed values are the floor: no side
#: effect, and no leash entry, so `Leash.rung_for` answers SHADOW for every target it is
#: asked about and nothing an overlay can say will change that. See
#: `THE_BLANK_TEMPLATE_IS_THE_STRICTEST_ONE`.
BLANK_MANIFEST: Final = TemplateManifest(
    identity=ManifestIdentity(
        template_id=BLANK_TEMPLATE_ID,
        version=1,
        published_by=SYSTEM_PUBLISHER,
        display_name="Blank",
        summary="An agent built by hand rather than installed from a published template.",
    )
)


def blank_template(*, key: str, at: datetime, signed_by: str = SYSTEM_PUBLISHER) -> SignedManifest:
    """The blank template, signed with this installation's key (M13.2.7).

    A function rather than a constant, because a signature is a value over a key and a
    module-level constant would have to hold one from somewhere. Signed like every other
    manifest so that `install` needs no special case: the whole point of this leaf is that
    a hand-built agent and an installed one travel the same path, and a template that
    skipped verification would be the second path arriving through the back door.
    """
    return publish(BLANK_MANIFEST, key=key, signed_by=signed_by, at=at)


def hand_built(
    *,
    key: str,
    instance_id: str,
    created_by: str,
    at: datetime,
    overlay: Mapping[str, JsonValue],
) -> TemplateInstance:
    """An agent somebody wrote from nothing, as an install of the blank template (M13.2.7).

    There is no second constructor here and no shortcut to `AgentRecord`. The persona, the
    tier, the tools and the ceiling all arrive in the overlay and go through
    `check_overlay`, `_with_overlay` and `materialise` exactly as an installed template's
    local edits do, which is what makes the one code path a fact rather than a claim.

    What the overlay cannot supply is a side effect or a leash rung, because both are
    sealed and the blank template's values for them are the strictest available.
    """
    return install(
        blank_template(key=key, at=at),
        key=key,
        instance_id=instance_id,
        created_by=created_by,
        at=at,
        overlay=overlay,
    )
