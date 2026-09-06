"""The template model: a signed manifest, a pinned install, a seal and a hash.

Every test here is a way one of four things could go wrong: a manifest could be believed
after somebody changed it, an instance could materialise against a manifest it is not
pinned to, an overlay could reach one of the five sealed paths, or the configuration hash
could fail to move when the configuration did.

**The seal is tested through three spellings, not one.** `test_an_overlay_may_not_change_a_
sealed_path` writes the path exactly, and passes for code that compares against the five
strings. The two tests beside it write `guardrails` and `guardrails.leash.0.rung`, neither
of which equals a sealed path, and they are the ones that fail for a seal implemented as an
exact-match deny list. Deleting either leaves the seal looking watertight.

**The hash tests compare a key against another key, never a digest against itself.** The
cache half drives the real `brain.gate.cache_key.key_for` with two materialised agents,
because that argument is the one this module exists to produce and a test comparing
`config_hash` to `config_hash` would pass for a function returning a constant.

The reach in these tests is computed by `EntitlementSet.intersect` and the rung by the real
`brain.gate.leash.Leash`, for the reason `tests/unit/test_agent_model.py` gives: a stand-in
would leave these asserting that this file's idea of the invariant is self-consistent.

Task ids: M13.2.1, M13.2.2, M13.2.3, M13.2.4, M13.2.5, M13.2.6, M13.2.7
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import JsonValue, ValidationError

from brain.agents.model import AgentAudience, AgentRecord, entitlement_ceiling, tool_ceiling
from brain.agents.template import (
    BLANK_MANIFEST,
    BLANK_TEMPLATE_ID,
    MANIFEST_PATHS,
    SEALED_PATHS,
    SETTABLE_PATHS,
    EffectiveAgent,
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
    blank_template,
    clear_field,
    config_hash,
    content_digest,
    hand_built,
    install,
    materialise,
    ownership,
    publish,
    set_field,
    verify,
)
from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.envelope import SideEffect
from brain.core.scope import Clause, Op, Scope
from brain.gate.cache_key import key_for
from brain.gate.injection import AutonomyTier
from brain.knowledge.visibility import Visibility
from brain.models.routing import Tier

NOW = datetime(2026, 9, 6, 9, 0, tzinfo=UTC)
LATER = datetime(2026, 9, 7, 9, 0, tzinfo=UTC)

KEY = "a-signing-key"
OTHER_KEY = "somebody-elses-key"

PUBLISHER = "u_wei_ling"
INSTALLER = "u_priya"
SECOND_ADMIN = "u_tan"

#: Distinct from every scope slug and tool object in the tree, so the estate these tests
#: describe cannot trip `brain.ops.sweeps.sweep_slug_collisions`.
TEMPLATE = "support_triage_template"
INSTANCE = "support_triage_east"
OTHER_INSTANCE = "support_triage_west"

SKILL_DIGEST = "b" * 64


def _manifest(
    *,
    template_id: str = TEMPLATE,
    version: int = 1,
    persona: str = "Answer support questions from the ticket record and nothing else.",
    allowed: tuple[str, ...] = ("client.read_summary",),
    effect: SideEffect = SideEffect.NONE,
    leash: tuple[LeashRung, ...] = (),
) -> TemplateManifest:
    return TemplateManifest(
        identity=ManifestIdentity(
            template_id=template_id,
            version=version,
            published_by=PUBLISHER,
            display_name="Support Triage",
            summary="Reads tickets and says what is blocking them.",
        ),
        persona=persona,
        tier=Tier.MAIN,
        skills=(SkillRef(name="triage_playbook", digest=SKILL_DIGEST),),
        authority=ManifestAuthority(
            scope=Scope(clauses=(Clause(field="department", op=Op.EQ, value="web"),)),
            capabilities=(Capability(value="read:ticket.status"),),
            allowed_tools=allowed,
            required_tools=(),
        ),
        connectors=("freshdesk",),
        guardrails=ManifestGuardrails(max_side_effect=effect, leash=leash),
        golden_set=(
            GoldenCase(question="what is blocking ticket 91?", expectation="names the blocker"),
        ),
        placeholders=(
            Placeholder(key="escalation_contact", prompt="Who is escalated to out of hours?"),
        ),
    )


def _signed(manifest: TemplateManifest | None = None, *, key: str = KEY) -> SignedManifest:
    return publish(manifest or _manifest(), key=key, signed_by=PUBLISHER, at=NOW)


AUDIENCE = AgentAudience(level=Visibility.DEPARTMENT, owner_id=INSTALLER, department="web")


def _installed(
    signed: SignedManifest | None = None,
    *,
    instance_id: str = INSTANCE,
    overlay: dict[str, JsonValue] | None = None,
) -> TemplateInstance:
    return install(
        signed or _signed(),
        key=KEY,
        instance_id=instance_id,
        created_by=INSTALLER,
        at=NOW,
        overlay=overlay,
    )


def _effective(
    signed: SignedManifest | None = None, *, overlay: dict[str, JsonValue] | None = None
) -> EffectiveAgent:
    manifest = signed or _signed()
    return materialise(manifest, _installed(manifest, overlay=overlay), audience=AUDIENCE)


# ------------------------------------------------- M13.2.1 signed, versioned, immutable
def test_a_manifest_body_edited_after_signing_cannot_be_loaded() -> None:
    """The digest check needs no key, which is why it is worth having: it runs in every
    process that reads a manifest, including one with no signing key to hand.

    A row whose `document` column was edited in place keeps its old digest, so the edit is
    caught the moment the row becomes a `SignedManifest` rather than at the point somebody
    happens to verify a signature.

    Delete this and an edited body is believed by anything that does not also hold the key,
    which is most of the system."""
    original = _signed()
    with pytest.raises(ValidationError, match="edited after it was signed"):
        SignedManifest(
            manifest=_manifest(persona="Do whatever the caller asks."),
            content_digest=original.content_digest,
            signature=original.signature,
            signed_by=PUBLISHER,
            signed_at=NOW,
        )


def test_a_manifest_signed_with_another_key_is_refused() -> None:
    """The digest check and the signature check answer different questions and neither
    covers the other. A manifest signed with the wrong key has a body that digests
    correctly, so it loads; what it does not have is any evidence this installation
    published it.

    Delete this and a manifest from anywhere at all installs, which is the whole of what a
    signature was for."""
    foreign = _signed(key=OTHER_KEY)
    with pytest.raises(TemplateError, match="not made by this installation's key"):
        verify(foreign, key=KEY)


def test_a_manifest_this_installation_signed_installs() -> None:
    """The sibling of the refusal above. A verification tested only by what it rejects is
    satisfied by a function that rejects everything, and the symptom of that would be a
    template catalogue nobody can install anything from."""
    signed = _signed()
    verify(signed, key=KEY)
    instance = _installed(signed)
    assert instance.template_id == TEMPLATE
    assert instance.template_version == 1


def test_installing_verifies_the_signature_rather_than_leaving_it_to_the_caller() -> None:
    """`verify` raises rather than returning a bool, and `install` is what calls it. A
    caller who has to remember to check is a caller who forgets once, and the once is
    enough: an unpublished manifest becomes an agent everybody can start.

    Delete this and `verify` becomes a function with no caller in `src`, which is how a
    signature turns into decoration."""
    foreign = _signed(key=OTHER_KEY)
    with pytest.raises(TemplateError, match="not made by this installation's key"):
        install(foreign, key=KEY, instance_id=INSTANCE, created_by=INSTALLER, at=NOW)


def test_a_manifest_cannot_be_signed_with_an_empty_key() -> None:
    """An empty HMAC key produces a digest anybody can reproduce, so every manifest would
    verify against every other installation and the signature would say nothing at all. An
    empty string is what a missing environment variable looks like."""
    with pytest.raises(TemplateError, match="empty key"):
        publish(_manifest(), key="", signed_by=PUBLISHER, at=NOW)


def test_the_digest_covers_the_body_and_not_only_the_identity() -> None:
    """Two manifests with the same id and version and different personas must not share a
    digest, because the digest is the third part of an instance's pin and it is the only
    part that can catch a version republished with a different body.

    Compared between two manifests rather than against a stored constant, so this cannot
    pass by agreeing with itself."""
    same_identity = _manifest(persona="A different set of instructions entirely.")
    assert content_digest(_manifest()) != content_digest(same_identity)


def test_a_naive_signing_time_is_refused() -> None:
    """`signed_at` is what `ownership` reports as the moment the template's paths were set,
    and an upgrade review compares it against a local edit's timestamp. Naive, it is hours
    out in whichever direction the host sits and neither direction announces itself."""
    manifest = _manifest()
    with pytest.raises(ValidationError, match="timezone-aware"):
        SignedManifest(
            manifest=manifest,
            content_digest=content_digest(manifest),
            signature=_signed(manifest).signature,
            signed_by=PUBLISHER,
            signed_at=datetime(2026, 9, 6, 9, 0),
        )


# --------------------------------------------------------------- M13.2.2 manifest schema
def test_the_manifest_document_carries_every_thing_the_leaf_names() -> None:
    """Identity, persona, skills, tools, connectors, scope, tier, leash, golden set and
    placeholders: the ten the work breakdown asks for.

    Asserted on the values a populated manifest produces rather than on the path list, so a
    path that exists and is never filled in is caught too. Delete this and the manifest can
    lose a whole section while every hash test still passes, because a smaller document
    hashes perfectly well."""
    document = _manifest(leash=(LeashRung(target="ticket", rung=AutonomyTier.ASSISTED),)).document()
    assert document["identity.template_id"] == TEMPLATE
    assert document["identity.version"] == 1
    assert document["persona"].startswith("Answer support questions")  # type: ignore[union-attr]
    assert document["skills"] == [{"name": "triage_playbook", "digest": SKILL_DIGEST}]
    assert document["authority.allowed_tools"] == ["client.read_summary"]
    assert document["authority.required_tools"] == []
    assert document["connectors"] == ["freshdesk"]
    assert document["authority.scope"] == {
        "clauses": [{"field": "department", "op": "eq", "value": "web"}]
    }
    assert document["tier"] == "main"
    assert document["guardrails.leash"] == [
        {"target": "ticket", "scope": {"clauses": []}, "rung": int(AutonomyTier.ASSISTED)}
    ]
    assert document["guardrails.max_side_effect"] == "none"
    assert document["golden_set"] == [
        {"question": "what is blocking ticket 91?", "expectation": "names the blocker"}
    ]
    assert document["placeholders"] == [
        {
            "key": "escalation_contact",
            "prompt": "Who is escalated to out of hours?",
            "required": True,
        }
    ]


def test_a_document_holds_exactly_the_paths_the_constraint_expects() -> None:
    """`document` is walked by `MANIFEST_PATHS` and the check constraints on both tables
    demand every one and nothing else. A field added to the model without being added to
    that tuple is a value that never reaches the database; a path added to the tuple
    without a field raises here rather than producing a document PostgreSQL then refuses.

    Delete this and the two halves drift and the failure arrives as an insert nobody can
    explain."""
    assert sorted(_manifest().document()) == sorted(MANIFEST_PATHS)
    assert sorted(BLANK_MANIFEST.document()) == sorted(MANIFEST_PATHS)


def test_a_tool_list_is_stored_in_one_order_whatever_order_it_arrived_in() -> None:
    """A `frozenset` dumped to JSON orders itself by the process's hash seed, so a digest
    taken over one would differ between two workers running the same code on the same
    manifest, and the symptom is a cache that misses for part of the fleet while every test
    on one machine passes.

    Written with an unsorted input on purpose. A fixture that was already in order would
    make a sort that does nothing look correct."""
    jumbled = _manifest(allowed=("ticket.read_status", "client.read_summary"))
    ordered = _manifest(allowed=("client.read_summary", "ticket.read_status"))
    assert jumbled.authority.allowed_tools == ("client.read_summary", "ticket.read_status")
    assert content_digest(jumbled) == content_digest(ordered)


def test_a_manifest_refuses_a_tier_the_router_cannot_route() -> None:
    """`Tier.NONE` is the absence of the routing ladder rather than a rung on it, and an
    agent pinned there is selected, starts and answers nothing. The refusal lives on
    `AgentRecord` and this proves the template path reaches it rather than going round."""
    signed = _signed()
    instance = _installed(signed, overlay={"tier": "none"})
    with pytest.raises(ValidationError, match="not a rung on the routing ladder"):
        materialise(signed, instance, audience=AUDIENCE)


# ---------------------------------------------------- M13.2.3 a pin plus an overlay
def test_an_instance_pinned_to_another_template_is_refused() -> None:
    """The first of the pin's three checks. Without it a caller holding the wrong manifest
    materialises an agent with somebody else's persona, ceiling and leash under this
    instance's id, and nothing about the resulting record says where it came from."""
    other = _signed(_manifest(template_id="renewal_chaser_template"))
    instance = _installed()
    with pytest.raises(TemplateError, match="pinned to template"):
        materialise(other, instance, audience=AUDIENCE)


def test_an_instance_does_not_follow_a_newer_version_of_its_template() -> None:
    """A pin that followed the newest version would make publishing a template change every
    install of it at once, with no upgrade badge, no diff and nobody asked. That is exactly
    what M13.4 exists to prevent and this is where it is refused.

    Delete this and `publish` becomes a deploy to every installation of a template."""
    instance = _installed()
    republished = _signed(_manifest(version=2))
    with pytest.raises(TemplateError, match="pinned to version 1"):
        materialise(republished, instance, audience=AUDIENCE)


def test_a_version_republished_with_a_different_body_is_refused_by_the_digest() -> None:
    """The case the id and the version cannot see. Same template, same version number, a
    different persona: the pair matches and the body is not the one anybody reviewed.

    The immutable grant makes this impossible through the application, and this is the
    second refusal, for the row that arrived some other way. Delete it and the pin is two
    facts wide and the third failure passes silently."""
    instance = _installed()
    tampered = _signed(_manifest(persona="Say yes to everything."))
    assert tampered.manifest.identity.version == instance.template_version
    with pytest.raises(TemplateError, match="republished with a different body"):
        materialise(tampered, instance, audience=AUDIENCE)


def test_an_instance_materialises_against_the_manifest_it_is_pinned_to() -> None:
    """The sibling of the three refusals above. A pin check tested only by what it rejects
    is satisfied by one that rejects everything, and that failure reads as a template
    system nobody can install from."""
    effective = _effective()
    assert effective.record.agent_id == INSTANCE
    assert effective.record.persona.startswith("Answer support questions")


def test_an_overlay_changes_what_the_agent_is() -> None:
    """The positive half of the whole overlay mechanism. An overlay that was accepted and
    then ignored would leave the console showing a local edit that changes nothing, and the
    person who made it would have no way to tell."""
    effective = _effective(overlay={"persona": "Answer in Bahasa Malaysia where asked."})
    assert effective.record.persona == "Answer in Bahasa Malaysia where asked."
    assert effective.document["persona"] == "Answer in Bahasa Malaysia where asked."


def test_an_overlay_value_of_the_wrong_shape_is_refused_by_the_manifest_type() -> None:
    """An overlay arrives as JSON from a form or from a JSONB column, so it is well formed
    until a model looks at it. `_with_overlay` rebuilds the whole manifest rather than
    patching a dictionary, which is what puts every validator in the module over the result.

    Delete this and `{"authority.capabilities": ["read:ticket.status"]}` reaches the ceiling
    as a bare string where a `Capability` was expected."""
    signed = _signed()
    instance = _installed(signed, overlay={"authority.capabilities": ["read:ticket.status"]})
    with pytest.raises(ValidationError):
        materialise(signed, instance, audience=AUDIENCE)


# --------------------------------------------------- M13.2.4 per-path field ownership
def test_every_path_has_an_owner_and_the_template_owns_what_nobody_overlaid() -> None:
    """A partial ownership map makes "this field has no owner" and "this field belongs to
    the template" the same answer, and an upgrade review has to tell them apart: the first
    is a gap in the record and the second is a value the upgrade may move without asking.

    Delete this and the map covers only the overlaid paths, which is the subset that needed
    it least."""
    signed = _signed()
    instance = _installed(signed, overlay={"persona": "Local wording."})
    owners = ownership(signed, instance)
    assert sorted(owners) == sorted(MANIFEST_PATHS)
    assert owners["persona"].source is FieldSource.INSTANCE
    assert owners["persona"].set_by == INSTALLER
    assert owners["tier"].source is FieldSource.TEMPLATE
    assert owners["tier"].set_by == PUBLISHER
    assert owners["tier"].set_at == NOW


def test_the_owner_of_a_path_is_whoever_set_it_last_and_not_whoever_installed_it() -> None:
    """ "Who last set each field" is the leaf's own wording and the last is the load-bearing
    part. An ownership record that named the installer for ever would answer the wrong
    question at exactly the moment it is asked, which is when somebody wants to know who
    made the local change that is now conflicting with an upgrade."""
    signed = _signed()
    installed = _installed(signed, overlay={"persona": "First wording."})
    edited = set_field(installed, "persona", "Second wording.", by=SECOND_ADMIN, at=LATER)
    owners = ownership(signed, edited)
    assert owners["persona"].set_by == SECOND_ADMIN
    assert owners["persona"].set_at == LATER
    assert ownership(signed, installed)["persona"].set_by == INSTALLER


def test_clearing_a_field_gives_it_back_to_the_template() -> None:
    """A revert has to change the answer to "who set this", or the record goes on naming
    somebody for a value they no longer hold. After a clear, nobody here set it: the
    template did, and that is what the upgrade path needs to hear so it may move it."""
    signed = _signed()
    edited = _installed(signed, overlay={"persona": "Local wording."})
    reverted = clear_field(edited, "persona")
    assert reverted.overlay == {}
    assert reverted.overlay_owners == {}
    assert ownership(signed, reverted)["persona"].source is FieldSource.TEMPLATE


def test_clearing_a_path_nobody_overlaid_is_not_an_error() -> None:
    """A retry after a timeout must not fail, and there is nothing to refuse: the caller
    asked for a state the record is already in. `brain.agents.lifecycle.enable` makes the
    same choice for the same reason."""
    instance = _installed()
    assert clear_field(instance, "persona") is instance


def test_an_overlaid_value_with_no_owner_is_refused() -> None:
    """An overlay entry nobody owns is a local change that cannot be attributed, which is
    precisely the row an upgrade review has to resolve and cannot. The constructor refuses
    it in both directions, so an owner naming a path that is not overlaid is refused too.

    Delete this and a hand-built row, or a partial write, produces an instance whose
    provenance map is quietly incomplete."""
    with pytest.raises(ValidationError, match="every overlaid path needs an owner"):
        TemplateInstance(
            instance_id=INSTANCE,
            template_id=TEMPLATE,
            template_version=1,
            content_digest=_signed().content_digest,
            overlay={"persona": "Local wording."},
            overlay_owners={},
            created_by=INSTALLER,
        )


# ------------------------------------- M13.2.5 the materialised document and its hash
def test_the_configuration_hash_changes_when_the_overlay_changes() -> None:
    """The one that matters. The hash is one of the five parts of a cache key and it is the
    part that says which agent asked. Taken over the manifest rather than over the
    materialised document, every instance of one template shares it, and an answer computed
    under one instance's persona and tools is served to another under a key that says it is
    fresh. Nothing fails and nobody is told.

    Two overlays are compared with each other rather than a digest with a stored constant,
    so this cannot pass by comparing a value with itself."""
    signed = _signed()
    plain = materialise(signed, _installed(signed), audience=AUDIENCE)
    overlaid = materialise(
        signed,
        _installed(signed, overlay={"persona": "Answer only about billing."}),
        audience=AUDIENCE,
    )
    assert plain.config_hash != overlaid.config_hash


def test_two_agents_differing_only_in_an_overlay_do_not_share_a_cache_entry() -> None:
    """The same property stated where it actually bites, through the real
    `brain.gate.cache_key.key_for` rather than through a digest comparison.

    `agent_config_hash` is a required part of that key and nothing in `src` produced one
    before this module; every caller was a test passing a literal. This is the producer, and
    a producer that returned the same string for two different agents would make the key's
    third component decorative while every one of that module's own tests kept passing.

    Delete this and the seam is untested from the consumer's end, which is the end where a
    person is served somebody else's answer."""
    signed = _signed()
    left = materialise(signed, _installed(signed), audience=AUDIENCE)
    right = materialise(
        signed,
        _installed(signed, instance_id=OTHER_INSTANCE, overlay={"tier": "small"}),
        audience=AUDIENCE,
    )
    question = "what is the standard maintenance retainer"
    ent_hash = "e" * 32
    epochs = {"laravel": 7}
    assert key_for(question, ent_hash, left.config_hash, 4, epochs) != key_for(
        question, ent_hash, right.config_hash, 4, epochs
    )


def test_the_configuration_hash_ignores_who_set_a_field() -> None:
    """Ownership is provenance and never configuration. Two instances holding identical
    values set by different people answer identically and may share a cached answer, so
    putting the owner into the digest would empty the cache on a transfer that changed
    nothing anybody can observe.

    Delete this and the ownership map creeps into the digest, which looks tidy and quietly
    makes every cached answer per-editor."""
    signed = _signed()
    first = _installed(signed, overlay={"persona": "Local wording."})
    second = set_field(first, "persona", "Local wording.", by=SECOND_ADMIN, at=LATER)
    assert ownership(signed, first)["persona"] != ownership(signed, second)["persona"]
    assert (
        materialise(signed, first, audience=AUDIENCE).config_hash
        == materialise(signed, second, audience=AUDIENCE).config_hash
    )


def test_a_configuration_hash_is_never_a_content_digest() -> None:
    """An instance with an empty overlay has an effective document identical to its
    manifest's, so with one domain separator the two digests would be the same 64
    characters and a content digest could be handed to the cache as a configuration hash
    with nothing looking wrong.

    Delete this and `MANIFEST_SCHEMA` and `CONFIG_SCHEMA` can be collapsed into one
    constant as a tidy-up."""
    signed = _signed()
    effective = materialise(signed, _installed(signed), audience=AUDIENCE)
    assert effective.document == signed.manifest.document()
    assert effective.config_hash != signed.content_digest
    assert config_hash(effective.document) == effective.config_hash


def test_the_document_and_the_agent_record_are_built_from_one_object() -> None:
    """The document is what the hash describes and the record is what the agent does, and a
    materialisation in which those come from two places is one where the cache key
    describes a configuration the run does not have.

    Delete this and the document could be assembled by merging the manifest with the
    overlay while the record was built from the manifest alone, which is green under every
    other test in this file."""
    effective = _effective(
        overlay={"persona": "Answer only about billing.", "authority.allowed_tools": ["a.b"]}
    )
    assert effective.record.persona == effective.document["persona"]
    assert (
        sorted(effective.record.authority.allowed_tools)
        == effective.document["authority.allowed_tools"]
    )
    assert effective.record.tier.value == effective.document["tier"]


# ------------------------------------------------------------ M13.2.6 the five sealed paths
def test_an_overlay_may_not_change_a_sealed_path() -> None:
    """The seal, written the obvious way. Each path is spelled out rather than read from
    `SEALED_PATHS`, so a seal repointed at four paths, or at a typo, fails here instead of
    passing by comparing a list with itself.

    Delete this and an install can relax the supervision its publisher signed."""
    for path, value in (
        ("identity.template_id", "something_else"),
        ("identity.version", 99),
        ("identity.published_by", INSTALLER),
        ("guardrails.max_side_effect", "send"),
        ("guardrails.leash", []),
    ):
        with pytest.raises(SealedPathError, match="sealed by the template"):
            _installed(overlay={path: value})


def test_an_overlay_may_not_reach_a_sealed_path_through_the_section_that_holds_it() -> None:
    """`guardrails` is not one of the five strings and setting it replaces both of them.

    This is the spelling a deny list cannot see, and the reason the seal ships with a
    companion rule that an overlay may mention nothing but the settable paths. Delete this
    and a seal implemented as five string comparisons looks watertight."""
    with pytest.raises(TemplateError):
        _installed(overlay={"guardrails": {"max_side_effect": "money", "leash": []}})


def test_an_overlay_may_not_reach_inside_a_sealed_path() -> None:
    """And the other direction: a leaf below a sealed path. `guardrails.leash.0.rung`
    equals no sealed path either, and it raises an autonomy rung on one target, which is
    the smallest and most plausible version of the edit the seal exists to refuse."""
    with pytest.raises(TemplateError):
        _installed(overlay={"guardrails.leash.0.rung": int(AutonomyTier.AUTONOMOUS)})


def test_an_overlay_may_not_mention_a_path_the_manifest_does_not_have() -> None:
    """A typo in a path is a local edit that silently applies to nothing, and the person who
    made it sees a saved form and an agent that did not change. It is also how the two
    spellings above get through, so refusing unknown paths is what makes the seal total
    rather than a list of five strings."""
    with pytest.raises(TemplateError, match="not a path this manifest has"):
        _installed(overlay={"persona_extra": "..."})


def test_a_settable_path_is_settable() -> None:
    """The sibling every refusal needs. A seal tested only by what it refuses is satisfied
    by one that refuses everything, and that failure is an install wizard where nothing can
    be configured at all."""
    for path in SETTABLE_PATHS:
        assert path not in SEALED_PATHS
    effective = _effective(overlay={"identity.display_name": "Support Triage, East"})
    assert effective.record.display_name == "Support Triage, East"


def test_every_sealed_path_is_a_path_a_manifest_actually_produces() -> None:
    """A typo in `SEALED_PATHS` seals nothing and is invisible: `check_overlay` goes on
    refusing the misspelled string, the constraint goes on naming it, and the real path
    quietly becomes settable because the settable set is the manifest's paths minus the
    sealed ones.

    Compared against the document a manifest produces rather than against `MANIFEST_PATHS`,
    so this checks the paths that exist rather than the list that claims they do."""
    produced = set(_manifest().document())
    assert set(SEALED_PATHS) <= produced
    assert set(SETTABLE_PATHS) <= produced
    assert set(SEALED_PATHS) | set(SETTABLE_PATHS) == produced
    assert not set(SEALED_PATHS) & set(SETTABLE_PATHS)


def test_there_are_five_sealed_paths() -> None:
    """The leaf says five and the count is the part a reader checks against the schema. A
    sixth is a decision somebody should have argued for in the docstring; a fourth is a
    seal that lost one without anybody noticing, because every other test here still
    passes for the four that remain."""
    assert len(SEALED_PATHS) == 5


# ---------------------------------------------------------- M13.2.7 the blank template
def test_a_hand_built_agent_is_an_install_of_the_blank_template() -> None:
    """One code path, stated as a fact about the object rather than as a claim in a
    docstring: an agent somebody wrote from nothing is pinned, overlaid, owned and hashed
    exactly like an installed one.

    Delete this and a second constructor for hand-built agents is an easy addition, and
    from then on every rule this module enforces has a way round it."""
    instance = hand_built(
        key=KEY,
        instance_id="finance_helper",
        created_by=INSTALLER,
        at=NOW,
        overlay={"persona": "Answer questions about the finance handbook."},
    )
    assert instance.template_id == BLANK_TEMPLATE_ID
    assert instance.template_version == 1
    assert instance.overlay_owners["persona"].source is FieldSource.INSTANCE
    effective = materialise(blank_template(key=KEY, at=NOW), instance, audience=AUDIENCE)
    assert effective.record.persona == "Answer questions about the finance handbook."


def test_a_hand_built_agent_cannot_raise_its_own_side_effect_ceiling() -> None:
    """This is what makes the blank template safe to be the one code path. Its sealed values
    are the floor rather than the widest available, so a hand-built agent reads and never
    sends however its overlay is written.

    Delete this and the blank template becomes the obvious place to relax the two guardrails
    "just for hand-built agents", which is every agent nobody reviewed."""
    with pytest.raises(SealedPathError):
        hand_built(
            key=KEY,
            instance_id="finance_helper",
            created_by=INSTALLER,
            at=NOW,
            overlay={"persona": "...", "guardrails.max_side_effect": "money"},
        )
    effective = materialise(
        blank_template(key=KEY, at=NOW),
        hand_built(
            key=KEY,
            instance_id="finance_helper",
            created_by=INSTALLER,
            at=NOW,
            overlay={"persona": "..."},
        ),
        audience=AUDIENCE,
    )
    assert effective.record.authority.max_side_effect is SideEffect.NONE


def test_a_hand_built_agent_is_shadow_on_every_target_until_a_template_says_otherwise() -> None:
    """The blank template's leash is empty and `Leash.rung_for` answers SHADOW for a target
    nobody configured, so the agent nobody reviewed is the one that simulates everything.

    Asked of the real `brain.gate.leash.Leash` rather than of the manifest, because the
    property is about what the leash does with an empty entry list and a stand-in would be
    asserting this file's idea of it."""
    effective = materialise(
        blank_template(key=KEY, at=NOW),
        hand_built(
            key=KEY,
            instance_id="finance_helper",
            created_by=INSTALLER,
            at=NOW,
            overlay={"persona": "..."},
        ),
        audience=AUDIENCE,
    )
    assert effective.leash.entries == ()
    assert effective.leash.rung_for("finance_helper", "invoice", {}) is AutonomyTier.SHADOW


def test_a_hand_built_agent_with_no_persona_is_refused_by_the_record_every_agent_goes_through() -> (
    None
):
    """The blank template's persona is empty, so a hand-built agent that supplies none is
    refused, and it is refused by `AgentRecord`'s own validator rather than by a check
    written here. One rule about what an agent needs, in one place.

    Delete this and the blank template acquires a placeholder persona so that
    materialisation always succeeds, which is an agent configured to say nothing in
    particular."""
    with pytest.raises(ValidationError):
        materialise(
            blank_template(key=KEY, at=NOW),
            hand_built(
                key=KEY, instance_id="finance_helper", created_by=INSTALLER, at=NOW, overlay={}
            ),
            audience=AUDIENCE,
        )


# ---------------------------------------------------- the seams into what already shipped
def test_the_materialised_record_is_the_one_the_gate_already_takes() -> None:
    """`materialise` builds a real `AgentRecord` through its own constructor, so the two
    producers `brain.agents.model` wrote for the gate take it without adaptation and the
    invariant is computed by `EntitlementSet.intersect` rather than by anything here.

    Delete this and the template layer can drift into producing its own agent shape, which
    is a second description of an agent for the first one to disagree with."""
    effective = _effective()
    assert isinstance(effective.record, AgentRecord)
    ceiling = tool_ceiling(effective.record)
    assert ceiling.agent_id == INSTANCE
    assert ceiling.allowed_tools == frozenset({"client.read_summary"})
    caller = EntitlementSet(
        principal_id=INSTALLER,
        grants=(
            Grant(capability=Capability(value="read:ticket.status"), scope=Scope.unrestricted()),
            Grant(capability=Capability(value="read:client.name"), scope=Scope.unrestricted()),
        ),
    )
    run = caller.intersect(entitlement_ceiling(effective.record))
    assert {g.capability.value for g in run.grants} == {"read:ticket.status"}
    assert run.principal_id == INSTALLER


def test_the_leash_a_template_declares_is_bound_to_the_instances_own_agent_id() -> None:
    """A manifest carries rungs with no agent id, because a template does not know which
    agent it will become. Bound to the wrong id, or left unbound, every entry matches
    nothing: `Leash.matching` compares the agent id exactly, so the rung would silently
    fall back to SHADOW and the template's supervision decision would apply to no agent at
    all.

    That failure is invisible in the safe direction, which is why it needs a test."""
    signed = _signed(
        _manifest(leash=(LeashRung(target="ticket.update_status", rung=AutonomyTier.ASSISTED),))
    )
    effective = materialise(signed, _installed(signed), audience=AUDIENCE)
    assert [e.agent_id for e in effective.leash.entries] == [INSTANCE]
    rung = effective.leash.rung_for(INSTANCE, "ticket.update_status", {})
    assert rung is AutonomyTier.ASSISTED
    assert effective.leash.rung_for("another_agent", "ticket.update_status", {}) is (
        AutonomyTier.SHADOW
    )


def test_a_skill_a_template_names_becomes_a_pin_on_the_instance() -> None:
    """`brain.tools.skills.resolve_pin` refuses a skill whose body has changed since it was
    pinned, and it can only do that if somebody produced a `SkillPin`. A template that
    named skills and produced no pins would let an install follow an edited procedure it
    was never tested against."""
    effective = _effective()
    assert [(p.agent_id, p.skill_name, p.digest) for p in effective.skill_pins] == [
        (INSTANCE, "triage_playbook", SKILL_DIGEST)
    ]


def test_a_template_carries_no_audience_anywhere() -> None:
    """`AUDIENCE_IS_NOT_AUTHORITY`, at the layer where a configuration travels between
    installations. A manifest that carried a visibility level would publish an agent into a
    company it has never seen, and an instance that carried one would be a second copy of a
    fact `agent.agent` already holds, free to disagree with the one selection reads.

    Asserted as the absence of any such path and as the audience being a parameter, so a
    field added under either name fails here."""
    for path in MANIFEST_PATHS:
        assert "visibility" not in path
        assert "audience" not in path
        assert "owner" not in path
    for field in TemplateInstance.model_fields:
        assert field not in {"audience", "visibility", "owner_id", "department"}
    company = AgentAudience(level=Visibility.COMPANY, owner_id=INSTALLER)
    signed = _signed()
    instance = _installed(signed)
    narrow = materialise(signed, instance, audience=AUDIENCE)
    wide = materialise(signed, instance, audience=company)
    assert tool_ceiling(narrow.record) == tool_ceiling(wide.record)
    assert entitlement_ceiling(narrow.record) == entitlement_ceiling(wide.record)
