"""Publishing is not an upgrade: a version lands on a shelf, and a badge is all that moves.

`brain.agents.template` says an instance is a three-field pin plus an overlay, and
`brain.agents.install` says how one is filled in. This module says what happens when
somebody publishes the next version of a template that is already installed somewhere, and
it adds one idea to those two: **an upgrade is a decision, and until somebody takes it the
running agent has not been touched.**

**Publishing changes nothing observable, and that is a property of the pin rather than a
promise made here.** An instance names a template id, a version and a content digest, and
`materialise` refuses all three ways. So a newly published version cannot reach a running
agent even by accident: the effective document is built from the manifest the pin names,
the configuration hash is taken over that document, and the cache key
`brain.gate.cache_key.key_for` builds is therefore the same string it was before the
publish. `tests/unit/test_agent_upgrade.py` asserts that end to end rather than by
inspection, because if publishing could move a running agent then every other leaf here
would be decoration. What does move is a badge, which is a thing a person has to act on.
See `PUBLISHING_MOVES_A_BADGE_AND_NEVER_A_RUNNING_AGENT`.

**There has to be somewhere the old version still lives, and the catalogue is not it.**
`TemplateCatalogue.offer` replaces the entry for a template id, which is the right shape
for the question it answers, "what does a new install get". It is the wrong shape for an
upgrade, which needs the pinned version and the newest version at once: with the catalogue
alone, publishing version 2 leaves every instance pinned to version 1 unable to materialise
at all, which is not "nothing changed", it is everything broken. `VersionShelf` is the
domain half of `agent.template_version`, keyed by the same pair that table's primary key
is, and `publish_version` writes to both so the two cannot drift.

**A conflict is a path this instance had claimed, and nothing else is one.** Three columns:
what the old template said, what the new template says, and what this install overlaid. A
path the template moved and nobody here overlaid is not a conflict, it is an update, and it
applies on acceptance without being asked about. Showing every changed path would bury the
three that need a decision under fourteen that do not, and the reader who scrolls past
fourteen rows scrolls past the fifteenth too. See
`A_PATH_NOBODY_OVERLAID_IS_AN_UPDATE_AND_NOT_A_CONFLICT`.

**The decision reads the overlay and the attribution reads the ownership map, and that
order is deliberate.** `ownership` is what records who last set each path and it is what
fills in the third column's provenance, but the question "is this path claimed here" is
answered by the overlay, because the overlay is what `materialise` reads.
`OWNERSHIP_IS_PROVENANCE_AND_NEVER_CONFIGURATION` says a forged `field_owners` row
misreports who typed a value and cannot change what the agent is; computing the conflict
set from the ownership map would make that false, because a forged row saying the template
owns a path would turn a conflict into a silent update and the local value would be
overwritten with nobody asked.

**A path where both answers coincide is still a conflict.** If the new template happens to
say what this install already overlaid, the effective value is the same whichever way it
resolves, and it is tempting to drop the row. The decision is not empty: taking the
template gives the path back, so the *next* version moves it silently, while keeping it
local means it conflicts again. That is a real difference, it shows up in `ownership`, and
it is not something to decide on the reader's behalf by comparing two values.

**A decline is pinned to a version and to a body, and it is one row that cannot be
deleted.** Declining is worth having only if it is durable: a decline that is forgotten by
the next publish is a nag, and a nag is how somebody accepts an upgrade they did not read.
So a decline names the version it declined, which means declining version 3 says nothing
about version 4 and the badge comes back when one arrives. It also names the content digest
of what was declined, for the reason the pin carries one: a version republished with a
different body is a body nobody reviewed, and a decline that went on hiding it would be the
seal's failure mode wearing a different hat. `agent.upgrade_decline` is granted SELECT and
INSERT and never UPDATE or DELETE, which is what "forever" means once a row exists. See
`A_DECLINE_IS_PINNED_TO_A_VERSION_AND_A_BODY`.

**An upgrade is not a route around the seal, and it is structural rather than checked.** A
sealed path can never be in an overlay, so it can never be claimed, so it can never be a
conflict, so no resolution can ever name one: `accept` hands its resolution keys to
`check_overlay` and gains no opinion of its own about which paths are sealed, exactly as
`brain.agents.install.plan` does. What the new template says about `guardrails.leash` and
`guardrails.max_side_effect` therefore lands as published, and it lands where a person
reading the report can see it, which is why a changed sealed path is marked rather than
hidden. See `AN_UPGRADE_IS_NOT_A_ROUTE_AROUND_THE_SEAL`.

**Only the newest version is ever offered.** An instance on version 1 with versions 2 and 3
published is offered version 3 and never version 2. Offering the intermediate one would
leave the badge up after it was accepted and would compute the same diff twice, and nobody
has ever wanted to install the second newest thing on purpose. It also means a decline of
the newest version does not fall back to offering an older one. See
`THE_NEWEST_VERSION_IS_THE_ONLY_ONE_OFFERED`.

Four designs were rejected.

*A third resolution that merges the two values.* Merging two JSON values produces a value
neither the publisher nor the installer wrote, and for `authority.allowed_tools` in
particular a union is a widening that nobody chose. Two answers, and the person picks one.

*Re-stamping ownership on a kept local value.* It reads as an audit improvement and it puts
the accepter's name against words somebody else typed. `FieldOwner` answers who last set a
field, and declining to move a value is not setting it. See
`ACCEPTING_AN_UPGRADE_IS_NOT_SETTING_THE_VALUES_IT_KEEPS`.

*A high-water mark column, "declined up to version 3".* One number instead of one row per
decline, and it makes declining version 4 retrospectively decline version 3, which nobody
did. Worse, it is an UPDATE where every other record in this area is an insert, so the
question "who declined this, and when" gets one answer that keeps being overwritten.

*A listing of every instance with an upgrade waiting.* It is the obvious companion to the
badge and it is the one shape that could leak here: a listing across an estate has to apply
`brain.agents.model.visible_to` against the agent records first, and this module holds none
of them, so a listing written here would either take a viewer it cannot check or return
agents a person may not see. Whoever holds the records builds it, and it must carry no
count of what it left out.

**One thing an acceptance does not re-check, said plainly rather than implied.** A new
version can declare a connector the old one did not, or a required placeholder nobody has
answered, and `accept` does not recompute
`brain.agents.install.completeness`. It cannot honestly: `completeness` reads an
`InstallDraft`, and the placeholder answers it needs live only on that draft and on the
`Installation`, because `brain.agents.install` argued against a `placeholder_answers` column
on the grounds that nothing writes the table. Reconstructing a draft from an instance would
mean inventing those answers, and an install reported complete on invented answers is worse
than one that says nothing. `Upgraded` carries the new instance and its `EffectiveAgent`, and
whoever holds the connector registry runs `connector_readiness` and `pinned_leash` over the
result the way `complete` does. This is a gap, not a decision to leave it open for ever.

**What consults this, and what does not.** No HTTP route calls any of it, and there is no
route behind the gate in this repository at all: `brain.agents.model`, `brain.agents.template`
and `brain.agents.install` each refused to invent one, and a second request pipeline
invented here would be a second thing for the real one to be reconciled with. The console
has no agent page either, so nothing renders the badge today. What is wired is real:
`publish_version` is a caller of `brain.agents.install.TemplateCatalogue.offer`, `accept`
is a caller of `brain.agents.template.verify`, `check_overlay` and `materialise`, and
`review` is a caller of `ownership`. Nothing in `src` writes `agent.upgrade_decline`, which
is the state `agent.template_version` and `agent.template_instance` are both already in.

Task ids: M13.4.1, M13.4.2, M13.4.3, M13.4.4, M13.4.5
"""

from __future__ import annotations

import enum
import hmac
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from brain.agents.install import Offer, TemplateCatalogue
from brain.agents.model import AGENT_ID_CHARS, OWNER_ID_CHARS, AgentAudience
from brain.agents.template import (
    MANIFEST_PATHS,
    SEALED_PATHS,
    EffectiveAgent,
    FieldOwner,
    SignedManifest,
    TemplateError,
    TemplateInstance,
    check_overlay,
    materialise,
    ownership,
    verify,
)
from brain.audit.ledger import DIGEST
from brain.core.department import SLUG_PATTERN

# ------------------------------------------------------------------ written-down reasons

#: The whole of M13.4.1, stated where somebody about to add a resolver will read it.
PUBLISHING_MOVES_A_BADGE_AND_NEVER_A_RUNNING_AGENT: Final = (
    "Publishing a new version of a template must change nothing about the agents already "
    "installed from it. An instance is pinned by template id, version and content digest, "
    "so the effective document, the configuration hash taken over it and the cache key "
    "built from that hash are all the same values after a publish as before one. The only "
    "thing that moves is a badge, which is a sentence a person has to act on. A resolver "
    "that looked up the newest version instead of the pinned one would upgrade every "
    "install in the estate with no diff, no acceptance and nobody told."
)

#: Why the diff is short, and what the word conflicting is doing.
A_PATH_NOBODY_OVERLAID_IS_AN_UPDATE_AND_NOT_A_CONFLICT: Final = (
    "A conflict is a path the new template moved that this install had already claimed. A "
    "path the template moved and nobody here overlaid is an update: it applies on "
    "acceptance and there is nothing to decide about it. Listing both as decisions buries "
    "the three that matter under the fourteen that do not, and a review of seventeen rows "
    "is a review nobody reads to the end. Whether a path is claimed is read from the "
    "overlay, because the overlay is what materialise reads; the ownership map supplies "
    "who set it and when, and never the verdict."
)

#: Why a sealed path can never be resolved, and why that needs no check of its own.
AN_UPGRADE_IS_NOT_A_ROUTE_AROUND_THE_SEAL: Final = (
    "The five sealed paths cannot appear in an overlay, so they cannot be claimed by an "
    "install, so they can never be conflicts and no resolution can name one. An upgrade "
    "therefore takes the new template's supervision exactly as published: the largest side "
    "effect a run may have and the rungs it is held to are the publisher's decision at "
    "both versions. accept hands its resolution keys to check_overlay rather than "
    "comparing them against SEALED_PATHS, so this module holds no second opinion about "
    "which paths are sealed, and a changed sealed path is marked in the report rather than "
    "hidden, because it is the one change an accepter cannot resolve and must read."
)

#: Why the resolution map has to cover the conflicts exactly.
A_RESOLUTION_IS_REQUIRED_FOR_EVERY_CONFLICT_AND_FOR_NOTHING_ELSE: Final = (
    "An upgrade is opt-in per path or it is not opt-in at all. A missing resolution would "
    "have to be given a default, and either default is somebody's local edit thrown away "
    "or a template's change silently ignored, decided by whoever wrote the default rather "
    "than by the person accepting. A resolution for a path that is not in conflict is a "
    "decision about something nobody was shown, which is the same failure from the other "
    "end: it means the caller and the report disagree about what the upgrade is."
)

#: Why keeping a value is not setting it.
ACCEPTING_AN_UPGRADE_IS_NOT_SETTING_THE_VALUES_IT_KEEPS: Final = (
    "A kept local value carries the FieldOwner it already had. The person accepting the "
    "upgrade declined to move that value; they did not write it, and FieldOwner answers "
    "who last set a field. Re-stamping it would put the accepter's name and the accepting "
    "moment against words somebody else typed, and it would destroy the one comparison an "
    "upgrade review needs next time, which is whether a local edit is older than the "
    "template version it is being compared against. A path resolved the other way keeps no "
    "owner at all, because it goes back to the template and the publisher owns it again."
)

#: Why a decline names a version and a body rather than a template.
A_DECLINE_IS_PINNED_TO_A_VERSION_AND_A_BODY: Final = (
    "A decline of version 3 says nothing about version 4, so the badge returns when a new "
    "version is published and stays down for ever otherwise. Pinned to a template instead, "
    "one decline would silence every future version, which is not a decline, it is an "
    "install nobody will ever look at again. The declined content digest is carried for "
    "the reason the pin carries one: a version republished with a different body is a body "
    "nobody reviewed, and a decline that went on hiding it would turn a refusal to read "
    "into a refusal to be shown."
)

#: Why the offer is always the newest version and never an intermediate one.
THE_NEWEST_VERSION_IS_THE_ONLY_ONE_OFFERED: Final = (
    "An instance on version 1 with versions 2 and 3 published is offered version 3. "
    "Offering version 2 would leave the badge up the moment it was accepted, compute the "
    "same diff a second time, and ask a person to read two reviews to arrive where one "
    "would have put them. It also means declining the newest version does not fall back to "
    "offering an older one, which would be a nag wearing the word choice."
)


# ------------------------------------------------------------------------ the vocabulary
class UpgradeBadge(enum.StrEnum):
    """Whether a newer version is waiting on this instance. Three states, and no fourth.

    `CURRENT` and `AVAILABLE` are the two an install can be in with nobody having decided
    anything. `DECLINED` is its own state rather than a flag beside `AVAILABLE`, because a
    console that painted the same amber for both would be nagging about the decision it was
    told not to nag about, and a badge that comes back after somebody says no is a badge
    people learn to click through.

    A colour is deliberately not in here, as it is not in `brain.agents.install.InstallBadge`:
    the state is the thing worth naming and how it is drawn belongs to whoever draws it.
    """

    #: Pinned to the newest published version. There is nothing to show.
    CURRENT = "current"
    #: A newer version exists and nobody here has decided about it yet.
    AVAILABLE = "available"
    #: A newer version exists and somebody declined it. Shown quietly or not at all.
    DECLINED = "declined"


class Resolution(enum.StrEnum):
    """What to do with one conflicting path. Two answers, and there is no third.

    A merge was rejected: merging two JSON values produces a value neither the publisher
    nor the installer wrote, and for a tool list in particular a union is a widening that
    nobody chose. The person picks one of the two values in front of them.
    """

    #: Keep what this install overlaid. The overlay entry and its owner stay as they are.
    KEEP_LOCAL = "keep_local"
    #: Take the new template's value. The overlay entry goes, so the path is the
    #: template's again and `ownership` reports the publisher, which is what
    #: `brain.agents.template.clear_field` means by giving a path back.
    TAKE_TEMPLATE = "take_template"


class Decline(BaseModel):
    """One version of one template, declined for one instance, by one person (M13.4.5).

    Frozen, and carrying the same three facts the pin does: which template, which version,
    and the digest of the body that was declined. `brain.tables.upgrade.UpgradeDeclineRow`
    is the durable half and its primary key is the first three of these.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    instance_id: str = Field(min_length=2, max_length=AGENT_ID_CHARS, pattern=SLUG_PATTERN)
    template_id: str = Field(min_length=2, max_length=AGENT_ID_CHARS, pattern=SLUG_PATTERN)
    version: int = Field(ge=1)
    #: The body that was declined, not merely its number. See
    #: `A_DECLINE_IS_PINNED_TO_A_VERSION_AND_A_BODY`.
    content_digest: str = Field(pattern=DIGEST)
    declined_by: str = Field(min_length=1, max_length=OWNER_ID_CHARS)
    declined_at: datetime

    @field_validator("declined_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        """A naive decline time is a silent bug, as it is on `FieldOwner` and `SignedManifest`.

        This is the timestamp that answers "when did we say no to this", which is the first
        question asked when somebody notices an agent is three versions behind.
        """
        if v.tzinfo is None:
            msg = "a decline time must be timezone-aware; a naive one is a silent bug"
            raise ValueError(msg)
        return v

    @property
    def key(self) -> tuple[str, str, int]:
        """The primary key of the row this becomes."""
        return (self.instance_id, self.template_id, self.version)


@dataclass
class Declines:
    """Every upgrade somebody declined, keyed the way the table keys them (M13.4.5).

    An instance rather than a module-level singleton, for the reason
    `brain.agents.install.TemplateCatalogue` gives about its own: a singleton is process
    state in a layer that holds none.

    Holds declines in memory. Nothing in `src` writes `agent.upgrade_decline`, which is the
    state every other table in this area is in, and the durability that makes a decline
    permanent is the table's missing UPDATE and DELETE grants rather than anything here.
    """

    _rows: dict[tuple[str, str, int], Decline] = field(default_factory=dict)

    def record(self, decline: Decline) -> Decline:
        """Store a decline, or return the one already stored for that version.

        Idempotent, for the reason `brain.agents.lifecycle.enable` gives: the caller asked
        for a state the record is already in, and a retry after a timeout must not fail. The
        first decline is the one that happened, so a second by somebody else does not
        overwrite the name on the row.

        Refuses a second decline of the same version carrying a different digest, which is
        the case the digest is here for: two different bodies have worn that version number
        and only one of them was read.
        """
        existing = self._rows.get(decline.key)
        if existing is None:
            self._rows[decline.key] = decline
            return decline
        if not hmac.compare_digest(existing.content_digest, decline.content_digest):
            msg = (
                f"{decline.template_id!r} version {decline.version} was already declined for "
                f"{decline.instance_id!r} with a body digesting to {existing.content_digest} "
                f"and this one digests to {decline.content_digest}; the version was "
                "republished with a different body and the decline covers the first one"
            )
            raise TemplateError(msg)
        return existing

    def applies_to(self, instance: TemplateInstance, candidate: SignedManifest) -> bool:
        """Whether this instance has already declined exactly this candidate.

        Three fields compared, as `brain.agents.template._check_pin` compares three: the
        instance, the version, and the body. A decline recorded against another body under
        the same number does not apply, so a republished manifest is shown again.
        """
        identity = candidate.manifest.identity
        found = self._rows.get((instance.instance_id, identity.template_id, identity.version))
        if found is None:
            return False
        return hmac.compare_digest(found.content_digest, candidate.content_digest)


# --------------------------------------------------------------- where a version lives
@dataclass
class VersionShelf:
    """Every published version of every template, keyed by the pair a pin names.

    The domain half of `agent.template_version`, and the thing `TemplateCatalogue` is not:
    a catalogue answers what a new install gets and holds one entry per template, so with
    the catalogue alone an instance pinned to an older version has nowhere to read its own
    manifest from. Both exist and `publish_version` writes to both.

    Append-only, like the table. `publish` refuses to overwrite a version that is already
    here, which is the domain-side statement of the missing UPDATE grant: a published
    manifest is a promise somebody signed, and an amended one is a different promise wearing
    the same version number.
    """

    _versions: dict[tuple[str, int], SignedManifest] = field(default_factory=dict)

    def publish(self, signed: SignedManifest) -> SignedManifest:
        """Shelve a version, refusing to replace one that is already shelved.

        Keyed off the manifest's own identity rather than off arguments, so a caller cannot
        file a manifest under a template id or a version it does not carry.
        """
        identity = signed.manifest.identity
        key = (identity.template_id, identity.version)
        existing = self._versions.get(key)
        if existing is not None:
            msg = (
                f"{identity.template_id!r} version {identity.version} is already published; "
                "a published manifest is never amended and a new body is a new version, "
                "because every instance pinned to this one would otherwise start "
                "materialising from the new body with nobody asked"
            )
            raise TemplateError(msg)
        self._versions[key] = signed
        return signed

    def versions(self, template_id: str) -> tuple[int, ...]:
        """Every published version of one template, ascending."""
        return tuple(sorted(v for (t, v) in self._versions if t == template_id))

    def newest(self, template_id: str) -> SignedManifest:
        """The highest published version of one template.

        The only version an upgrade is ever offered against; see
        `THE_NEWEST_VERSION_IS_THE_ONLY_ONE_OFFERED`.
        """
        published = self.versions(template_id)
        if not published:
            msg = f"no version of template {template_id!r} has been published here"
            raise TemplateError(msg)
        return self._versions[(template_id, published[-1])]

    def pinned(self, instance: TemplateInstance) -> SignedManifest:
        """The manifest this instance is actually running.

        All three fields of the pin are checked here rather than left to `materialise`,
        because this is the lookup an upgrade review builds its "old template" column from
        and a column read off another body would be a diff against a manifest nobody ran.
        The digest comparison is the one that catches a version republished underneath a
        running install, which `publish` refuses and a direct write to the table does not.
        """
        key = (instance.template_id, instance.template_version)
        found = self._versions.get(key)
        if found is None:
            msg = (
                f"{instance.instance_id!r} is pinned to version {instance.template_version} "
                f"of {instance.template_id!r} and no such version is published here"
            )
            raise TemplateError(msg)
        if not hmac.compare_digest(found.content_digest, instance.content_digest):
            msg = (
                f"{instance.instance_id!r} is pinned to a body digesting to "
                f"{instance.content_digest} and version {instance.template_version} of "
                f"{instance.template_id!r} digests to {found.content_digest}; the version "
                "was republished with a different body"
            )
            raise TemplateError(msg)
        return found


def publish_version(
    signed: SignedManifest,
    *,
    shelf: VersionShelf,
    catalogue: TemplateCatalogue,
    audience: AgentAudience,
) -> Offer:
    """Publish a version: shelve it, and point the catalogue at it (M13.4.1).

    Both halves, in one function, because either alone is wrong in a way nobody notices.
    Shelved and not offered, the version exists and no new install can reach it. Offered and
    not shelved, a new install pins a version that is not on the shelf, so its own upgrade
    review cannot read the manifest it is running and the badge machinery refuses.

    It changes nothing about the installs that already exist, which is the point of the
    leaf: `TemplateCatalogue.offer` replaces an entry in a picker, and every instance is
    pinned by three fields that this does not touch. See
    `PUBLISHING_MOVES_A_BADGE_AND_NEVER_A_RUNNING_AGENT`.

    The audience is the offer's, meaning who may install this template, and it is not the
    audience of any agent: `THE_OFFER_AUDIENCE_IS_NOT_THE_AGENT_AUDIENCE` is the rule and
    `complete` and `accept` both take the agent's audience as their own argument.
    """
    shelf.publish(signed)
    return catalogue.offer(signed, audience=audience)


# ------------------------------------------------------- the three-column diff (M13.4.3)
@dataclass(frozen=True)
class PathChange:
    """One path the new version moves that this install never claimed.

    Two columns rather than three, and that is the difference from `Conflict`: there is no
    local value to show because nobody here set one, so there is nothing to decide and the
    new value applies on acceptance.
    """

    path: str
    #: What the pinned version says. Named `was` because from the reader's seat it is what
    #: their agent has been doing.
    was: JsonValue
    #: What the candidate version says.
    now: JsonValue
    #: Whether this is one of the five paths an overlay may never touch. Marked rather than
    #: hidden: a changed leash or a changed side effect is the one thing in an upgrade an
    #: accepter cannot resolve, so it is the one thing they have to read. See
    #: `AN_UPGRADE_IS_NOT_A_ROUTE_AROUND_THE_SEAL`.
    sealed: bool


@dataclass(frozen=True)
class Conflict:
    """One path the new version moves that this install had already claimed (M13.4.3).

    The three columns, in the order a person reads them: what the template said, what the
    template now says, and what was set here. `owner` is the fourth thing on the row and not
    a fourth column, because it is provenance rather than a value: who set the local one and
    when, which is what decides whether a local edit predates the change being offered.
    """

    path: str
    #: Column one: the pinned version's value.
    was: JsonValue
    #: Column two: the candidate version's value.
    now: JsonValue
    #: Column three: what this install overlaid, as it was written. Read from the overlay
    #: rather than from the effective document, because the question this column answers is
    #: what somebody set here, and for a collection the manifest normalises the document
    #: holds what the model made of it rather than what was typed.
    local: JsonValue
    #: Who last set the local value, from `brain.agents.template.ownership`.
    owner: FieldOwner


@dataclass(frozen=True)
class UpgradeReview:
    """What is waiting for one instance, and what accepting it would do (M13.4.2, M13.4.3).

    Carries the instance and both manifests rather than ids, so `accept` and `decline` take
    only this and cannot be handed a review of one install and the instance of another.

    `conflicts` and `updates` are filled in whatever the badge says, including `DECLINED`. A
    decline is a decision not to be nagged, not a decision never to be shown: somebody who
    opens the record still sees what they turned down, and can still accept it.
    """

    instance: TemplateInstance
    #: The manifest the agent is running now, read off the shelf by the pin.
    pinned: SignedManifest
    #: The newest published version, or `None` when the instance is already on it.
    candidate: SignedManifest | None
    badge: UpgradeBadge
    conflicts: tuple[Conflict, ...]
    updates: tuple[PathChange, ...]

    @property
    def from_version(self) -> int:
        return self.instance.template_version

    @property
    def to_version(self) -> int | None:
        """The version on offer, or `None` when there is not one."""
        return None if self.candidate is None else self.candidate.manifest.identity.version

    @property
    def needs_a_decision(self) -> bool:
        """Whether accepting this requires somebody to resolve anything."""
        return bool(self.conflicts)


def _diff(
    pinned: SignedManifest, candidate: SignedManifest, instance: TemplateInstance
) -> tuple[tuple[Conflict, ...], tuple[PathChange, ...]]:
    """Split every moved path into the ones that need a decision and the ones that do not.

    Walks `MANIFEST_PATHS`, so both lists come back in one order whatever order an overlay
    was written in, and a console renders the same review twice running. Walking the overlay
    or the document's own keys would order the rows by insertion, which changes when
    somebody edits a different field.

    Claimed is read from `instance.overlay` and never from the ownership map. See the module
    docstring: a forged `field_owners` row must not be able to turn a conflict into a silent
    update, because `materialise` reads the overlay and would keep the local value while the
    review said the template's had been taken.
    """
    was = pinned.manifest.document()
    now = candidate.manifest.document()
    owners = ownership(pinned, instance)
    conflicts: list[Conflict] = []
    updates: list[PathChange] = []
    for path in MANIFEST_PATHS:
        if was[path] == now[path]:
            continue
        if path in instance.overlay:
            conflicts.append(
                Conflict(
                    path=path,
                    was=was[path],
                    now=now[path],
                    local=instance.overlay[path],
                    owner=owners[path],
                )
            )
        else:
            updates.append(
                PathChange(path=path, was=was[path], now=now[path], sealed=path in SEALED_PATHS)
            )
    return tuple(conflicts), tuple(updates)


def review(instance: TemplateInstance, *, shelf: VersionShelf, declines: Declines) -> UpgradeReview:
    """The badge and the diff for one install, computed together (M13.4.2, M13.4.3).

    One function for both leaves, because they are one question asked twice and two
    functions would be two ways to answer it: a badge saying nothing is waiting beside a
    diff with rows in it is a screen nobody can act on, and the state that produced it would
    be nobody's bug.

    Writes nothing, anywhere. A review reads the shelf and the decline record and has no
    side effect at all, which is what makes M13.4.1 true from this end as well: looking at
    what an upgrade would do is not accepting it, the pin does not move, and the instance
    handed in comes back untouched because it is frozen.
    """
    pinned = shelf.pinned(instance)
    candidate = shelf.newest(instance.template_id)
    if candidate.manifest.identity.version <= instance.template_version:
        return UpgradeReview(
            instance=instance,
            pinned=pinned,
            candidate=None,
            badge=UpgradeBadge.CURRENT,
            conflicts=(),
            updates=(),
        )
    conflicts, updates = _diff(pinned, candidate, instance)
    badge = (
        UpgradeBadge.DECLINED
        if declines.applies_to(instance, candidate)
        else UpgradeBadge.AVAILABLE
    )
    return UpgradeReview(
        instance=instance,
        pinned=pinned,
        candidate=candidate,
        badge=badge,
        conflicts=conflicts,
        updates=updates,
    )


# ------------------------------------------------------ accepting and declining (M13.4.4)
@dataclass(frozen=True)
class Upgraded:
    """An instance that has moved to a new version, and what it now materialises to.

    The effective agent is built here rather than left to the caller, so an upgrade that
    cannot be materialised is refused before the pin moves. A local value that was legal
    under the old manifest and is not under the new one fails in `_with_overlay`'s
    revalidation, and the version of this that returned an instance alone would hand back a
    row nobody can turn into an agent and no obvious way back.
    """

    instance: TemplateInstance
    effective: EffectiveAgent
    #: Who accepted, and when. Not an owner of anything: see
    #: `ACCEPTING_AN_UPGRADE_IS_NOT_SETTING_THE_VALUES_IT_KEEPS`.
    accepted_by: str
    accepted_at: datetime


def accept(
    reviewed: UpgradeReview,
    *,
    resolutions: Mapping[str, Resolution],
    key: str,
    audience: AgentAudience,
    by: str,
    at: datetime,
) -> Upgraded:
    """Move an instance to the reviewed version, one path at a time (M13.4.4).

    Nothing else in this module moves a pin, so an upgrade happens exactly when somebody
    calls this and never as a consequence of publishing, reviewing or listing.

    The order is the argument.

    **The signature is verified before anything is read**, because this is the moment a new
    manifest stops being data and becomes configuration, which is the same point
    `brain.agents.template.install` makes about the first version. A shelf is a store and
    stores do not check signatures.

    **The resolution keys go through `check_overlay`**, so a resolution naming
    `guardrails.leash` is refused with the seal's own sentence and this module gains no
    opinion about which paths are sealed. See `AN_UPGRADE_IS_NOT_A_ROUTE_AROUND_THE_SEAL`
    and `brain.agents.install.THE_SEAL_IS_NOT_RESTATED_HERE`.

    **The resolutions must cover the conflicts exactly**, per
    `A_RESOLUTION_IS_REQUIRED_FOR_EVERY_CONFLICT_AND_FOR_NOTHING_ELSE`. Both halves are
    refused with the paths named, because both are silent otherwise.

    **A local edit on a path the new version did not touch survives untouched.** It is not a
    conflict, nobody was asked about it, and dropping it would make an upgrade a way of
    quietly reverting local configuration.

    **A kept value keeps its owner and a released one keeps none.** See
    `ACCEPTING_AN_UPGRADE_IS_NOT_SETTING_THE_VALUES_IT_KEEPS`.
    """
    candidate = reviewed.candidate
    if candidate is None:
        msg = (
            f"{reviewed.instance.instance_id!r} is already on version "
            f"{reviewed.from_version} of {reviewed.instance.template_id!r}, which is the "
            "newest published here; there is no upgrade to accept"
        )
        raise TemplateError(msg)
    verify(candidate, key=key)

    # Values are irrelevant; `check_overlay` reads keys. Annotated rather than inlined so the
    # type handed over is the union the domain's validator takes, which is what makes this a
    # call to the seal's own message rather than a shape that happens to fit.
    probe: dict[str, JsonValue] = dict.fromkeys(resolutions)
    check_overlay(probe)

    expected = {conflict.path for conflict in reviewed.conflicts}
    given = set(resolutions)
    if given != expected:
        unresolved = sorted(expected - given)
        unasked = sorted(given - expected)
        msg = (
            f"an upgrade is accepted one conflicting path at a time; {unresolved} were "
            f"shown and not resolved, and {unasked} were resolved and not shown"
        )
        raise TemplateError(msg)

    instance = reviewed.instance
    overlay: dict[str, JsonValue] = {}
    owners: dict[str, FieldOwner] = {}
    for path, value in instance.overlay.items():
        if resolutions.get(path) is Resolution.TAKE_TEMPLATE:
            continue
        overlay[path] = value
        owners[path] = instance.overlay_owners[path]

    identity = candidate.manifest.identity
    upgraded = TemplateInstance(
        instance_id=instance.instance_id,
        template_id=identity.template_id,
        template_version=identity.version,
        content_digest=candidate.content_digest,
        overlay=overlay,
        overlay_owners=owners,
        created_by=instance.created_by,
    )
    return Upgraded(
        instance=upgraded,
        effective=materialise(candidate, upgraded, audience=audience),
        accepted_by=by,
        accepted_at=at,
    )


def decline(reviewed: UpgradeReview, *, declines: Declines, by: str, at: datetime) -> Decline:
    """Say no to this version, for good, without being asked again (M13.4.5).

    Records the version and the body rather than the template, so the badge stays down for
    this version for ever and comes back the day a newer one is published. See
    `A_DECLINE_IS_PINNED_TO_A_VERSION_AND_A_BODY`.

    Refuses when there is nothing on offer. Declining an upgrade that does not exist would
    write a row against the version the instance is already running, and the next publish
    would then arrive already declined.

    Declining does not close the door. `accept` still works on a declined review, because a
    decline is a decision not to be interrupted rather than a decision never to look.
    """
    candidate = reviewed.candidate
    if candidate is None:
        msg = (
            f"{reviewed.instance.instance_id!r} is already on version "
            f"{reviewed.from_version} of {reviewed.instance.template_id!r}; there is no "
            "upgrade to decline, and a decline recorded against the running version would "
            "make the next publish arrive already refused"
        )
        raise TemplateError(msg)
    identity = candidate.manifest.identity
    return declines.record(
        Decline(
            instance_id=reviewed.instance.instance_id,
            template_id=identity.template_id,
            version=identity.version,
            content_digest=candidate.content_digest,
            declined_by=by,
            declined_at=at,
        )
    )
