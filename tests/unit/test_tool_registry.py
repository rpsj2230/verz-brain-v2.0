"""The tool registry and the skill loader, rule by rule.

These are the behaviours a tool author meets. The rules that must never break whatever
anybody edits live next door in `tests/invariants/test_tool_registry_invariants.py`; this
file is the one that says what each refusal actually does.

Task ids: M12.1.1, M12.1.2, M12.1.3, M12.1.4, M12.1.5, M12.2.1, M12.2.4, M12.2.5,
M12.2.6, M12.2.7, M12.2.8, M12.2.9
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.envelope import Entity, IdentityMode, SideEffect, ToolDefinition, TypedResult
from brain.core.redaction import OPAQUE_CAPABILITY, UntypedShapeError
from brain.core.scope import Clause, Op, Scope
from brain.gate.catalogue import AgentCeiling
from brain.gate.injection import AutonomyTier
from brain.tools.registry import (
    RUN_SKILL_SCRIPT,
    ResultContract,
    ToolRegistrationError,
    ToolRegistry,
    assert_tool_name,
    default_rung,
    rung_ceiling,
)
from brain.tools.skills import (
    ImportedSkill,
    Skill,
    SkillCard,
    SkillError,
    SkillPin,
    SkillSource,
    SkillState,
    SourceKind,
    body_of,
    card_for,
    diff_skills,
    execution_tool,
    offered_cards,
    parse_frontmatter,
    pin_skill,
    required_capabilities,
    resolve_pin,
    safe_archive_member,
    safe_archive_members,
    skill_from_markdown,
    skill_reach,
    unknown_tools,
)

NOW = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
COMMIT = "a" * 40
BYTES_DIGEST = "b" * 64


class ClientRow(Entity):
    """A record shaped the way the redactor needs, so a handler can promise to return one."""

    name: str = ""


def a_typed_handler() -> TypedResult[ClientRow]:
    return TypedResult[ClientRow]()


def an_untyped_handler() -> dict[str, str]:
    return {}


def an_unannotated_handler():
    """No return annotation, deliberately. It is the fixture for the default-deny rule."""
    return TypedResult[ClientRow]()


def _definition(
    name: str = "client.read_summary",
    *,
    capability: str = "read:client.name",
    effect: SideEffect = SideEffect.NONE,
    mode: IdentityMode = IdentityMode.DELEGATED,
    entity: str = "client",
    description: str = "reads a client summary",
    source: str = "",
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description,
        entity=entity,
        required_capability=capability,
        side_effect=effect,
        identity_mode=mode,
        source=source,
    )


def _department(name: str) -> Scope:
    return Scope(clauses=(Clause(field="department", op=Op.EQ, value=name),))


def _skill(**overrides: object) -> Skill:
    base: dict[str, object] = {
        "name": "hosting-expiry",
        "description": "check hosting and domain expiry together",
        "tools": ("client.read_summary",),
        "body": "1. read the client\n2. say when it expires",
    }
    base.update(overrides)
    return Skill.model_validate(base)


def _source() -> SkillSource:
    return SkillSource(kind=SourceKind.GITHUB, location="verz/skills", commit=COMMIT)


def _imported(skill: Skill | None = None) -> ImportedSkill:
    return ImportedSkill(skill=skill or _skill(), source=_source())


# ------------------------------------------------------------ the name (M12.1.1)


def test_a_tool_name_is_source_verb_noun() -> None:
    """The happy path. If this fails, nothing else in the file is testing a registry that
    can hold anything."""
    registry = ToolRegistry()
    registered = registry.register(_definition(), a_typed_handler)
    assert registered.name == "client.read_summary"
    assert registered.source == "client"
    assert registered.object_name == "client"


@pytest.mark.parametrize(
    "name",
    [
        "client.read",
        "read_summary",
        "client..read_summary",
        "client.read_",
        "client._read",
        "Client.read_summary",
        "client.read summary",
    ],
)
def test_a_name_that_is_not_source_verb_noun_is_refused(name: str) -> None:
    """Without this the model is shown a name that does not say what the tool acts on, and
    it either never picks it or picks it for the wrong reason. Neither raises anything.

    Asserted against the rule directly, because several of these are also refused by
    `ToolDefinition`'s own looser pattern and would never reach the registry."""
    with pytest.raises(ToolRegistrationError, match=r"source\.verb_noun"):
        assert_tool_name(name)


def test_the_registry_refuses_a_name_the_envelope_would_accept() -> None:
    """`ToolDefinition` and `brain.ops.sweeps` both admit `client.read`, because both are
    written as name-dot-name. Delete this and the strict half of the grammar is gone, and
    the only tools that lose it are the ones whose names stopped saying what they act on."""
    registry = ToolRegistry()
    with pytest.raises(ToolRegistrationError, match=r"source\.verb_noun"):
        registry.register(_definition("client.read"), a_typed_handler)


def test_a_declared_source_that_disagrees_with_the_name_is_refused() -> None:
    """Delete this and a tool named for Xero can run against HubSpot's credential, with the
    trace naming the system in the tool's name rather than the one that was called."""
    registry = ToolRegistry()
    with pytest.raises(ToolRegistrationError, match="declares source"):
        registry.register(_definition(source="hubspot"), a_typed_handler)


def test_an_object_name_no_field_policy_could_match_is_refused() -> None:
    """`ToolDefinition.entity` carries no pattern of its own. Without this check a tool can
    declare `Client Ltd`, match no policy rule, and have every field of every record it
    returns withheld as unclassified, which reads as a permission bug for weeks."""
    registry = ToolRegistry()
    with pytest.raises(ToolRegistrationError, match="not a name"):
        registry.register(_definition(entity="Client Ltd"), a_typed_handler)


# ------------------------------------------------------ the capability (M12.1.2)


def test_a_malformed_required_capability_is_refused_at_registration() -> None:
    """The catalogue already treats an unparseable capability as unreachable, silently. This
    is the half that tells the person who wrote the typo."""
    registry = ToolRegistry()
    with pytest.raises(ToolRegistrationError, match="not a capability"):
        registry.register(_definition(capability="read client name"), a_typed_handler)


def test_an_unknown_verb_is_refused_like_any_other_malformed_capability() -> None:
    """`Capability` closes the verb set. Without this the registry would accept
    `peek:client.name`, which no grant can ever cover, so the tool would be invisible."""
    registry = ToolRegistry()
    with pytest.raises(ToolRegistrationError, match="not a capability"):
        registry.register(_definition(capability="peek:client.name"), a_typed_handler)


def test_the_registry_hands_back_a_capability_and_never_a_string() -> None:
    """If this returned the string, every caller would parse it again and each of them would
    decide for itself what an unparseable one meant."""
    registry = ToolRegistry()
    registered = registry.register(_definition(), a_typed_handler)
    assert registered.capability == Capability(value="read:client.name")
    assert isinstance(registered.capability, Capability)


def test_a_side_effecting_tool_that_asks_only_to_read_is_refused() -> None:
    """Delete this and everybody who can read an invoice can send one, because the
    catalogue admits the send tool on the strength of the read grant."""
    registry = ToolRegistry()
    with pytest.raises(ToolRegistrationError, match="asking only"):
        registry.register(
            _definition(
                "invoice.send_reminder",
                capability="read:invoice.status",
                effect=SideEffect.SEND,
                entity="invoice",
            ),
            a_typed_handler,
        )


def test_a_read_only_tool_may_require_more_than_it_needs() -> None:
    """The asymmetry is deliberate: over-strict narrows and discloses nothing, so refusing
    it would be a rule with no failure behind it."""
    registry = ToolRegistry()
    registry.register(_definition(capability="write:client.name"), a_typed_handler)
    assert registry.names() == ("client.read_summary",)


# ------------------------------------------------------- identity mode and scope


def test_a_service_mode_tool_without_a_scope_is_refused() -> None:
    """The source will not narrow a shared credential for us. Without this a service tool
    reaches every row that credential reaches, and nothing in the gate can see it."""
    registry = ToolRegistry()
    with pytest.raises(ToolRegistrationError, match="no scope"):
        registry.register(_definition(mode=IdentityMode.SERVICE), a_typed_handler)


def test_a_service_mode_tool_with_an_unrestricted_scope_is_refused() -> None:
    """`Scope()` satisfies "it has a scope" and admits every row. Without this the rule is
    enforceable only by whoever remembers what it was for."""
    registry = ToolRegistry()
    with pytest.raises(ToolRegistrationError, match="unrestricted scope"):
        registry.register(_definition(mode=IdentityMode.SERVICE), a_typed_handler, scope=Scope())


def test_a_service_mode_tool_with_a_narrowing_scope_registers() -> None:
    """The rule has to admit the legitimate case, or every service tool gets written as a
    delegated one to get past it."""
    registry = ToolRegistry()
    registered = registry.register(
        _definition(mode=IdentityMode.SERVICE), a_typed_handler, scope=_department("finance")
    )
    assert registered.scope == _department("finance")


def test_a_delegated_tool_needs_no_scope() -> None:
    """A delegated call runs under the caller's own token, so the source enforces its own
    permissions too. Demanding a scope here would be a rule with no failure behind it."""
    registry = ToolRegistry()
    assert registry.register(_definition(), a_typed_handler).scope is None


# --------------------------------------------------------------- one name, one tool


def test_two_tools_may_not_share_a_name() -> None:
    """Without this, which tool a caller reaches is decided by import order, and the loser
    is invisible rather than broken."""
    registry = ToolRegistry()
    registry.register(_definition(), a_typed_handler)
    with pytest.raises(ToolRegistrationError, match="import order"):
        registry.register(_definition(description="a different tool"), a_typed_handler)


def test_get_refuses_an_unregistered_name_rather_than_returning_none() -> None:
    """Returning None would put the decision about a missing tool in every caller, and the
    cheapest thing a caller does with None is skip it."""
    registry = ToolRegistry()
    with pytest.raises(ToolRegistrationError, match="no tool is registered"):
        registry.get("client.read_summary")


# --------------------------------------------------- the result contract (M12.1.4)


def test_a_tool_with_no_return_annotation_is_refused() -> None:
    """Default-deny on shape. Without it, an unannotated tool reaches the redactor at
    request time, after the fetch, and the answer is an exception."""
    registry = ToolRegistry()
    with pytest.raises(UntypedShapeError, match="declares no return type"):
        registry.register(_definition(), an_unannotated_handler)


def test_a_tool_that_returns_a_dict_is_refused() -> None:
    """A dict gives the redactor no entity to ask a capability question about, so it would
    have to pass it through unchecked or drop it whole."""
    registry = ToolRegistry()
    with pytest.raises(UntypedShapeError, match="only TypedResult"):
        registry.register(_definition(), an_untyped_handler)


def test_an_opaque_tool_must_require_the_opaque_capability() -> None:
    """Without this the tool is described to a model, selected, and refused by the redactor
    after the fetch, which is exactly the described-then-refused leak the catalogue exists
    to prevent."""
    registry = ToolRegistry()
    with pytest.raises(ToolRegistrationError, match="opaque"):
        registry.register(
            _definition("drive.read_file"),
            a_typed_handler,
            result_contract=ResultContract.OPAQUE,
        )


def test_an_opaque_tool_registers_when_it_requires_the_opaque_capability() -> None:
    """The escape hatch has to remain usable, or the first tool that needs it is written as
    a typed one that lies about its shape."""
    registry = ToolRegistry()
    registered = registry.register(
        _definition("drive.read_file", capability=OPAQUE_CAPABILITY.value),
        a_typed_handler,
        result_contract=ResultContract.OPAQUE,
    )
    assert registered.result_contract is ResultContract.OPAQUE


# ------------------------------------------------ one way to run a script (M12.2.9)


def test_a_second_tool_may_not_claim_the_skill_script_object() -> None:
    """Delete this and there are two paths to running a skill's scripts, while the sandbox,
    the leash and the output redaction sit on one of them."""
    registry = ToolRegistry()
    with pytest.raises(ToolRegistrationError, match="reserved"):
        registry.register(
            _definition(
                "skill.run_anything", capability="invoke:skill_script", entity="skill_script"
            ),
            a_typed_handler,
        )


def test_the_one_execution_tool_may_claim_the_skill_script_object() -> None:
    """The reservation has to admit its own holder, or the rule refuses the tool it exists
    to protect."""
    registry = ToolRegistry()
    registered = registry.register(
        _definition(RUN_SKILL_SCRIPT, capability="invoke:skill_script", entity="skill_script"),
        a_typed_handler,
    )
    assert registered.name == execution_tool()


# --------------------------------------------------- startup validation (M12.1.5)


def test_a_frozen_registry_refuses_further_registration() -> None:
    """A tool that appears after a catalogue has been projected is a tool whose presence
    depends on when the request arrived."""
    registry = ToolRegistry().freeze()
    with pytest.raises(ToolRegistrationError, match="after the registry was frozen"):
        registry.register(_definition(), a_typed_handler)


def test_freezing_refuses_two_tools_described_identically() -> None:
    """Names are unique and descriptions are the other half of what the model chooses from.
    Two tools described the same way are chosen between by position in a list."""
    registry = ToolRegistry()
    registry.register(_definition("client.read_summary"), a_typed_handler)
    registry.register(_definition("client.read_profile"), a_typed_handler)
    with pytest.raises(ToolRegistrationError, match="share the description"):
        registry.freeze()


def test_freezing_reports_every_finding_at_once() -> None:
    """Whoever is fixing this is looking at a build. A build that reveals one problem per
    run takes an afternoon."""
    registry = ToolRegistry()
    registry.register(_definition("client.read_summary"), a_typed_handler)
    registry.register(_definition("client.read_profile"), a_typed_handler)
    registry.register(
        _definition("ticket.read_one", entity="ticket", description="a ticket"), a_typed_handler
    )
    registry.register(
        _definition("ticket.read_two", entity="ticket", description="a ticket"), a_typed_handler
    )
    assert len(registry.validate()) == 2


def test_a_clean_registry_freezes() -> None:
    """The check has to pass on a correct registry, or startup validation is a thing people
    switch off."""
    registry = ToolRegistry()
    registry.register(_definition(), a_typed_handler)
    assert registry.freeze().is_frozen


def test_iterating_a_registry_yields_definitions_in_name_order() -> None:
    """This is what `project` consumes. An unstable order defeats prompt caching just as
    thoroughly as a varying membership, and does it invisibly."""
    registry = ToolRegistry()
    registry.register(
        _definition("ticket.read_status", capability="read:ticket.status", entity="ticket"),
        a_typed_handler,
    )
    registry.register(_definition(), a_typed_handler)
    assert [t.name for t in registry] == ["client.read_summary", "ticket.read_status"]
    assert len(registry) == 2


def test_object_names_are_the_list_the_collision_sweep_needs() -> None:
    """`sweep_slug_collisions` reads tool objects out of source text, which misses every
    tool built from a connector manifest. This is the same list from what is registered."""
    registry = ToolRegistry()
    registry.register(_definition(), a_typed_handler)
    registry.register(
        _definition("ticket.read_status", capability="read:ticket.status", entity="ticket"),
        a_typed_handler,
    )
    assert registry.object_names() == ("client", "ticket")


# ------------------------------------------- side effect drives the leash (M12.1.3)


def test_the_default_rung_falls_as_the_side_effect_grows() -> None:
    """Without a mapping, every tool an agent holds is exercised at whatever rung the agent
    was configured with, and reading a ticket is supervised like sending an invoice."""
    assert default_rung(SideEffect.NONE) is AutonomyTier.AUTONOMOUS
    assert default_rung(SideEffect.DRAFT) is AutonomyTier.ASSISTED
    assert default_rung(SideEffect.WRITE) is AutonomyTier.ASSISTED
    assert default_rung(SideEffect.SEND) is AutonomyTier.ASSISTED
    assert default_rung(SideEffect.MONEY) is AutonomyTier.SHADOW


def test_a_side_effect_tightens_a_configured_rung() -> None:
    """The only shape in which a default may touch a leash. Delete it and the mapping
    becomes a per-tool default, which is the widening `brain.gate.leash` refuses to have."""
    assert rung_ceiling(AutonomyTier.AUTONOMOUS, SideEffect.MONEY) is AutonomyTier.SHADOW
    assert rung_ceiling(AutonomyTier.SHADOW, SideEffect.NONE) is AutonomyTier.SHADOW


# ------------------------------------------------ the SKILL.md format (M12.2.1)


def test_a_skill_md_parses_into_declarations_and_a_body() -> None:
    """The happy path for the format the whole import pipeline is built on."""
    fields, body = parse_frontmatter(
        "---\nname: hosting-expiry\ntools: [client.read_summary]\n---\nstep one\n"
    )
    assert fields == {"name": "hosting-expiry", "tools": ("client.read_summary",)}
    assert body == "step one"


def test_a_skill_md_declaring_a_capability_is_refused_by_name() -> None:
    """A skill arrives from outside the company. If a declaration could grant, importing a
    file would be a way to grant, and the review would be reviewing prose."""
    with pytest.raises(SkillError, match="declares no reach of its own"):
        parse_frontmatter("---\nname: x\ncapabilities: [read:client.contract_value]\n---\n")


def test_an_unknown_frontmatter_key_is_refused_rather_than_ignored() -> None:
    """An ignored declaration reads exactly like an honoured one to the person who wrote
    it, which is how somebody believes a skill is restricted when it is not."""
    with pytest.raises(SkillError, match="not one of"):
        parse_frontmatter("---\nname: x\ndescription: y\nowner: finance\n---\n")


def test_a_duplicated_frontmatter_key_is_refused() -> None:
    """YAML takes the last one. A reviewer reads top to bottom and approves the first."""
    with pytest.raises(SkillError, match="twice"):
        parse_frontmatter("---\nname: x\ntools: [a.read_b]\ntools: [c.read_d]\n---\n")


def test_a_skill_md_that_does_not_open_with_a_fence_is_refused() -> None:
    """Searching for a fence anywhere would make a code block in the middle of a README
    into the declarations of a skill nobody wrote."""
    with pytest.raises(SkillError, match="first line"):
        parse_frontmatter("# Hosting\n---\nname: x\n---\n")


def test_an_unclosed_fence_is_refused() -> None:
    """Without this the whole document is read as declarations and the body is empty, so a
    reviewer approves a skill whose procedure has silently vanished."""
    with pytest.raises(SkillError, match="never closed"):
        parse_frontmatter("---\nname: x\ndescription: y\n")


def test_a_skill_naming_something_that_is_not_a_tool_name_is_refused() -> None:
    """A skill saying `read the ticket` names nothing that could ever be registered, and
    the author should hear that from the parser rather than from an agent that used no
    tools at all."""
    with pytest.raises(SkillError, match=r"source\.verb_noun"):
        skill_from_markdown("---\nname: x\ndescription: y\ntools: [read the ticket]\n---\nbody")


def test_a_single_tool_without_brackets_reads_as_one_item() -> None:
    """The commonest way to write one item. Guessing here is safe because the guess still
    has to be a real tool name, so a wrong one produces an unknown tool, never an extra."""
    skill = skill_from_markdown("---\nname: x\ndescription: y\ntools: a.read_b\n---\nbody")
    assert skill.tools == ("a.read_b",)


def test_an_empty_list_declares_no_tools() -> None:
    """A skill that uses no tools is a legitimate thing to write, and an empty list must not
    read as the string `[]` and then fail the tool-name check with a confusing message."""
    assert skill_from_markdown("---\nname: x\ndescription: y\ntools: []\n---\nbody").tools == ()


def test_an_unclosed_frontmatter_list_is_refused() -> None:
    """Without this, `tools: [a.read_b` reads as the scalar `[a.read_b`, fails the tool-name
    check, and the author is told about a grammar rather than about the bracket."""
    with pytest.raises(SkillError, match="not closed"):
        parse_frontmatter("---\nname: x\ntools: [a.read_b\n---\n")


def test_a_frontmatter_line_that_is_not_key_value_is_refused() -> None:
    """A line with no colon declares nothing. Skipping it silently is how half a
    declaration survives a bad paste and nobody notices the missing half."""
    with pytest.raises(SkillError, match="not `key: value`"):
        parse_frontmatter("---\nname: x\njust some prose\n---\n")


def test_a_github_source_that_is_not_owner_repo_is_refused() -> None:
    """A location that is not a repository cannot be fetched, and the import would fail
    with whatever the http client said rather than with what was wrong."""
    with pytest.raises(ValueError, match="owner/repo"):
        SkillSource(
            kind=SourceKind.GITHUB, location="https://github.com/verz/skills", commit=COMMIT
        )


def test_a_non_github_source_may_not_carry_a_commit() -> None:
    """A commit on an upload means somebody believes it is pinned to one. It is not, and
    the digest is the only thing that pins it."""
    with pytest.raises(ValueError, match="carries no commit"):
        SkillSource(
            kind=SourceKind.UPLOAD,
            location="skills.zip",
            commit=COMMIT,
            content_digest=BYTES_DIGEST,
        )


def test_an_over_long_archive_member_name_is_refused() -> None:
    """A path long enough to matter is a path built to defeat something, usually a buffer
    or a display that truncates the interesting end of it."""
    with pytest.raises(SkillError, match="over the"):
        safe_archive_member("a/" * 120 + "b")


# ------------------------------------------------- archive members (M12.2.4)


@pytest.mark.parametrize(
    "member",
    [
        "../etc/passwd",
        "skills/../../etc/passwd",
        "/etc/passwd",
        "C:/Windows/System32/x.dll",
        "..\\..\\windows\\x.dll",
        "skills/",
        "",
        "skills/\x00evil",
    ],
)
def test_an_archive_member_that_could_be_written_outside_the_root_is_refused(member: str) -> None:
    """Every one of these writes somewhere nobody chose. The backslash cases are the ones a
    check written on Linux misses and a Windows host honours."""
    with pytest.raises(SkillError):
        safe_archive_member(member)


def test_an_ordinary_member_name_is_accepted() -> None:
    """The rule has to admit a real skill folder, or importing is impossible and the
    validation gets removed rather than fixed."""
    assert safe_archive_member("hosting-expiry/scripts/check_expiry.py")


def test_one_hostile_member_refuses_the_whole_archive() -> None:
    """Extracting the rest produces a folder that looks like a skill, passes review because
    the reviewer reads what is there, and is missing whatever was refused."""
    with pytest.raises(SkillError):
        safe_archive_members(["skill/SKILL.md", "../evil"])


def test_an_archive_without_a_skill_md_is_refused() -> None:
    """Without this, an arbitrary zip imports as a skill with no declarations at all."""
    with pytest.raises(SkillError, match="not a skill"):
        safe_archive_members(["skill/readme.md"])


def test_an_archive_with_two_skill_md_files_is_refused() -> None:
    """Which one describes the skill would be decided by the order of the archive, which is
    chosen by whoever built it."""
    with pytest.raises(SkillError, match=r"2 SKILL\.md"):
        safe_archive_members(["a/SKILL.md", "b/SKILL.md"])


# --------------------------------------------------- where a skill came from


def test_a_github_import_pinned_to_a_branch_is_refused() -> None:
    """A branch moves and a tag moves more quietly. An import pinned to either fetches
    whatever is there on the day it runs rather than what was reviewed."""
    with pytest.raises(ValueError, match="full commit sha"):
        SkillSource(kind=SourceKind.GITHUB, location="verz/skills", commit="main")


def test_a_url_import_without_a_content_digest_is_refused() -> None:
    """Without a digest there is nothing to compare a re-fetch against, so the approval
    covers a moment rather than a file."""
    with pytest.raises(ValueError, match="sha256"):
        SkillSource(kind=SourceKind.URL, location="https://example.com/s.zip")


def test_a_url_import_over_plain_http_is_refused() -> None:
    """A skill fetched over a channel somebody can rewrite is a skill somebody else
    wrote."""
    with pytest.raises(ValueError, match="not https"):
        SkillSource(
            kind=SourceKind.URL, location="http://example.com/s.zip", content_digest=BYTES_DIGEST
        )


def test_an_uploaded_file_name_gets_the_archive_member_rule() -> None:
    """The uploaded name is written to disk somewhere, so it is a member name like any
    other and the traversal rule applies to it."""
    with pytest.raises(SkillError):
        SkillSource(kind=SourceKind.UPLOAD, location="../evil.zip", content_digest=BYTES_DIGEST)


# ------------------------------------------------------- review state (M12.2.5)


def test_an_imported_skill_is_not_executable_until_it_is_reviewed() -> None:
    """The whole point of importing into a non-executable state. Without it, a file from
    GitHub runs the moment it lands."""
    assert not _imported().is_executable()


def test_an_approval_needs_a_named_reviewer() -> None:
    """ "Approved by the system" is how nothing gets reviewed and everybody believes
    something was."""
    with pytest.raises(SkillError, match="named person"):
        _imported().approved_by("  ", NOW)


def test_an_approved_skill_is_executable() -> None:
    """The state machine has to reach the end, or review is a wall rather than a gate."""
    assert _imported().approved_by("u_weiling", NOW).is_executable()


def test_a_rejected_skill_is_not_executable() -> None:
    """Without this a rejection is a note in a table and the skill still runs."""
    assert not _imported().rejected_by("u_weiling", NOW).is_executable()


def test_a_decided_skill_cannot_be_decided_again() -> None:
    """A second decision overwrites the first, and the record of who approved what is the
    entire product of a review."""
    approved = _imported().approved_by("u_weiling", NOW)
    with pytest.raises(SkillError, match="already approved"):
        approved.rejected_by("u_priya", NOW)


def test_editing_an_approved_skill_makes_it_unreviewed_again() -> None:
    """Delete this and "approve once, edit afterwards" is the whole bypass."""
    approved = _imported().approved_by("u_weiling", NOW)
    edited = approved.with_content(_skill(body="1. do something else entirely"))
    assert edited.state is SkillState.IMPORTED
    assert not edited.is_executable()


def test_a_rename_is_not_an_edit() -> None:
    """Letting an edit rename a skill would let a new one inherit the approval of the one
    it replaced."""
    with pytest.raises(SkillError, match="a rename is a new skill"):
        _imported().with_content(_skill(name="something-else"))


# -------------------------------------------- progressive disclosure (M12.2.8)


def test_the_body_of_an_unreviewed_skill_is_not_disclosed() -> None:
    """Withholding execution while disclosing the procedure is theatre: an agent that reads
    the steps carries them out with the tools it already holds."""
    with pytest.raises(SkillError, match="not disclosed"):
        body_of(_imported())


def test_the_body_of_an_approved_skill_is_disclosed_on_demand() -> None:
    """The other half of progressive disclosure. Without it the format is unusable and
    somebody puts the body in the card."""
    approved = _imported().approved_by("u_weiling", NOW)
    assert body_of(approved).startswith("1. read the client")


def test_a_card_carries_a_name_and_a_description_and_nothing_else() -> None:
    """The card is what goes into every prompt. If it could carry a body, progressive
    disclosure would be a habit rather than a shape."""
    card = card_for(_skill())
    assert set(SkillCard.model_fields) == {"name", "description", "version"}
    assert card.description.startswith("check hosting")


def test_only_approved_skills_are_offered_as_cards() -> None:
    """An unapproved skill listed and refused teaches the model that the procedure exists,
    and the model will say so."""
    approved = _imported().approved_by("u_weiling", NOW)
    pending = _imported(_skill(name="other-skill"))
    assert [c.name for c in offered_cards([approved, pending])] == ["hosting-expiry"]


# ------------------------------------------------------ version locking (M12.2.7)


def test_an_agent_is_pinned_to_the_version_that_was_approved() -> None:
    """Without a pin an agent silently follows an edit and runs a procedure it was never
    tested with."""
    approved = _imported().approved_by("u_weiling", NOW)
    pinned = pin_skill("a_helpdesk", approved)
    assert resolve_pin(pinned, approved) is approved


def test_a_pin_refuses_a_skill_that_has_moved() -> None:
    """This is the failure the pin exists for: the golden set that proved the agent works
    was run against different words."""
    approved = _imported().approved_by("u_weiling", NOW)
    pinned = pin_skill("a_helpdesk", approved)
    moved = _imported(_skill(body="2. do it differently")).approved_by("u_priya", NOW)
    with pytest.raises(SkillError, match="edited since it was pinned"):
        resolve_pin(pinned, moved)


def test_an_unreviewed_skill_cannot_be_pinned() -> None:
    """Pinning before review puts the configuration in front of the review, and the agent
    is configured to run something nobody has read."""
    with pytest.raises(SkillError, match="cannot be pinned"):
        pin_skill("a_helpdesk", _imported())


def test_a_pin_refuses_a_different_skill_with_the_same_digest_field() -> None:
    """A pin names one skill. Matching on the digest alone would let any skill that hashed
    the same satisfy it, and the name is the cheap half of that check."""
    approved = _imported().approved_by("u_weiling", NOW)
    wrong = SkillPin(agent_id="a", skill_name="other-skill", digest=approved.skill.digest())
    with pytest.raises(SkillError, match="pin names"):
        resolve_pin(wrong, approved)


# ------------------------------------------------------------ the diff (M12.2.6)


def test_a_diff_names_the_fields_that_changed() -> None:
    """A queue of a hundred pending items needs to say which touched the procedure and
    which bumped a version."""
    assert diff_skills(_skill(), _skill(body="different", version="1.0.0")) == ("body", "version")


def test_an_unchanged_skill_diffs_to_nothing() -> None:
    """Without this a re-import with no changes fills the review queue, and a queue nobody
    can empty is a queue nobody reads."""
    assert diff_skills(_skill(), _skill()) == ()


# ------------------------------------------------- reach, composed and never added


def test_a_skill_reaches_only_what_its_caller_already_holds() -> None:
    """The composition rule. If a skill could add reach, importing a file would be a way to
    grant a capability."""
    registry = ToolRegistry()
    registry.register(_definition(), a_typed_handler)
    registry.register(
        _definition("client.read_money", capability="read:client.contract_value"), a_typed_handler
    )
    skill = _skill(tools=("client.read_summary", "client.read_money"))
    caller = EntitlementSet(
        principal_id="p_weiling",
        grants=(Grant(capability=Capability(value="read:client.name"), scope=Scope()),),
    )
    ceiling = AgentCeiling(agent_id="a_helpdesk", allowed_tools=frozenset(registry.names()))
    assert skill_reach(skill, registry, caller, ceiling, now=NOW) == ("client.read_summary",)


def test_a_skill_naming_an_unregistered_tool_reaches_nothing_extra() -> None:
    """An unresolvable name treated as unconstrained is the same failure as a malformed
    capability treated as no requirement."""
    registry = ToolRegistry()
    registry.register(_definition(), a_typed_handler)
    skill = _skill(tools=("client.read_summary", "ghost.read_everything"))
    caller = EntitlementSet(
        principal_id="p_weiling",
        grants=(Grant(capability=Capability(value="read:client.name"), scope=Scope()),),
    )
    ceiling = AgentCeiling(agent_id="a_helpdesk", allowed_tools=frozenset(registry.names()))
    assert unknown_tools(skill, registry) == ("ghost.read_everything",)
    assert skill_reach(skill, registry, caller, ceiling, now=NOW) == ("client.read_summary",)


def test_required_capabilities_are_read_off_the_tools() -> None:
    """This is what a review screen shows. It has to come from the registry, or a skill
    would be describing its own reach and the description could be wrong."""
    registry = ToolRegistry()
    registry.register(_definition(), a_typed_handler)
    registry.register(
        _definition("client.read_money", capability="read:client.contract_value"), a_typed_handler
    )
    skill = _skill(tools=("client.read_summary", "client.read_money"))
    assert [c.value for c in required_capabilities(skill, registry)] == [
        "read:client.contract_value",
        "read:client.name",
    ]
