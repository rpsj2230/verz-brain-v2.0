"""Tools and skills: the two things an agent is built from, and only one of them grants.

Two modules, split along the line the architecture draws between them.

- `registry`: what a tool is allowed to be. The closed `source.verb_noun` naming grammar,
  the required capability as a `Capability` rather than a string, the scope a SERVICE
  identity-mode tool must carry, the typed-or-opaque result contract, the side effect that
  can only tighten a leash rung, and one name meaning one tool.
- `skills`: procedures that compose tools. The `SKILL.md` format, import pinning, archive
  member safety, the review state that gates both execution and disclosure, per-agent
  version locking, and progressive disclosure.

The dependency runs one way: skills import the registry, and the registry knows nothing
about skills beyond reserving one object name for the single tool that runs their scripts.
That direction is the reason a skill can never widen anything. A tool is the only grantable
noun in the platform, so reach is decided in `registry` and composed in `skills`, and there
is no path by which composing changes what was decided.

This package feeds `brain.gate.catalogue.project` and does not replace it. Projection is a
per-request question about a person and stays where it is; everything here is a question
about a tool or a skill, asked once, at registration or at import.

It writes no SQLAlchemy models and no migrations. Where a leaf names a table
(`tool_definition`), what is here is the type and the rules that govern it; the tables
belong to whoever owns `src/brain/tables`.
"""

from __future__ import annotations

from brain.tools.registry import (
    OBJECT_NAME_RE,
    RUN_SKILL_SCRIPT,
    SKILL_SCRIPT_OBJECT,
    TOOL_NAME_RE,
    RegisteredTool,
    ResultContract,
    ToolRegistrationError,
    ToolRegistry,
    assert_effect_matches_capability,
    assert_object_name,
    assert_object_not_reserved,
    assert_result_contract,
    assert_service_tool_is_scoped,
    assert_source_agrees,
    assert_tool_name,
    capability_for,
    default_rung,
    rung_ceiling,
)
from brain.tools.skills import (
    ARCHIVE_SEGMENT_RE,
    COMMIT_RE,
    DIGEST_RE,
    DIGEST_SCHEMA,
    FRONTMATTER_KEYS,
    GITHUB_REPO_RE,
    MAX_MEMBER_LENGTH,
    REACH_KEYS,
    SKILL_FILE,
    SKILL_NAME_RE,
    VERSION_RE,
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

__all__ = [
    "ARCHIVE_SEGMENT_RE",
    "COMMIT_RE",
    "DIGEST_RE",
    "DIGEST_SCHEMA",
    "FRONTMATTER_KEYS",
    "GITHUB_REPO_RE",
    "MAX_MEMBER_LENGTH",
    "OBJECT_NAME_RE",
    "REACH_KEYS",
    "RUN_SKILL_SCRIPT",
    "SKILL_FILE",
    "SKILL_NAME_RE",
    "SKILL_SCRIPT_OBJECT",
    "TOOL_NAME_RE",
    "VERSION_RE",
    "ImportedSkill",
    "RegisteredTool",
    "ResultContract",
    "Skill",
    "SkillCard",
    "SkillError",
    "SkillPin",
    "SkillSource",
    "SkillState",
    "SourceKind",
    "ToolRegistrationError",
    "ToolRegistry",
    "assert_effect_matches_capability",
    "assert_object_name",
    "assert_object_not_reserved",
    "assert_result_contract",
    "assert_service_tool_is_scoped",
    "assert_source_agrees",
    "assert_tool_name",
    "body_of",
    "capability_for",
    "card_for",
    "default_rung",
    "diff_skills",
    "execution_tool",
    "offered_cards",
    "parse_frontmatter",
    "pin_skill",
    "required_capabilities",
    "resolve_pin",
    "rung_ceiling",
    "safe_archive_member",
    "safe_archive_members",
    "skill_from_markdown",
    "skill_reach",
    "unknown_tools",
]
