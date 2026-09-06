"""Versioning and upgrade: a publish that moves nothing, a diff that is short, a decline.

Every test here is a way one of five things could go wrong: a publish could reach an agent
somebody is already using, a badge could appear on the wrong install, a diff could ask about
paths nobody has to decide, an acceptance could change something nobody resolved, or a
decline could come back and ask again.

**The first leaf is asserted end to end and never by inspection.** M13.4.1 is what makes the
other four safe, so the tests for it publish a real second version through the real
catalogue and then compare the effective document, the configuration hash and the key the
real `brain.gate.cache_key.key_for` builds. A test that compared a digest with itself, or
that asserted the pin fields had not moved, would pass for a resolver that looked up the
newest version and rebuilt the agent from it.

**And its positive sibling is here too.** A system where nothing ever changes satisfies
"publishing changes nothing", so `test_accepting_an_upgrade_does_move_the_cache_key` drives
the same key through the same function after an acceptance and requires it to differ.

**The diff tests count both lists.** A conflict list that is right and an update list that
quietly drops a path would leave an accepter agreeing to changes they were never shown, so
`test_every_path_the_new_version_moves_appears_in_exactly_one_of_the_two_lists` walks the
manifest's own path list rather than the report's.

**The seal is tested through a resolution, which is the only new way at it.** An overlay
cannot name a sealed path, so the way an upgrade could become a route around the seal is a
resolution map that names one, and there is a test for exactly that.

The fixtures build two and three real signed versions of one template and install real
instances from them. Nothing here stands in for `materialise`, `ownership`, `verify` or
`check_overlay`: those are the functions this module is a caller of, and a stand-in would
leave these tests asserting that this file's idea of an upgrade is self-consistent.

Task ids: M13.4.1, M13.4.2, M13.4.3, M13.4.4, M13.4.5
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import JsonValue, ValidationError

from brain.agents.install import TemplateCatalogue
from brain.agents.model import AgentAudience, AgentViewer
from brain.agents.template import (
    MANIFEST_PATHS,
    SEALED_PATHS,
    FieldOwner,
    FieldSource,
    GoldenCase,
    LeashRung,
    ManifestAuthority,
    ManifestGuardrails,
    ManifestIdentity,
    Placeholder,
    SealedPathError,
    SignedManifest,
    SkillRef,
    TemplateError,
    TemplateInstance,
    TemplateManifest,
    install,
    materialise,
    ownership,
    publish,
)
from brain.agents.upgrade import (
    Decline,
    Declines,
    Resolution,
    UpgradeBadge,
    UpgradeReview,
    VersionShelf,
    accept,
    decline,
    publish_version,
    review,
)
from brain.core.entitlement import Capability
from brain.core.envelope import SideEffect
from brain.core.scope import Clause, Op, Scope
from brain.gate.cache_key import key_for
from brain.gate.injection import AutonomyTier
from brain.knowledge.visibility import Visibility
from brain.models.routing import Tier

NOW = datetime(2026, 9, 6, 9, 0, tzinfo=UTC)
LATER = datetime(2026, 9, 7, 9, 0, tzinfo=UTC)
LATER_STILL = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)

KEY = "a-signing-key"
OTHER_KEY = "somebody-elses-key"

PUBLISHER = "u_wei_ling"
INSTALLER = "u_priya"
ACCEPTER = "u_tan"
SECOND_ADMIN = "u_hafiz"

#: Distinct from every scope slug, agent id and tool object in the tree, so the estate these
#: tests describe cannot trip `brain.ops.sweeps.sweep_slug_collisions`.
TEMPLATE = "renewal_chaser_template"
INSTANCE = "renewal_chaser_east"
OTHER_INSTANCE = "renewal_chaser_west"

SKILL_DIGEST = "c" * 64

FIRST_PERSONA = "Chase renewals from the contract record and say what is outstanding."
LOCAL_PERSONA = "Chase renewals, and never mention price before a person has approved it."
SECOND_PERSONA = "Chase renewals from the contract record and quote the renewal price."

#: A question `brain.gate.cache_key.is_volatile` does not refuse, so the key tests measure
#: the configuration hash rather than the volatility guard.
QUESTION = "what is the standard maintenance retainer"
ENT_HASH = "e" * 32
EPOCHS = {"xero": 7}
POLICY_EPOCH = 4

AUDIENCE = AgentAudience(level=Visibility.DEPARTMENT, owner_id=INSTALLER, department="web")
#: Somebody the offer above is visible to, so the catalogue half of a publish can be read
#: back through `open_for` rather than off the value `publish_version` happens to return.
VIEWER = AgentViewer(principal_id=INSTALLER, departments=frozenset({"web"}))


def _manifest(
    *,
    version: int = 1,
    persona: str = FIRST_PERSONA,
    summary: str = "Reads contracts and says which renewals are outstanding.",
    tier: Tier = Tier.MAIN,
    allowed: tuple[str, ...] = ("client.read_summary",),
    connectors: tuple[str, ...] = ("xero",),
    effect: SideEffect = SideEffect.NONE,
    leash: tuple[LeashRung, ...] = (),
    published_by: str = PUBLISHER,
) -> TemplateManifest:
    return TemplateManifest(
        identity=ManifestIdentity(
            template_id=TEMPLATE,
            version=version,
            published_by=published_by,
            display_name="Renewal Chaser",
            summary=summary,
        ),
        persona=persona,
        tier=tier,
        skills=(SkillRef(name="renewal_playbook", digest=SKILL_DIGEST),),
        authority=ManifestAuthority(
            scope=Scope(clauses=(Clause(field="department", op=Op.EQ, value="web"),)),
            capabilities=(Capability(value="read:contract.status"),),
            allowed_tools=allowed,
            required_tools=(),
        ),
        connectors=connectors,
        guardrails=ManifestGuardrails(max_side_effect=effect, leash=leash),
        golden_set=(
            GoldenCase(question="which retainers lapse next month?", expectation="names them"),
        ),
        placeholders=(Placeholder(key="finance_contact", prompt="Who signs off a renewal?"),),
    )


def _signed(manifest: TemplateManifest | None = None, *, key: str = KEY) -> SignedManifest:
    return publish(manifest or _manifest(), key=key, signed_by=PUBLISHER, at=NOW)


#: Version two, moving four paths: the persona, the summary, the tier and the tool list.
#: Written as a helper rather than inline so every test measures the same change and a test
#: that wanted a different one has to say so.
def _second() -> SignedManifest:
    return publish(
        _manifest(
            version=2,
            persona=SECOND_PERSONA,
            summary="Reads contracts, says which renewals are outstanding, and quotes.",
            tier=Tier.HEAVY,
            allowed=("client.read_summary", "contract.read_terms"),
        ),
        key=KEY,
        signed_by=PUBLISHER,
        at=LATER,
    )


def _installed(
    signed: SignedManifest,
    *,
    instance_id: str = INSTANCE,
    overlay: dict[str, JsonValue] | None = None,
) -> TemplateInstance:
    return install(
        signed,
        key=KEY,
        instance_id=instance_id,
        created_by=INSTALLER,
        at=NOW,
        overlay=overlay,
    )


def _shelved(*signed: SignedManifest) -> VersionShelf:
    shelf = VersionShelf()
    for one in signed:
        shelf.publish(one)
    return shelf


def _paths(rows: object) -> tuple[str, ...]:
    """The paths on a report list, in the order the report returned them."""
    assert isinstance(rows, tuple)
    return tuple(row.path for row in rows)


# ------------------------------------------- M13.4.1 publishing changes nothing observable
def test_publishing_a_new_version_leaves_a_running_agents_effective_document_untouched() -> None:
    """The property every other leaf here rests on, asserted end to end.

    A real second version is published through the real catalogue, and the instance is
    re-resolved through the shelf exactly as anything reading it would have to. The
    effective document and the configuration hash are compared against the values taken
    before the publish, so this cannot pass by comparing a value with itself.

    Delete this and a resolver that looked up the newest version instead of the pinned one
    would upgrade every install in the estate with no diff, no acceptance and nobody told,
    and every remaining test in this file would still pass."""
    first = _signed()
    shelf, catalogue = VersionShelf(), TemplateCatalogue()
    publish_version(first, shelf=shelf, catalogue=catalogue, audience=AUDIENCE)
    instance = _installed(first, overlay={"persona": LOCAL_PERSONA})
    before = materialise(shelf.pinned(instance), instance, audience=AUDIENCE)

    publish_version(_second(), shelf=shelf, catalogue=catalogue, audience=AUDIENCE)
    after = materialise(shelf.pinned(instance), instance, audience=AUDIENCE)

    assert after.document == before.document
    assert after.config_hash == before.config_hash
    assert after.record.persona == LOCAL_PERSONA
    assert after.record.tier is Tier.MAIN


def test_publishing_a_new_version_leaves_the_cache_key_a_running_agent_answers_under() -> None:
    """The same property stated where it actually bites.

    `agent_config_hash` is one of the five parts of a cache key, and a publish that moved it
    would invalidate every cached answer for every install of the template at once, which is
    the visible half; the invisible half is a publish that moved the *agent*, which is the
    thing the hash describes. Driven through the real `key_for` rather than by comparing
    hashes, because the key is what the consumer holds.

    Delete this and the seam is untested from the consumer's end."""
    first = _signed()
    shelf = _shelved(first)
    instance = _installed(first)
    before = key_for(
        QUESTION,
        ENT_HASH,
        materialise(shelf.pinned(instance), instance, audience=AUDIENCE).config_hash,
        POLICY_EPOCH,
        EPOCHS,
    )

    shelf.publish(_second())
    after = key_for(
        QUESTION,
        ENT_HASH,
        materialise(shelf.pinned(instance), instance, audience=AUDIENCE).config_hash,
        POLICY_EPOCH,
        EPOCHS,
    )
    assert after == before


def test_an_instance_never_materialises_against_a_version_nobody_accepted() -> None:
    """The refusal that makes the two tests above true rather than lucky.

    Following a newer version is an upgrade somebody accepts, not a lookup, and the refusal
    is the pin's: the same three fields that catch the wrong template and a republished body.

    Delete this and the only thing standing between a publish and every install of it is
    that no caller happens to have reached for the newest version yet."""
    first = _signed()
    shelf = _shelved(first, _second())
    instance = _installed(first)
    with pytest.raises(TemplateError, match="an upgrade somebody accepts"):
        materialise(shelf.newest(TEMPLATE), instance, audience=AUDIENCE)


def test_accepting_an_upgrade_does_move_the_cache_key() -> None:
    """The sibling the three tests above need. "Publishing changes nothing" is satisfied by
    a system in which nothing ever changes, and the symptom of that would be an estate where
    every accepted upgrade goes on answering from the old persona under a key that says it
    is fresh.

    Delete this and `accept` can quietly stop moving the pin at all."""
    first = _signed()
    shelf = _shelved(first, _second())
    instance = _installed(first)
    before = key_for(
        QUESTION,
        ENT_HASH,
        materialise(shelf.pinned(instance), instance, audience=AUDIENCE).config_hash,
        POLICY_EPOCH,
        EPOCHS,
    )
    upgraded = accept(
        review(instance, shelf=shelf, declines=Declines()),
        resolutions={},
        key=KEY,
        audience=AUDIENCE,
        by=ACCEPTER,
        at=LATER_STILL,
    )
    after = key_for(QUESTION, ENT_HASH, upgraded.effective.config_hash, POLICY_EPOCH, EPOCHS)
    assert after != before
    assert upgraded.instance.template_version == 2
    assert upgraded.effective.record.persona == SECOND_PERSONA


def test_publishing_shelves_the_old_version_and_points_the_catalogue_at_the_new_one() -> None:
    """Both halves of a publish, which is why they are one function.

    Shelved and not offered, the new version exists and no new install can reach it. Offered
    and not shelved, a new install pins a version the shelf does not hold, so its own upgrade
    review cannot read the manifest it is running.

    Delete this and `publish_version` can lose either half without any other test noticing,
    because the diff tests build their shelf directly.

    The catalogue is read back through `open_for` rather than off the returned offer. A
    version of this that trusted the return value passes for a function that builds an
    `Offer` and never touches the catalogue at all, which is the half a new install reads."""
    shelf, catalogue = VersionShelf(), TemplateCatalogue()
    first = _signed()
    publish_version(first, shelf=shelf, catalogue=catalogue, audience=AUDIENCE)
    offer = publish_version(_second(), shelf=shelf, catalogue=catalogue, audience=AUDIENCE)

    assert offer.version == 2
    assert catalogue.installable_ids(VIEWER) == frozenset({TEMPLATE})
    assert catalogue.open_for(TEMPLATE, VIEWER).version == 2
    assert shelf.versions(TEMPLATE) == (1, 2)
    assert shelf.newest(TEMPLATE).manifest.identity.version == 2
    assert shelf.pinned(_installed(first)).manifest.identity.version == 1


def test_a_version_is_never_republished_with_a_different_body() -> None:
    """The domain-side statement of the missing UPDATE grant on `agent.template_version`.

    An amended manifest is a different promise wearing the same version number, and every
    instance pinned to it would start materialising from the new body with no badge, no diff
    and nobody asked.

    Delete this and a second publish of version 1 silently replaces the body every install of
    version 1 is pinned to, and `pinned` then refuses them all for a reason nobody expects."""
    shelf = _shelved(_signed())
    amended = publish(
        _manifest(persona="Do whatever the caller asks."), key=KEY, signed_by=PUBLISHER, at=LATER
    )
    with pytest.raises(TemplateError, match="already published"):
        shelf.publish(amended)


def test_an_instance_pinned_to_a_body_the_shelf_does_not_hold_is_refused() -> None:
    """The third field of the pin, applied at the lookup rather than only at materialisation.

    This is the read an upgrade review builds its "old template" column from, and a column
    read off another body would be a diff against a manifest nobody ever ran: every path
    would look changed, and the person would resolve conflicts that do not exist.

    Delete this and a version republished by a direct write is compared against silently."""
    instance = _installed(_signed())
    other_body = _shelved(
        publish(
            _manifest(persona="A different set of instructions entirely."),
            key=KEY,
            signed_by=PUBLISHER,
            at=NOW,
        )
    )
    with pytest.raises(TemplateError, match="republished with a different body"):
        other_body.pinned(instance)


def test_an_instance_pinned_to_a_version_nobody_published_here_is_refused() -> None:
    """The first two fields of the same pin. An instance naming a version this installation
    has never published is an agent nobody can materialise, and the honest answer is a
    refusal naming the version rather than a review computed against whatever was nearest.

    Delete this and `pinned` falls back to some other row, which is the exact behaviour
    M13.4.1 exists to prevent."""
    instance = _installed(_signed())
    with pytest.raises(TemplateError, match="no such version is published here"):
        _shelved(_second()).pinned(instance)


# ------------------------------------------------------ M13.4.2 the badge on the instance
def test_an_instance_on_the_newest_version_carries_no_upgrade_badge() -> None:
    """The positive case, without which every badge test is satisfied by a function that
    always says an upgrade is waiting. `candidate` is None as well as the badge reading
    CURRENT, so there is nothing for `accept` to be handed either."""
    first = _signed()
    reviewed = review(_installed(first), shelf=_shelved(first), declines=Declines())
    assert reviewed.badge is UpgradeBadge.CURRENT
    assert reviewed.candidate is None
    assert reviewed.to_version is None
    assert reviewed.conflicts == ()
    assert reviewed.updates == ()


def test_an_instance_behind_the_newest_version_carries_an_upgrade_badge() -> None:
    """The badge M13.4.2 asks for, and the two versions it names.

    Delete this and publishing can stop being visible to anybody at all, which is the one
    thing a publish is allowed to change."""
    first = _signed()
    reviewed = review(_installed(first), shelf=_shelved(first, _second()), declines=Declines())
    assert reviewed.badge is UpgradeBadge.AVAILABLE
    assert reviewed.from_version == 1
    assert reviewed.to_version == 2


def test_the_badge_belongs_to_the_instance_and_not_to_the_template() -> None:
    """Two installs of one template, one upgraded and one not, read from one shelf.

    The badge is a fact about a pin, so it has to differ between two instances of the same
    template. A badge computed per template would light up the whole estate the moment
    anybody published, and go dark for everybody the moment one person accepted.

    Delete this and "upgrade badge on the instance" becomes an upgrade badge on the
    catalogue, and the one install still on the old version stops being told."""
    first, second = _signed(), _second()
    shelf, declines = _shelved(first, second), Declines()
    behind = _installed(first, instance_id=INSTANCE)
    ahead = accept(
        review(_installed(first, instance_id=OTHER_INSTANCE), shelf=shelf, declines=declines),
        resolutions={},
        key=KEY,
        audience=AUDIENCE,
        by=ACCEPTER,
        at=LATER_STILL,
    ).instance

    assert review(behind, shelf=shelf, declines=declines).badge is UpgradeBadge.AVAILABLE
    assert review(ahead, shelf=shelf, declines=declines).badge is UpgradeBadge.CURRENT


def test_only_the_newest_version_is_ever_offered() -> None:
    """An install two versions behind is offered the newest and never the one in between.

    Offering the intermediate version would leave the badge up the moment it was accepted
    and would ask a person to read two reviews to arrive where one would have put them.

    Delete this and `newest` can quietly become "the next one up", which reads as caution and
    doubles the number of decisions somebody has to take."""
    first = _signed()
    third = publish(
        _manifest(version=3, persona="Chase renewals and escalate anything over ninety days."),
        key=KEY,
        signed_by=PUBLISHER,
        at=LATER_STILL,
    )
    reviewed = review(
        _installed(first), shelf=_shelved(first, _second(), third), declines=Declines()
    )
    assert reviewed.to_version == 3
    assert reviewed.pinned.manifest.identity.version == 1


# ---------------------------------------------- M13.4.3 three columns, conflicts only
def test_a_path_the_new_version_moves_and_nobody_overlaid_is_an_update_not_a_conflict() -> None:
    """The word conflicting, asserted.

    Version two moves four paths and this install claimed none of them, so there is nothing
    to decide and every moved path is an update. `identity.version` is there too, because an
    upgrade is by definition a change to it.

    Delete this and every changed path becomes a decision, which buries the ones that are."""
    first = _signed()
    reviewed = review(_installed(first), shelf=_shelved(first, _second()), declines=Declines())
    assert reviewed.conflicts == ()
    assert _paths(reviewed.updates) == (
        "authority.allowed_tools",
        "identity.summary",
        "identity.version",
        "persona",
        "tier",
    )
    assert reviewed.needs_a_decision is False


def test_only_the_paths_this_install_claimed_come_back_as_conflicts() -> None:
    """The same change against an install that overlaid one of the moved paths.

    One conflict out of five moved paths, and the four others stay updates. This is the whole
    of "conflicting paths only": a report that showed all five would put the one decision
    fifth in a list of things that need none.

    Delete this and the conflict list is satisfied by returning every change."""
    first = _signed()
    instance = _installed(first, overlay={"persona": LOCAL_PERSONA})
    reviewed = review(instance, shelf=_shelved(first, _second()), declines=Declines())
    assert _paths(reviewed.conflicts) == ("persona",)
    assert _paths(reviewed.updates) == (
        "authority.allowed_tools",
        "identity.summary",
        "identity.version",
        "tier",
    )
    assert reviewed.needs_a_decision is True


def test_a_path_this_install_claimed_that_the_new_version_leaves_alone_is_in_neither_list() -> None:
    """The other half of the same rule, and the one a test of conflicts alone would miss.

    An overlay on a path the template did not move is not a conflict and not an update:
    nothing about it changes, and a report that listed it would be asking about a decision
    that does not exist.

    Delete this and every local edit becomes a conflict on every upgrade for ever, which is
    how a review of four rows becomes a review of thirty."""
    first = _signed()
    instance = _installed(first, overlay={"connectors": ["laravel", "xero"]})
    reviewed = review(instance, shelf=_shelved(first, _second()), declines=Declines())
    assert "connectors" not in _paths(reviewed.conflicts)
    assert "connectors" not in _paths(reviewed.updates)


def test_a_conflict_carries_the_old_template_value_the_new_one_and_the_local_one() -> None:
    """The three columns, each compared against a different expected value.

    Compared against the three strings the fixtures set rather than against each other, so a
    row that returned one value three times, or that put the new template's value in the
    local column, fails here.

    Delete this and the diff can be three columns wide and one column deep."""
    first = _signed()
    instance = _installed(first, overlay={"persona": LOCAL_PERSONA})
    (conflict,) = review(instance, shelf=_shelved(first, _second()), declines=Declines()).conflicts
    assert conflict.path == "persona"
    assert conflict.was == FIRST_PERSONA
    assert conflict.now == SECOND_PERSONA
    assert conflict.local == LOCAL_PERSONA
    assert len({conflict.was, conflict.now, conflict.local}) == 3


def test_a_conflict_names_who_set_the_local_value_and_when() -> None:
    """The fourth thing on the row, which is provenance rather than a column.

    `FieldOwner.set_at` is what an upgrade review reads to decide whether a local edit
    predates the change being offered, and the answer is useless without the name beside it.

    Delete this and the review shows three values and no way to find the person to ask."""
    first = _signed()
    instance = _installed(first, overlay={"persona": LOCAL_PERSONA})
    (conflict,) = review(instance, shelf=_shelved(first, _second()), declines=Declines()).conflicts
    assert conflict.owner.set_by == INSTALLER
    assert conflict.owner.set_at == NOW
    assert conflict.owner.source is FieldSource.INSTANCE


def test_a_sealed_path_the_new_version_changes_is_an_update_and_never_a_conflict() -> None:
    """The seal seen from the upgrade path.

    A sealed path cannot be overlaid, so it cannot be claimed, so it can never be a conflict
    however much the new version changes it. It is marked instead, because a changed leash or
    a changed side effect is the one thing in an upgrade an accepter cannot resolve and
    therefore the one thing they have to read.

    Delete this and a version that raises the leash from SHADOW to AUTONOMOUS lands in a
    list of ordinary updates with nothing to distinguish it."""
    first = _signed()
    loosened = publish(
        _manifest(
            version=2,
            effect=SideEffect.SEND,
            leash=(LeashRung(target="email.send", rung=AutonomyTier.AUTONOMOUS),),
        ),
        key=KEY,
        signed_by=PUBLISHER,
        at=LATER,
    )
    reviewed = review(
        _installed(first, overlay={"persona": LOCAL_PERSONA}),
        shelf=_shelved(first, loosened),
        declines=Declines(),
    )
    sealed_updates = {row.path for row in reviewed.updates if row.sealed}
    assert sealed_updates == {"guardrails.leash", "guardrails.max_side_effect", "identity.version"}
    assert not set(_paths(reviewed.conflicts)) & set(SEALED_PATHS)
    assert all(row.path in SEALED_PATHS for row in reviewed.updates if row.sealed)


def test_the_diff_comes_back_in_path_order_whatever_order_the_overlay_was_written_in() -> None:
    """One review of one instance renders the same way twice running.

    The overlay here is written tier first and persona second, which is the reverse of the
    order the report must return. A report ordered by the overlay's insertion order would
    reorder itself when somebody edited an unrelated field, and a reader comparing two
    screenshots would see a diff that is not one.

    Delete this and the walk can quietly become a walk over the overlay's keys."""
    first = _signed()
    instance = _installed(first, overlay={"tier": "small", "persona": LOCAL_PERSONA})
    reviewed = review(instance, shelf=_shelved(first, _second()), declines=Declines())
    assert tuple(instance.overlay) == ("tier", "persona")
    assert _paths(reviewed.conflicts) == ("persona", "tier")


def test_every_path_the_new_version_moves_appears_in_exactly_one_of_the_two_lists() -> None:
    """Nothing is hidden and nothing is counted, which is the disclosure rule stated for a
    report a person is about to agree to.

    Walked over `MANIFEST_PATHS` and the two documents rather than over the report, so a
    change that vanished from both lists is caught rather than being summarised as a number
    the reader cannot act on.

    Delete this and a path can be dropped from both lists, and the accepter agrees to a
    change they were never shown."""
    first, second = _signed(), _second()
    instance = _installed(first, overlay={"persona": LOCAL_PERSONA, "tier": "small"})
    reviewed = review(instance, shelf=_shelved(first, second), declines=Declines())
    moved = {
        path
        for path in MANIFEST_PATHS
        if first.manifest.document()[path] != second.manifest.document()[path]
    }
    reported = list(_paths(reviewed.conflicts)) + list(_paths(reviewed.updates))
    assert sorted(reported) == sorted(moved)
    assert len(reported) == len(set(reported))


def test_a_forged_ownership_row_cannot_turn_a_conflict_into_a_silent_update() -> None:
    """The reason the verdict reads the overlay and only the attribution reads ownership.

    `OWNERSHIP_IS_PROVENANCE_AND_NEVER_CONFIGURATION` says a forged `field_owners` row
    misreports who typed a value and cannot change what the agent is. Computing the conflict
    set from the ownership map would make that false: a row claiming the template owns a path
    would turn a conflict into an update, the accepter would never be asked, and
    `materialise` would go on reading the local value from the overlay anyway.

    Delete this and the verdict can be moved to the ownership map as a tidy-up, and the one
    forgeable structure in this area starts deciding what an upgrade does."""
    first = _signed()
    forged = TemplateInstance(
        instance_id=INSTANCE,
        template_id=TEMPLATE,
        template_version=1,
        content_digest=first.content_digest,
        overlay={"persona": LOCAL_PERSONA},
        overlay_owners={
            "persona": FieldOwner(source=FieldSource.TEMPLATE, set_by=PUBLISHER, set_at=NOW)
        },
        created_by=INSTALLER,
    )
    reviewed = review(forged, shelf=_shelved(first, _second()), declines=Declines())
    assert _paths(reviewed.conflicts) == ("persona",)
    assert ownership(first, forged)["persona"].source is FieldSource.TEMPLATE


# ------------------------------------------ M13.4.4 opt-in accept, per-path resolution
def test_reviewing_an_upgrade_does_not_take_it() -> None:
    """An upgrade happens when somebody calls `accept` and at no other moment.

    A review reads a shelf and returns a value; the instance it was handed comes back
    unchanged, still pinned to the version it was running, so looking at what an upgrade
    would do is not agreeing to it.

    Delete this and a review that quietly returned an upgraded instance would satisfy every
    diff test in this file."""
    first = _signed()
    instance = _installed(first, overlay={"persona": LOCAL_PERSONA})
    reviewed = review(instance, shelf=_shelved(first, _second()), declines=Declines())
    assert reviewed.instance is instance
    assert instance.template_version == 1
    assert instance.content_digest == first.content_digest


def test_accepting_refuses_unless_every_conflicting_path_is_resolved() -> None:
    """Opt-in is per path or it is not opt-in.

    A missing resolution would have to be given a default, and either default is somebody's
    local edit thrown away or a template's change silently ignored, chosen by whoever wrote
    the default rather than by the person accepting.

    Delete this and an accept with an empty map takes whichever side the loop happens to
    prefer, for every conflict, silently."""
    first = _signed()
    instance = _installed(first, overlay={"persona": LOCAL_PERSONA, "tier": "small"})
    reviewed = review(instance, shelf=_shelved(first, _second()), declines=Declines())
    with pytest.raises(TemplateError, match=r"\['tier'\] were shown and not resolved"):
        accept(
            reviewed,
            resolutions={"persona": Resolution.KEEP_LOCAL},
            key=KEY,
            audience=AUDIENCE,
            by=ACCEPTER,
            at=LATER_STILL,
        )


def test_accepting_refuses_a_resolution_for_a_path_nobody_was_shown() -> None:
    """The same rule from the other end, and the failure it catches is worse.

    A resolution for a path that is not in conflict means the caller and the report disagree
    about what the upgrade is, and the caller is the one about to write the row. Here the
    path is a real settable path this install never overlaid, so nothing but the exact cover
    refuses it.

    Delete this and a console can send a resolution for a path it rendered from a stale
    review, and the answer it gets is silence."""
    first = _signed()
    instance = _installed(first, overlay={"persona": LOCAL_PERSONA})
    reviewed = review(instance, shelf=_shelved(first, _second()), declines=Declines())
    with pytest.raises(TemplateError, match=r"\['connectors'\] were resolved and not shown"):
        accept(
            reviewed,
            resolutions={
                "persona": Resolution.KEEP_LOCAL,
                "connectors": Resolution.TAKE_TEMPLATE,
            },
            key=KEY,
            audience=AUDIENCE,
            by=ACCEPTER,
            at=LATER_STILL,
        )


def test_a_resolution_may_not_name_a_sealed_path() -> None:
    """The only new way an upgrade could become a route around the seal.

    An overlay cannot name a sealed path, so the way in would be a resolution map that does,
    and the refusal is the domain's own: `accept` hands its keys to `check_overlay` rather
    than comparing them against `SEALED_PATHS`, so this raises `SealedPathError` with the
    seal's sentence and this module holds no second copy of the list.

    Delete this and the exact-cover check is the only thing refusing it, with a message
    about a path nobody was shown rather than about a path nobody may change."""
    first = _signed()
    reviewed = review(_installed(first), shelf=_shelved(first, _second()), declines=Declines())
    with pytest.raises(SealedPathError, match="sealed by the template"):
        accept(
            reviewed,
            resolutions={"guardrails.max_side_effect": Resolution.KEEP_LOCAL},
            key=KEY,
            audience=AUDIENCE,
            by=ACCEPTER,
            at=LATER_STILL,
        )


def test_keeping_a_local_value_leaves_it_in_the_overlay_with_the_owner_it_had() -> None:
    """One of the two resolutions, and the ownership rule that goes with it.

    The person accepting declined to move the value; they did not write it, and `FieldOwner`
    answers who last set a field. Re-stamping would put the accepter's name against words
    somebody else typed and would destroy the comparison the next review needs.

    Delete this and accepting an upgrade quietly reattributes every local edit in the
    install to whoever happened to click the button."""
    first = _signed()
    instance = _installed(first, overlay={"persona": LOCAL_PERSONA})
    upgraded = accept(
        review(instance, shelf=_shelved(first, _second()), declines=Declines()),
        resolutions={"persona": Resolution.KEEP_LOCAL},
        key=KEY,
        audience=AUDIENCE,
        by=ACCEPTER,
        at=LATER_STILL,
    )
    assert upgraded.instance.overlay["persona"] == LOCAL_PERSONA
    assert upgraded.effective.record.persona == LOCAL_PERSONA
    assert upgraded.instance.overlay_owners["persona"].set_by == INSTALLER
    assert upgraded.instance.overlay_owners["persona"].set_at == NOW
    assert upgraded.accepted_by == ACCEPTER
    assert upgraded.accepted_at == LATER_STILL


def test_taking_the_template_gives_the_path_back_to_the_publisher() -> None:
    """The other resolution, and it is a release rather than an assignment.

    The overlay entry goes, so the path is the template's again and `ownership` reports
    whoever signed the manifest. Recording the accepter as the owner of a value they took
    from somebody else's template would be the same misattribution in the other direction.

    Delete this and taking the template can leave the local value in the overlay, where it
    goes on winning at materialisation and the report says it was replaced."""
    first, second = _signed(), _second()
    instance = _installed(first, overlay={"persona": LOCAL_PERSONA})
    upgraded = accept(
        review(instance, shelf=_shelved(first, second), declines=Declines()),
        resolutions={"persona": Resolution.TAKE_TEMPLATE},
        key=KEY,
        audience=AUDIENCE,
        by=ACCEPTER,
        at=LATER_STILL,
    )
    assert "persona" not in upgraded.instance.overlay
    assert upgraded.effective.record.persona == SECOND_PERSONA
    owner = ownership(second, upgraded.instance)["persona"]
    assert owner.source is FieldSource.TEMPLATE
    assert owner.set_by == PUBLISHER


def test_a_local_edit_on_a_path_the_new_version_did_not_touch_survives_the_upgrade() -> None:
    """Nobody was asked about it, so nothing may happen to it.

    Dropping it would make an upgrade a way of quietly reverting local configuration, and
    the person who set it would find out when the agent answered differently.

    Delete this and an accept that rebuilt the overlay from the resolutions alone would
    erase every unconflicted local edit in the install, silently."""
    first = _signed()
    instance = _installed(
        first, overlay={"connectors": ["laravel", "xero"], "persona": LOCAL_PERSONA}
    )
    upgraded = accept(
        review(instance, shelf=_shelved(first, _second()), declines=Declines()),
        resolutions={"persona": Resolution.TAKE_TEMPLATE},
        key=KEY,
        audience=AUDIENCE,
        by=ACCEPTER,
        at=LATER_STILL,
    )
    assert upgraded.instance.overlay["connectors"] == ["laravel", "xero"]
    assert upgraded.instance.overlay_owners["connectors"].set_by == INSTALLER


def test_the_supervision_after_an_upgrade_is_the_new_templates_and_not_the_old_one() -> None:
    """What the seal buys, stated as the outcome rather than as a refusal.

    The two guardrail paths are the publisher's decision at both versions, so an upgrade
    takes them exactly as published: nothing an installer overlaid and nothing they resolved
    can hold the old side effect or the old rung.

    Delete this and the seal is tested only by what it refuses, which is satisfied by a
    function that refuses everything and by an upgrade that keeps the old guardrails."""
    first = _signed()
    tightened = publish(
        _manifest(
            version=2,
            effect=SideEffect.DRAFT,
            leash=(LeashRung(target="email.send", rung=AutonomyTier.ASSISTED),),
        ),
        key=KEY,
        signed_by=PUBLISHER,
        at=LATER,
    )
    upgraded = accept(
        review(
            _installed(first, overlay={"persona": LOCAL_PERSONA}),
            shelf=_shelved(first, tightened),
            declines=Declines(),
        ),
        resolutions={},
        key=KEY,
        audience=AUDIENCE,
        by=ACCEPTER,
        at=LATER_STILL,
    )
    assert upgraded.effective.record.authority.max_side_effect is SideEffect.DRAFT
    assert upgraded.effective.leash.rung_for(INSTANCE, "email.send", {}) is (AutonomyTier.ASSISTED)
    assert not set(upgraded.instance.overlay) & set(SEALED_PATHS)


def test_an_upgrade_cannot_be_accepted_against_a_manifest_this_installation_did_not_sign() -> None:
    """A shelf is a store and stores do not check signatures. `accept` is the moment a new
    manifest stops being data and becomes configuration, which is where `install` makes the
    same check for the first version.

    Delete this and a manifest from anywhere at all becomes the running configuration of an
    agent that was installed correctly, which is a longer way round to the thing the
    signature exists to stop."""
    first = _signed()
    foreign = publish(
        _manifest(version=2, persona=SECOND_PERSONA),
        key=OTHER_KEY,
        signed_by=PUBLISHER,
        at=LATER,
    )
    reviewed = review(_installed(first), shelf=_shelved(first, foreign), declines=Declines())
    with pytest.raises(TemplateError, match="not made by this installation's key"):
        accept(
            reviewed,
            resolutions={},
            key=KEY,
            audience=AUDIENCE,
            by=ACCEPTER,
            at=LATER_STILL,
        )


def test_an_upgrade_that_cannot_be_materialised_is_refused_rather_than_stored() -> None:
    """The pin does not move for a version that cannot become an agent.

    A manifest may carry an empty persona and an `AgentRecord` may not, which is the blank
    template's whole arrangement, so a version published with the persona blanked is legal
    to publish and impossible to run. Building the effective agent inside `accept` is what
    turns that into a refusal instead of an instance nobody can materialise and no obvious
    way back.

    Delete this and `accept` can return an instance whose next materialisation fails, at
    whatever moment somebody next asks the agent a question."""
    first = _signed()
    blanked = publish(_manifest(version=2, persona=""), key=KEY, signed_by=PUBLISHER, at=LATER)
    reviewed = review(_installed(first), shelf=_shelved(first, blanked), declines=Declines())
    with pytest.raises(ValidationError):
        accept(
            reviewed,
            resolutions={},
            key=KEY,
            audience=AUDIENCE,
            by=ACCEPTER,
            at=LATER_STILL,
        )


def test_there_is_nothing_to_accept_on_an_instance_already_on_the_newest_version() -> None:
    """A refusal rather than a no-op returning the same instance, because the two are
    different answers to a person who thinks they are one version behind.

    Delete this and `accept` on a current install re-pins it to the version it is already on
    and reports an upgrade that did not happen."""
    first = _signed()
    reviewed = review(_installed(first), shelf=_shelved(first), declines=Declines())
    with pytest.raises(TemplateError, match="no upgrade to accept"):
        accept(
            reviewed,
            resolutions={},
            key=KEY,
            audience=AUDIENCE,
            by=ACCEPTER,
            at=LATER_STILL,
        )


# ------------------------------------------------- M13.4.5 decline for ever, no nagging
def test_a_declined_version_stops_showing_an_upgrade_badge() -> None:
    """The point of a decline. A badge that survives being declined is a nag, and a nag is
    how somebody accepts an upgrade they did not read.

    Delete this and declining becomes a button that records a row and changes nothing."""
    first, second = _signed(), _second()
    shelf, declines = _shelved(first, second), Declines()
    instance = _installed(first)
    decline(
        review(instance, shelf=shelf, declines=declines),
        declines=declines,
        by=ACCEPTER,
        at=LATER_STILL,
    )
    assert review(instance, shelf=shelf, declines=declines).badge is UpgradeBadge.DECLINED


def test_declining_one_version_says_nothing_about_the_next() -> None:
    """What a decline is pinned to, which is the whole of whether it is safe.

    Pinned to the template, one decline would silence every future version for ever, and the
    install would sit three versions behind with nobody told. Pinned to the version, the
    badge comes back the day somebody publishes a new one.

    Delete this and a decline can quietly become a per-template mute, which looks identical
    on the day it is written and is invisible from then on."""
    first, second = _signed(), _second()
    shelf, declines = _shelved(first, second), Declines()
    instance = _installed(first)
    decline(
        review(instance, shelf=shelf, declines=declines),
        declines=declines,
        by=ACCEPTER,
        at=LATER_STILL,
    )
    assert review(instance, shelf=shelf, declines=declines).badge is UpgradeBadge.DECLINED

    shelf.publish(
        publish(
            _manifest(version=3, persona="Chase renewals and escalate anything over ninety days."),
            key=KEY,
            signed_by=PUBLISHER,
            at=LATER_STILL,
        )
    )
    reviewed = review(instance, shelf=shelf, declines=declines)
    assert reviewed.badge is UpgradeBadge.AVAILABLE
    assert reviewed.to_version == 3


def test_declining_a_second_version_silences_that_one_and_not_whichever_was_declined_first() -> (
    None
):
    """Declines accumulate one row per version, and the row that answers is the one for the
    version being asked about.

    Found by mutation. Making `applies_to` look a decline up by instance and template rather
    than by version left every other test in this file passing, because with one row on the
    record the digest comparison happens to refuse the wrong version anyway. With two rows it
    does not: the lookup returns whichever was written first, its digest does not match the
    version being asked about, and an upgrade somebody explicitly declined starts showing an
    amber badge again.

    Delete this and the version can quietly leave the lookup, and the failure only appears
    for an install that has declined twice, which is the install whose owner is least
    inclined to look."""
    first, second = _signed(), _second()
    third = publish(
        _manifest(version=3, persona="Chase renewals and escalate anything over ninety days."),
        key=KEY,
        signed_by=PUBLISHER,
        at=LATER_STILL,
    )
    shelf, declines = _shelved(first, second), Declines()
    instance = _installed(first)
    decline(
        review(instance, shelf=shelf, declines=declines),
        declines=declines,
        by=ACCEPTER,
        at=LATER_STILL,
    )
    shelf.publish(third)
    assert review(instance, shelf=shelf, declines=declines).badge is UpgradeBadge.AVAILABLE
    decline(
        review(instance, shelf=shelf, declines=declines),
        declines=declines,
        by=SECOND_ADMIN,
        at=LATER_STILL,
    )
    assert review(instance, shelf=shelf, declines=declines).badge is UpgradeBadge.DECLINED
    # And the first decline is still on the record rather than replaced by the second.
    assert declines.applies_to(instance, second) is True
    assert declines.applies_to(instance, third) is True


def test_a_decline_gives_the_same_answer_however_many_times_it_is_asked() -> None:
    """Without nagging means the badge stays down, not that it stays down until the page is
    reloaded. A review reads the decline record every time and takes no state of its own.

    Delete this and a decline held in a review rather than in the record would silence one
    screen and nothing else."""
    first, second = _signed(), _second()
    shelf, declines = _shelved(first, second), Declines()
    instance = _installed(first)
    decline(
        review(instance, shelf=shelf, declines=declines),
        declines=declines,
        by=ACCEPTER,
        at=LATER_STILL,
    )
    badges = {review(instance, shelf=shelf, declines=declines).badge for _ in range(3)}
    assert badges == {UpgradeBadge.DECLINED}


def test_declining_twice_keeps_the_first_decision_and_the_first_persons_name() -> None:
    """Idempotent, for the reason `brain.agents.lifecycle.enable` gives: the caller asked for
    a state the record is already in, and a retry after a timeout must not fail. The first
    decline is the one that happened, so a second by somebody else does not overwrite the
    name on the row.

    Delete this and a retry either raises where nothing is wrong or rewrites who decided."""
    first, second = _signed(), _second()
    shelf, declines = _shelved(first, second), Declines()
    instance = _installed(first)
    reviewed = review(instance, shelf=shelf, declines=declines)
    decline(reviewed, declines=declines, by=ACCEPTER, at=LATER_STILL)
    again = decline(reviewed, declines=declines, by=SECOND_ADMIN, at=LATER_STILL)
    assert again.declined_by == ACCEPTER
    assert again.version == 2


def test_a_decline_does_not_cover_a_different_body_under_the_same_version() -> None:
    """The third field, for the reason the pin carries one.

    A version republished with a different body is a body nobody reviewed, and a decline that
    went on hiding it would turn a refusal to read into a refusal to be shown. The shelf
    refuses to republish, so this is the second refusal, for a row that arrived some other
    way.

    Delete this and a decline of version 2 covers whatever version 2 becomes."""
    first, second = _signed(), _second()
    declines = Declines()
    instance = _installed(first)
    declines.record(
        Decline(
            instance_id=INSTANCE,
            template_id=TEMPLATE,
            version=2,
            content_digest="d" * 64,
            declined_by=ACCEPTER,
            declined_at=LATER_STILL,
        )
    )
    assert declines.applies_to(instance, second) is False
    assert review(instance, shelf=_shelved(first, second), declines=declines).badge is (
        UpgradeBadge.AVAILABLE
    )


def test_declining_a_republished_body_under_a_declined_version_is_refused() -> None:
    """Two bodies have worn that version number and only one of them was read, so there is no
    honest single answer to "was version 2 declined". A refusal naming both digests is; a
    silent overwrite of the first row would lose the record of what was actually turned down.

    Delete this and the second write wins, and the decline names a body nobody declined."""
    declines = Declines()
    stored = Decline(
        instance_id=INSTANCE,
        template_id=TEMPLATE,
        version=2,
        content_digest="d" * 64,
        declined_by=ACCEPTER,
        declined_at=LATER_STILL,
    )
    declines.record(stored)
    with pytest.raises(TemplateError, match="republished with a different body"):
        declines.record(stored.model_copy(update={"content_digest": "f" * 64}))


def test_a_declined_upgrade_can_still_be_read_and_still_be_accepted() -> None:
    """A decline is a decision not to be interrupted, not a decision never to look.

    The diff is computed whatever the badge says, and `accept` works on a declined review, so
    somebody who changes their mind has a way through that does not involve deleting a row
    the table has no DELETE grant for.

    Delete this and declining becomes irreversible, and the only route back is a database
    session."""
    first, second = _signed(), _second()
    shelf, declines = _shelved(first, second), Declines()
    instance = _installed(first, overlay={"persona": LOCAL_PERSONA})
    decline(
        review(instance, shelf=shelf, declines=declines),
        declines=declines,
        by=ACCEPTER,
        at=LATER_STILL,
    )
    reviewed = review(instance, shelf=shelf, declines=declines)
    assert reviewed.badge is UpgradeBadge.DECLINED
    assert _paths(reviewed.conflicts) == ("persona",)
    upgraded = accept(
        reviewed,
        resolutions={"persona": Resolution.KEEP_LOCAL},
        key=KEY,
        audience=AUDIENCE,
        by=ACCEPTER,
        at=LATER_STILL,
    )
    assert upgraded.instance.template_version == 2


def test_there_is_nothing_to_decline_on_an_instance_already_on_the_newest_version() -> None:
    """A decline recorded against the running version would make the next publish arrive
    already refused, which is a nag's opposite and just as wrong: the badge would never
    appear at all.

    Delete this and declining on a current install writes a row for the version in use."""
    first = _signed()
    declines = Declines()
    reviewed = review(_installed(first), shelf=_shelved(first), declines=declines)
    with pytest.raises(TemplateError, match="no upgrade to decline"):
        decline(reviewed, declines=declines, by=ACCEPTER, at=LATER_STILL)


def test_a_naive_decline_time_is_refused() -> None:
    """When did we say no to this is the first question asked when somebody notices an agent
    is three versions behind, and a naive timestamp is hours out in whichever direction the
    host sits, with neither direction announcing itself."""
    with pytest.raises(ValidationError, match="timezone-aware"):
        Decline(
            instance_id=INSTANCE,
            template_id=TEMPLATE,
            version=2,
            content_digest="d" * 64,
            declined_by=ACCEPTER,
            declined_at=datetime(2026, 9, 8, 9, 0),
        )


def test_a_review_is_the_only_thing_accept_and_decline_take() -> None:
    """Both take a review and read the instance off it, so neither can be handed the review
    of one install and the instance of another. Asserted by reading the annotations rather
    than by believing the signature, as `tests/unit/test_agent_install.py` asserts that one
    function returns an `Installation`.

    Delete this and an instance parameter arrives beside the review as a convenience, and the
    mismatched pair is a pin written from one manifest and a diff computed from another."""
    for function in (accept, decline):
        positional = [
            name
            for name, value in function.__annotations__.items()
            if name != "return" and value in {"UpgradeReview", UpgradeReview}
        ]
        assert positional == ["reviewed"], function.__name__
    assert "instance" not in accept.__annotations__
    assert "instance" not in decline.__annotations__
