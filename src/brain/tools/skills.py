"""Skills: procedures that compose tools, and that can never compose a capability.

A skill is a folder with a `SKILL.md` in it. It says what to do and in what order, and the
architecture is explicit about what it is not: of the three things an agent is built from,
"a tool is grantable, and it is the only one". A skill names tools it expects to use, and
its reach is whatever those tools would have reached for this caller anyway.

**What breaks without that rule.** A skill is imported from GitHub, a URL or an upload,
which makes it the one part of an agent that arrives from outside the company. If a skill
could declare a capability, importing a file would be a way to grant one, and the review
that is supposed to catch it would be reviewing prose. So `Skill` has no field that could
hold a capability, a grant, a scope or a leash rung, and the frontmatter parser refuses
those keys by name rather than ignoring them. The absence is the mechanism; the refusal is
so that somebody who tries is told why.

Three more rules run through this file.

**An imported skill is not executable until a named person has approved it, and approval is
bound to a digest of the content.** An approval that survived an edit would be an approval
of something nobody read, which is the argument `brain.gate.leash.SuspendedAction` makes
about storing its own action digest rather than deriving it on read.

**Not executable means not disclosed.** `body_of` refuses to hand over the instructions of a
skill that has not been approved. Withholding only the script would be theatre: the body is
a procedure, and an agent that reads it carries it out with the tools it already holds.

**Progressive disclosure is a type, not a habit.** `SkillCard` has nowhere to put a body, so
the cheap half of a skill cannot accidentally carry the expensive half into a prompt.

Scope: this is domain logic. Nothing here opens a socket, reads a file or extracts an
archive. Importing from GitHub or a URL needs a network and importing an upload needs a
filesystem; what is here is everything that has to be true about the thing that arrives,
including which archive member names may be written at all.

Task ids: M12.2.1, M12.2.4, M12.2.5, M12.2.6, M12.2.7, M12.2.8, M12.2.9
"""

from __future__ import annotations

import enum
import hashlib
import re
from collections.abc import Iterable
from datetime import datetime
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from brain.core.entitlement import Capability, EntitlementSet
from brain.gate.catalogue import AgentCeiling, project
from brain.tools.registry import RUN_SKILL_SCRIPT, TOOL_NAME_RE, ToolRegistry

# --------------------------------------------------------------------- grammars

#: A skill's name. Folded the same way `brain.core.department._normalise` folds a slug, so
#: `hosting-expiry` and `hosting_expiry` are the same name to a reader and cannot both be
#: imported.
SKILL_NAME_RE: Final = re.compile(r"^[a-z][a-z0-9_-]{0,79}$")

#: Semantic-looking, and informational only. Version locking is by digest, because a
#: version string is written by whoever edited the file and a digest is not.
VERSION_RE: Final = re.compile(r"^\d+\.\d+\.\d+$")

#: A sha256 hexdigest, as `brain.gate.leash.DIGEST` and `brain.audit.ledger.DIGEST` spell it.
DIGEST_RE: Final = re.compile(r"^[0-9a-f]{64}$")

#: A full commit sha. Deliberately not a short one: a short sha is a prefix, and a prefix
#: can become ambiguous in a repository that has grown since the import.
COMMIT_RE: Final = re.compile(r"^[0-9a-f]{40}$")

#: `owner/repo`, as GitHub spells it.
GITHUB_REPO_RE: Final = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,38}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"
)

#: One path segment inside an imported archive.
ARCHIVE_SEGMENT_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

#: The longest member name an archive may carry. A guard rather than a limit anybody meets;
#: a path long enough to matter is a path built to defeat something.
MAX_MEMBER_LENGTH: Final = 200

#: The file that makes a folder a skill.
SKILL_FILE: Final = "SKILL.md"

#: Domain separation for every digest here, with the same warning `brain.gate.leash`
#: attaches to its own: changing what a digest covers invalidates every approval ever
#: granted, so this line is a migration rather than an edit.
DIGEST_SCHEMA: Final = "brain.skill.v1"

#: What a `SKILL.md` may declare. Closed, because the alternative is that an unknown key is
#: ignored, and a skill whose `capabilities:` line was silently ignored looks exactly like a
#: skill whose `capabilities:` line was honoured.
FRONTMATTER_KEYS: Final[frozenset[str]] = frozenset(
    {"name", "description", "version", "tools", "scripts"}
)

#: Keys that are refused with an explanation rather than as merely unknown. Every one of
#: them is somebody expressing reach in a file that arrived from outside the company.
REACH_KEYS: Final[frozenset[str]] = frozenset(
    {"capabilities", "capability", "grant", "grants", "scope", "scopes", "leash", "rung"}
)


class SkillError(Exception):
    """A skill was declared, imported or used in a way that cannot be made safe.

    Outside the user-facing taxonomy in `brain.core.errors`, for the reason
    `brain.core.redaction.UntypedShapeError` gives: nobody asking a question sees this. It
    is a contract violation by an author, an importer or a caller, and it belongs in front
    of whichever of them is looking.
    """


# ------------------------------------------------- the frontmatter parser (M12.2.1)


def _parse_scalar_or_list(raw: str) -> str | tuple[str, ...]:
    """One frontmatter value: a scalar, or an inline `[a, b]` list. Nothing else.

    Rejected: PyYAML. It is a new dependency on the one code path that runs over a file
    fetched from GitHub, and the interesting half of YAML is exactly the half an attacker
    reaches for. Aliases expand, merge keys compose, tags construct, and a safe loader is
    safe only for as long as nobody changes the call. A frontmatter with two shapes has no
    such half.

    Rejected: block lists written as indented `- item` lines. They are what people are used
    to, and they bring indentation semantics with them, which is where the entire class of
    "the list silently became a string" bugs lives.
    """
    text = raw.strip()
    if not text.startswith("["):
        return text
    if not text.endswith("]"):
        msg = f"frontmatter list {raw!r} is not closed"
        raise SkillError(msg)
    inner = text[1:-1].strip()
    if not inner:
        return ()
    return tuple(item.strip() for item in inner.split(","))


def parse_frontmatter(text: str) -> tuple[dict[str, str | tuple[str, ...]], str]:
    """Split a `SKILL.md` into its declared keys and its body (M12.2.1).

    The opening fence must be the very first line. A file with anything before it, including
    a blank line or a byte order mark, is refused rather than searched: searching for a
    fence anywhere in a document means a fenced block halfway down a README becomes the
    frontmatter of a skill nobody wrote.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        msg = (
            "a SKILL.md must open with a --- fence on its first line; a fence found "
            "anywhere else would make a code block in the middle of a document into a "
            "declaration"
        )
        raise SkillError(msg)

    closing = next((i for i, line in enumerate(lines[1:], start=1) if line.strip() == "---"), None)
    if closing is None:
        msg = "the frontmatter fence in this SKILL.md is never closed"
        raise SkillError(msg)

    fields: dict[str, str | tuple[str, ...]] = {}
    for number, line in enumerate(lines[1:closing], start=2):
        if not line.strip():
            continue
        key, separator, raw = line.partition(":")
        key = key.strip()
        if not separator:
            msg = f"line {number} of this SKILL.md is not `key: value`: {line.strip()!r}"
            raise SkillError(msg)
        if key in REACH_KEYS:
            msg = (
                f"this SKILL.md declares {key!r}; a skill composes tools and declares no "
                "reach of its own, because a file imported from outside the company would "
                "otherwise be a way to grant one"
            )
            raise SkillError(msg)
        if key not in FRONTMATTER_KEYS:
            msg = (
                f"this SKILL.md declares {key!r}, which is not one of "
                f"{sorted(FRONTMATTER_KEYS)}; an unknown key is refused rather than ignored, "
                "because an ignored declaration reads exactly like an honoured one"
            )
            raise SkillError(msg)
        if key in fields:
            # YAML would take the last one. Two `tools:` lines would then mean whichever the
            # parser saw second, and a reviewer reading top to bottom would approve the first.
            msg = f"this SKILL.md declares {key!r} twice; which one applies would be a guess"
            raise SkillError(msg)
        fields[key] = _parse_scalar_or_list(raw)

    return fields, "\n".join(lines[closing + 1 :]).strip("\n")


# ------------------------------------------------------------------ where it came from


class SourceKind(enum.StrEnum):
    """How a skill arrived. Three, as the architecture lists them."""

    GITHUB = "github"
    URL = "url"
    UPLOAD = "upload"


class SkillSource(BaseModel):
    """Where a skill came from, in a form that can be fetched again and compared.

    Every kind carries something immutable, and that is the whole content of this model.
    An import that cannot be repeated is an import of whatever was at the address on the day
    it ran, so the review that approved it approved a moment rather than a file.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: SourceKind
    #: `owner/repo` for GitHub, an https URL, or a file name for an upload.
    location: str = Field(min_length=1, max_length=400)
    #: GitHub only. A full 40-character commit sha.
    commit: str = ""
    #: URL and upload. A sha256 over the bytes that arrived.
    content_digest: str = ""

    @model_validator(mode="after")
    def _pinned_to_something_that_cannot_move(self) -> Self:
        """Refuse any import that a later fetch could resolve differently (M12.2.2, M12.2.3).

        A branch moves, a tag moves more quietly, and a URL moves without telling anybody.
        The reviewer approved bytes, so the pin has to be over bytes or over the one
        identifier in git that is derived from them.
        """
        if self.kind is SourceKind.GITHUB:
            if not GITHUB_REPO_RE.match(self.location):
                msg = f"github source {self.location!r} is not owner/repo"
                raise ValueError(msg)
            if not COMMIT_RE.match(self.commit):
                msg = (
                    f"github source {self.location!r} is pinned to {self.commit!r}, which is "
                    "not a full commit sha; a branch or a tag moves, so the import would "
                    "fetch whatever is there on the day it runs rather than what was reviewed"
                )
                raise ValueError(msg)
            return self

        if self.commit:
            msg = f"a {self.kind.value} source carries no commit"
            raise ValueError(msg)
        if not DIGEST_RE.match(self.content_digest):
            msg = (
                f"a {self.kind.value} source must carry a sha256 of the bytes that arrived; "
                "without one there is nothing to compare a re-fetch against"
            )
            raise ValueError(msg)
        if self.kind is SourceKind.URL and not self.location.startswith("https://"):
            msg = (
                f"url source {self.location!r} is not https; a skill fetched over a channel "
                "somebody can rewrite is a skill somebody else wrote"
            )
            raise ValueError(msg)
        if self.kind is SourceKind.UPLOAD:
            # The uploaded file's own name is written to disk somewhere, so it is a member
            # name like any other and gets the same rule.
            safe_archive_member(self.location)
        return self


# ---------------------------------------------------- archive members (M12.2.4)


def safe_archive_member(name: str) -> str:
    """Refuse an archive member name that could be written outside the extraction root.

    The classic refusal is `../../etc/passwd`, and it is the least interesting one. The two
    that matter here are a backslash and a drive letter, because this platform is developed
    on Windows and deployed on Linux: `..\\..\\x` is one harmless file name on Linux and a
    traversal on Windows, so a check that ran only where it was written would pass in CI and
    fail in the one place it counts.

    What this cannot check is a member's mode. A symlink pointing at `/etc` has a perfectly
    ordinary name, and no rule about names will ever catch it; whoever opens the archive has
    to refuse a member that is not a regular file. Saying so here is better than implying
    this function is the whole defence, which is the same reason
    `brain.gate.leash.run_shadow` states what it cannot verify about a simulator.
    """
    if not name or not name.strip():
        msg = "an archive member with an empty name cannot be written anywhere on purpose"
        raise SkillError(msg)
    if len(name) > MAX_MEMBER_LENGTH:
        msg = f"archive member name is {len(name)} characters, over the {MAX_MEMBER_LENGTH} limit"
        raise SkillError(msg)
    if any(character < " " or character == "\x7f" for character in name):
        msg = f"archive member {name!r} carries a control character"
        raise SkillError(msg)
    if "\\" in name:
        msg = (
            f"archive member {name!r} contains a backslash; it is a path separator on the "
            "host this is developed on and an ordinary character on the host it runs on, so "
            "the same name means two different things"
        )
        raise SkillError(msg)
    if name.startswith("/"):
        msg = f"archive member {name!r} is an absolute path"
        raise SkillError(msg)
    if re.match(r"^[A-Za-z]:", name):
        msg = f"archive member {name!r} names a drive"
        raise SkillError(msg)
    if name.endswith("/"):
        msg = (
            f"archive member {name!r} is a directory entry; it carries no content, and "
            "creating directories from names in the archive is how an empty entry becomes a "
            "path the next member is written through"
        )
        raise SkillError(msg)
    for segment in name.split("/"):
        if segment in {"", ".", ".."}:
            msg = f"archive member {name!r} contains a {segment!r} segment"
            raise SkillError(msg)
        if not ARCHIVE_SEGMENT_RE.match(segment):
            msg = f"archive member {name!r} has a segment that is not a file name: {segment!r}"
            raise SkillError(msg)
    return name


def safe_archive_members(names: Iterable[str]) -> tuple[str, ...]:
    """Check every member, and refuse the archive rather than the member.

    Skipping a bad member and extracting the rest produces a folder that looks like a skill,
    passes review because the reviewer reads what is there, and is missing whatever the
    refused member was going to be. An archive with one hostile name is a hostile archive.

    It also insists on exactly one `SKILL.md`. None means it is not a skill; two means the
    loader picks one, and which one it picks is the order the archive happens to list them
    in.
    """
    checked = tuple(safe_archive_member(name) for name in names)
    manifests = [name for name in checked if name.rsplit("/", 1)[-1] == SKILL_FILE]
    if not manifests:
        msg = f"this archive has no {SKILL_FILE}, so it is not a skill"
        raise SkillError(msg)
    if len(manifests) > 1:
        msg = (
            f"this archive has {len(manifests)} {SKILL_FILE} files ({sorted(manifests)}); "
            "which one describes the skill would be decided by the order of the archive"
        )
        raise SkillError(msg)
    return checked


# ----------------------------------------------------------------------- the skill


class Skill(BaseModel):
    """A procedure, and the tools it expects to use.

    Note what is not here: no capability, no scope, no leash rung, no connector, no
    credential and no executor. A skill's reach is the union of its tools' requirements
    intersected with the caller, and the way to guarantee that is to leave it nothing else
    to say. `extra="forbid"` closes the same door from the other side, so a table row or a
    parsed document carrying one of those keys fails to load rather than loading with it
    ignored.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=400)
    version: str = "0.0.0"
    #: Tool names, sorted and deduplicated so that reordering the line does not change the
    #: digest and therefore does not void an approval.
    tools: tuple[str, ...] = ()
    #: Scripts inside the folder, run through one tool and no other. See `execution_tool`.
    scripts: tuple[str, ...] = ()
    #: The instructions. Withheld until the skill is approved; see `body_of`.
    body: str = ""

    @field_validator("name")
    @classmethod
    def _is_a_slug(cls, v: str) -> str:
        if not SKILL_NAME_RE.match(v):
            msg = f"skill name {v!r} is not a lowercase slug"
            raise ValueError(msg)
        return v

    @field_validator("version")
    @classmethod
    def _looks_like_a_version(cls, v: str) -> str:
        if not VERSION_RE.match(v):
            msg = f"skill version {v!r} is not major.minor.patch"
            raise ValueError(msg)
        return v

    @field_validator("tools")
    @classmethod
    def _tool_names_are_tool_names(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        """Every named tool must at least be spellable as one.

        Whether the tool exists is a different question, asked by `unknown_tools` against a
        registry. This one is about the file: a skill naming `read the ticket` names nothing
        that could ever be registered, and the reviewer should be told that by the parser
        rather than by an agent that quietly used no tools.
        """
        bad = sorted(name for name in v if not TOOL_NAME_RE.match(name))
        if bad:
            msg = f"skill names tools that are not source.verb_noun: {bad}"
            raise ValueError(msg)
        return tuple(sorted(set(v)))

    @field_validator("scripts")
    @classmethod
    def _scripts_stay_inside_the_folder(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for path in v:
            safe_archive_member(path)
        return tuple(sorted(set(v)))

    def digest(self) -> str:
        """A digest over everything a reviewer read.

        Length-prefixed before hashing, for the reason `brain.gate.leash._digest` gives:
        joining with a separator makes two different skills share a digest as soon as any
        part can contain the separator, and an approval satisfied by a different skill than
        the one shown is worse than no approval.
        """
        parts: list[str] = [
            DIGEST_SCHEMA,
            self.name,
            self.version,
            self.description,
            self.body,
            *self.tools,
            *self.scripts,
        ]
        joined = "".join(f"{len(part)}:{part}" for part in parts)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def skill_from_markdown(text: str) -> Skill:
    """Build a skill from a `SKILL.md`, or refuse it (M12.2.1)."""
    fields, body = parse_frontmatter(text)
    missing = sorted({"name", "description"} - set(fields))
    if missing:
        msg = f"this SKILL.md declares no {missing}; a skill with no description has no card"
        raise SkillError(msg)

    def scalar(key: str, default: str = "") -> str:
        value = fields.get(key, default)
        if not isinstance(value, str):
            msg = f"{key!r} in this SKILL.md is a list; it takes a single value"
            raise SkillError(msg)
        return value

    def listed(key: str) -> tuple[str, ...]:
        value = fields.get(key, ())
        if isinstance(value, str):
            # A single item without brackets is the commonest way to write one, and reading
            # it as a one-element list is the only place this parser guesses. It guesses in
            # the direction that cannot widen anything: the item still has to be a real tool
            # name, and a wrong guess produces an unknown tool rather than an extra one.
            return (value,) if value else ()
        return value

    try:
        return Skill(
            name=scalar("name"),
            description=scalar("description"),
            version=scalar("version", "0.0.0"),
            tools=listed("tools"),
            scripts=listed("scripts"),
            body=body,
        )
    except ValueError as exc:
        raise SkillError(str(exc)) from exc


# -------------------------------------------------------- review and state (M12.2.5)


class SkillState(enum.StrEnum):
    """Where an imported skill has got to. Closed; there is no fourth state."""

    IMPORTED = "imported"
    APPROVED = "approved"
    REJECTED = "rejected"


class ImportedSkill(BaseModel):
    """A skill, where it came from, and whether a named person has approved it.

    `approved_digest` is stored rather than derived. A stored skill edited in place no
    longer matches the approval that was granted for it, and `is_executable` goes false
    without anybody having to remember to re-open the review. Deriving it on read would make
    an altered skill agree with itself forever.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    skill: Skill
    source: SkillSource
    state: SkillState = SkillState.IMPORTED
    #: Who decided. Named, because "approved by the system" is how nothing gets reviewed.
    reviewer: str = ""
    reviewed_at: datetime | None = None
    approved_digest: str = ""

    @field_validator("reviewed_at")
    @classmethod
    def _tz_aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            msg = "reviewed_at must be timezone-aware; a naive timestamp is a silent bug"
            raise ValueError(msg)
        return v

    def is_executable(self) -> bool:
        """Approved, by somebody named, and unchanged since (M12.2.5)."""
        return (
            self.state is SkillState.APPROVED
            and bool(self.reviewer)
            and self.approved_digest == self.skill.digest()
        )

    def _decided(self, state: SkillState, reviewer: str, at: datetime) -> Self:
        if self.state is not SkillState.IMPORTED:
            msg = (
                f"skill {self.skill.name!r} is already {self.state.value}; a second decision "
                "would overwrite the first, and the record of who approved what is the "
                "entire product of a review"
            )
            raise SkillError(msg)
        if not reviewer.strip():
            msg = "a skill is approved by a named person, never by an empty string"
            raise SkillError(msg)
        if at.tzinfo is None:
            msg = "a review time must be timezone-aware"
            raise SkillError(msg)
        return self.model_copy(
            update={
                "state": state,
                "reviewer": reviewer,
                "reviewed_at": at,
                # A rejection records no digest. Only an approval is a statement about
                # particular bytes; a rejection is a statement about the skill.
                "approved_digest": self.skill.digest() if state is SkillState.APPROVED else "",
            }
        )

    def approved_by(self, reviewer: str, at: datetime) -> Self:
        return self._decided(SkillState.APPROVED, reviewer, at)

    def rejected_by(self, reviewer: str, at: datetime) -> Self:
        return self._decided(SkillState.REJECTED, reviewer, at)

    def with_content(self, skill: Skill) -> Self:
        """A new version of the same skill, unreviewed again.

        The name may not change. A different name is a different skill, and letting an edit
        rename one would let an approved skill inherit the approval of the skill it replaced.
        """
        if skill.name != self.skill.name:
            msg = (
                f"cannot replace {self.skill.name!r} with {skill.name!r}; a rename is a new "
                "skill, and it does not inherit a review"
            )
            raise SkillError(msg)
        return self.model_copy(
            update={
                "skill": skill,
                "state": SkillState.IMPORTED,
                "reviewer": "",
                "reviewed_at": None,
                "approved_digest": "",
            }
        )


def diff_skills(old: Skill, new: Skill) -> tuple[str, ...]:
    """Which fields differ, for the review queue (M12.2.6).

    Names only. The review screen renders both versions side by side, because a reviewer
    approving a procedure has to read it; what a queue lists is which of a hundred pending
    items touched the body and which only bumped a version.
    """
    return tuple(
        name for name in sorted(Skill.model_fields) if getattr(old, name) != getattr(new, name)
    )


# ------------------------------------------------- progressive disclosure (M12.2.8)


class SkillCard(BaseModel):
    """The cheap half of a skill: what it is called and what it is for.

    There is no body field and no script field, and that is the mechanism rather than an
    oversight. Progressive disclosure exists to keep a prompt small, and a rule saying "send
    the card, not the body" is a rule that holds until somebody needs one body and passes
    the whole skill. A type with nowhere to put a body cannot carry one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str
    version: str


def card_for(skill: Skill) -> SkillCard:
    return SkillCard(name=skill.name, description=skill.description, version=skill.version)


def offered_cards(skills: Iterable[ImportedSkill]) -> tuple[SkillCard, ...]:
    """The cards an agent may be shown: approved skills only, in name order.

    An unapproved skill is absent rather than listed and refused, which is the rule
    `brain.gate.catalogue` applies to tools and for the same reason. A card for a skill the
    agent cannot load teaches the model that the procedure exists, and the model will say so.
    """
    return tuple(
        card_for(item.skill)
        for item in sorted(skills, key=lambda item: item.skill.name)
        if item.is_executable()
    )


def body_of(imported: ImportedSkill) -> str:
    """The instructions, on demand, and only for an approved skill (M12.2.5, M12.2.8).

    Withholding execution while disclosing the body would be theatre. The body is the
    procedure: an agent that has read "check domain expiry, then open a ticket" can do
    exactly that with the tools it already holds, and the review would have gated the one
    part of a skill that is not the point of it.
    """
    if not imported.is_executable():
        msg = (
            f"skill {imported.skill.name!r} is {imported.state.value} and its body is not "
            "disclosed; instructions are the procedure, so an unreviewed skill that can be "
            "read has been executed by an agent that follows them with its own tools"
        )
        raise SkillError(msg)
    return imported.skill.body


# --------------------------------------------------- version locking (M12.2.7)


class SkillPin(BaseModel):
    """One agent, one skill, one exact version of it.

    By digest and not by version string. A version is a number an author types, and the
    failure this prevents is a skill edited without one, which is also the commonest way a
    reviewed procedure changes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_id: str = Field(min_length=1, max_length=128)
    skill_name: str
    digest: str

    @field_validator("digest")
    @classmethod
    def _is_a_digest(cls, v: str) -> str:
        if not DIGEST_RE.match(v):
            msg = f"skill pin digest {v!r} is not a sha256"
            raise ValueError(msg)
        return v


def pin_skill(agent_id: str, imported: ImportedSkill) -> SkillPin:
    """Pin an agent to the version of a skill that was approved.

    Only an executable skill can be pinned. Pinning an unreviewed one would put the review
    after the configuration, and the agent would be configured to run something nobody had
    read yet.
    """
    if not imported.is_executable():
        msg = f"skill {imported.skill.name!r} is {imported.state.value} and cannot be pinned"
        raise SkillError(msg)
    return SkillPin(
        agent_id=agent_id, skill_name=imported.skill.name, digest=imported.skill.digest()
    )


def resolve_pin(pin: SkillPin, imported: ImportedSkill) -> ImportedSkill:
    """The pinned skill, or a refusal. Never the newest one (M12.2.7).

    An agent that silently followed an edit would run a procedure it was never tested with,
    and the golden set that proved it works would have been run against different words.
    Upgrading is a decision somebody makes, which is a new pin.
    """
    if pin.skill_name != imported.skill.name:
        msg = f"pin names {pin.skill_name!r} and this is {imported.skill.name!r}"
        raise SkillError(msg)
    if pin.digest != imported.skill.digest():
        msg = (
            f"agent {pin.agent_id!r} is pinned to a version of {pin.skill_name!r} that this "
            "one is not; the skill has been edited since it was pinned, and following the "
            "edit would run a procedure the agent was never tested with"
        )
        raise SkillError(msg)
    if not imported.is_executable():
        msg = f"skill {imported.skill.name!r} is {imported.state.value} and is not executable"
        raise SkillError(msg)
    return imported


# ------------------------------------------------------- one execution path (M12.2.9)


def execution_tool() -> str:
    """The single tool a skill's scripts run through.

    It takes no arguments, and that is the guarantee rather than an accident of the body,
    in the same way and for the same reason as `brain.core.redaction.render_lock`. A
    function that cannot see the skill cannot return a different executor for one, so no
    skill can name its own runner however its folder is written; `Skill` has no field for
    one either, and `brain.tools.registry.assert_object_not_reserved` refuses a second tool
    that claims the skill-script object. Three closed doors, because the sandbox, the leash
    and the output redaction are properties of this path and not of the script.
    """
    return RUN_SKILL_SCRIPT


# ------------------------------------------------------------- reach, never granted


def unknown_tools(skill: Skill, registry: ToolRegistry) -> tuple[str, ...]:
    """Tools this skill names that are not registered.

    A skill naming a tool nobody registered is misconfigured, and the honest thing is to say
    which one. What must never happen is the opposite reading, where an unresolvable name is
    treated as unconstrained: that is the same failure as treating a malformed capability as
    no requirement, and `skill_reach` therefore drops an unknown tool rather than passing it
    through.
    """
    return tuple(sorted(name for name in skill.tools if not registry.has(name)))


def required_capabilities(skill: Skill, registry: ToolRegistry) -> tuple[Capability, ...]:
    """The union of what this skill's tools require, sorted and deduplicated.

    This is what a review screen shows: "this skill needs read:ticket.status and
    write:ticket.status". It is a description of the tools, computed from the registry, and
    the skill contributes nothing to it but a list of names.
    """
    held: dict[str, Capability] = {}
    for name in skill.tools:
        if registry.has(name):
            capability = registry.get(name).capability
            held[capability.value] = capability
    return tuple(held[value] for value in sorted(held))


def skill_reach(
    skill: Skill,
    registry: ToolRegistry,
    entitlement: EntitlementSet,
    ceiling: AgentCeiling,
    *,
    now: datetime | None = None,
    universal: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    """The tools this skill can actually use here: its list, intersected with the catalogue.

    Computed by asking `brain.gate.catalogue.project` and intersecting, rather than by
    re-checking entitlements against the registry. That is deliberate and it is the whole
    property: a skill cannot exceed the catalogue because it is not consulted about the
    catalogue. Restating the entitlement check here would be a second opinion about who may
    reach what, and the day the two disagree, the permissive one wins silently.

    An unknown tool and a tool outside the caller's reach both simply do not appear. The
    skill is not refused for naming them, because a skill is a procedure and a procedure
    that mentions a step this person cannot take is an ordinary thing; what it must not do
    is take it.
    """
    admitted = frozenset(
        project(registry, entitlement, ceiling, now=now, universal=universal).names
    )
    return tuple(sorted(set(skill.tools) & admitted))
