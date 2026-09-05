"""The automation canvas: a place for deterministic plumbing, and never for judgement.

Activepieces is offered because clients ask for it and because refusing costs more than it
saves: somebody who wants "when a form is submitted, create a ticket" will otherwise build
it in Zapier, outside every control in this system. Running it here is the safer of two
real options, not an endorsement.

What makes it safe is a boundary, and the boundary has four parts.

**Flows are deterministic.** A step is an HTTP request, a tool call, a transformation, a
branch or a delay. There is no step type that asks a model what to do next, and the absence
is the mechanism - `StepKind` is a closed set with nowhere to express one, so a flow that
wanted agent control flow could not be described, let alone run. This is the same
construction `brain.gate.injection` uses for refusals and `brain.ops.pii` uses for blocks.
The reason is that agent control flow in this system is governed: a leash, an autonomy
tier, an approval, an entitlement intersection, an audit row. A canvas that could branch on
a model's answer would be a second, ungoverned agent runtime with a drag-and-drop editor,
and its runs would not appear anywhere an auditor looks.

**A flow reaches data only through the tool API, with the caller's own entitlements.**
`flow_reach` is `E(caller) ∩ flow_ceiling`, the same intersection every agent run uses. A
flow that declares a wider capability than its caller holds gets nothing extra; it is the
intersection, not the maximum. This is the one property worth proving rather than
asserting, and it is proved in the test suite against the real `EntitlementSet`, not
against a stub of it.

**The container holds no credential to anything it should not reach.** Not "should not use"
- should not reach. The flows in it are written by whoever the client puts in front of the
canvas, which is by definition somebody outside this repository's review, so the container
is treated as hostile: no database URL, no vault token, no provider key, and an egress
allowlist rather than an open network.

**It is enabled by configuration and never by a fork.** One image, one compose entry, a
boolean. A client-specific build is a build that stops receiving security updates the day
it is cut, and nobody notices because it works.

The allowlist matches hostnames exactly. Suffix matching is the obvious implementation and
it is the bug: `"notapi.lark.com".endswith("api.lark.com")` is true, so a suffix check on an
allowlist admits any host somebody can register whose name ends in the right characters. A
genuine subdomain is written out as its own entry.

Not claimed here: the custom piece itself (M32.6.1.3) is TypeScript in the Activepieces
plugin format and does not exist, and the container (M32.6.1.1) has a memory budget in
`brain.ops.wiring` and no compose service. What is here is the boundary the piece will have
to obey and the checks that say whether it does.

Task ids: M32.6.1.2, M32.6.1.4, M32.6.2.1, M32.6.2.2
"""

from __future__ import annotations

import enum
import re
from collections.abc import Iterable, Mapping
from typing import Final

from brain.core.entitlement import EntitlementSet

#: Stated where a reader meets it, and asserted by the test suite so that deleting the rule
#: deletes a test.
DETERMINISTIC_ONLY: Final = (
    "An automation flow is plumbing: a trigger, some steps, a result. Anything that "
    "requires judgement is an agent run, which is leashed, entitled, approved and audited. "
    "A canvas that could branch on a model's answer would be a second agent runtime with "
    "no leash and no audit trail."
)


class AutomationError(Exception):
    """Raised when a flow, an egress target or a container environment breaks the boundary."""


class StepKind(enum.StrEnum):
    """Every kind of step a flow may contain.

    There is no member for a model call, an agent, a prompt or a decision, and there is
    nowhere to put one without changing this enum in a diff somebody reviews. That is the
    whole of the boundary in `DETERMINISTIC_ONLY`: it is not a rule flows are asked to
    follow, it is a vocabulary in which the forbidden thing cannot be said.
    """

    #: An outbound request, subject to the egress allowlist.
    HTTP_REQUEST = "http_request"
    #: A call into our tool API, which runs the gate.
    TOOL_CALL = "tool_call"
    #: Pure data shaping: field mapping, formatting, arithmetic.
    TRANSFORM = "transform"
    #: A branch on a value already in the flow.
    BRANCH = "branch"
    DELAY = "delay"


def assert_deterministic(steps: Iterable[Mapping[str, object]]) -> None:
    """Refuse a flow descriptor containing a step this system will not run.

    Flow descriptors arrive as JSON from a canvas outside this repository, so they are
    validated as untrusted input rather than typed. A step with no `kind` is refused rather
    than skipped: an unrecognised step that runs anyway is the definition of the boundary
    not existing, and a canvas is free to add step types on its own release schedule.
    """
    known = {k.value for k in StepKind}
    for index, step in enumerate(steps):
        kind = step.get("kind")
        if not isinstance(kind, str) or kind not in known:
            msg = (
                f"step {index} has kind {kind!r}, which is not one of {sorted(known)}. "
                f"{DETERMINISTIC_ONLY}"
            )
            raise AutomationError(msg)


# ----------------------------------------------------------------- egress
#: Hosts a flow may reach. Exact matches only. Every entry is something a client's plumbing
#: genuinely needs; a host that is merely convenient does not go on it, because the list is
#: read as permission by whoever adds the next one.
EGRESS_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {
        "open.larksuite.com",
        "open.feishu.cn",
        "api.xero.com",
        "graph.microsoft.com",
        "hooks.slack.com",
    }
)


def egress_refusals(hosts: Iterable[str]) -> tuple[str, ...]:
    """Every host in this flow that the sandbox may not reach.

    Exact comparison, lower-cased. Not `endswith`, not a regex over the suffix: the suffix
    check admits `notapi.xero.com`, which anybody can register, and the failure looks like
    the allowlist working.
    """
    refused: list[str] = []
    for host in hosts:
        normalised = host.strip().lower().rstrip(".")
        if normalised not in EGRESS_ALLOWLIST:
            refused.append(
                f"{host!r} is not on the egress allowlist; add it there, in a diff, or "
                "route the call through a tool where the gate can see it"
            )
    return tuple(refused)


# ----------------------------------------------------------------- credentials
#: Environment variable names that carry something the sandbox must not hold. Matched on
#: word parts rather than as substrings, so `MONKEY` does not match `KEY`.
_CREDENTIAL_NAME_RE: Final = re.compile(
    r"(^|_)(PASSWORD|PASSWD|SECRET|TOKEN|KEY|DSN|CREDENTIALS?)($|_)", re.IGNORECASE
)

#: Anything that is a connection string, whichever scheme it announces itself with.
_CONNECTION_SCHEMES: Final[tuple[str, ...]] = (
    "postgres://",
    "postgresql://",
    "postgresql+psycopg://",
    "redis://",
    "rediss://",
    "amqp://",
    "mongodb://",
)


def credential_leaks(env: Mapping[str, str]) -> tuple[str, ...]:
    """Everything in this container's environment that it must not have been given.

    Two independent checks, because either alone misses the real cases. A name check alone
    misses `BRAIN_UPSTREAM=postgresql://brain:...@db:5432/brain`, which is a full credential
    under an innocent name. A value check alone misses `VAULT_TOKEN=hvs.CAESIH...`, which is
    the most dangerous variable of all and looks like an opaque string.

    Reports the variable name and never its value. A checker that quoted what it found would
    put the credential into whatever log the check writes to, which is the same failure as
    the one it is looking for.
    """
    findings: list[str] = []
    for name, value in env.items():
        if _CREDENTIAL_NAME_RE.search(name):
            findings.append(f"{name}: a credential the sandbox has no business holding")
            continue
        lowered = value.strip().lower()
        if lowered.startswith(_CONNECTION_SCHEMES):
            findings.append(f"{name}: holds a connection string")
    return tuple(findings)


# ----------------------------------------------------------------- enablement
def enabled_for(config: Mapping[str, object]) -> bool:
    """Whether this client runs the canvas. One image, one boolean, no branch of the code.

    A non-boolean is refused rather than coerced. `"false"` is truthy in Python, in
    JavaScript, and in the YAML that a compose file's environment section produces, and a
    feature that is meant to be off and is on is exactly the shape of failure this whole
    module is a boundary against. Absent means off, because a client who has not been asked
    has not agreed.
    """
    if "activepieces" not in config:
        return False
    value = config["activepieces"]
    if not isinstance(value, bool):
        msg = (
            f"activepieces enablement is {value!r} ({type(value).__name__}); it must be a "
            "boolean. A string here is truthy in every language this passes through."
        )
        raise AutomationError(msg)
    return value


# ----------------------------------------------------------------- reach
def flow_reach(caller: EntitlementSet, flow_ceiling: EntitlementSet) -> EntitlementSet:
    """What a flow may actually touch: the caller's entitlements narrowed by the flow's own.

    The same intersection an agent run uses, deliberately calling the same method rather
    than reimplementing it. A second implementation of the platform's central invariant is
    a second place for it to be subtly wrong, and the subtly wrong version is the one
    running inside the sandbox authored by somebody outside this repository.

    Note which way round it is. The flow's declaration is a ceiling, never a grant: a flow
    declaring `read:client.*` on behalf of a caller who holds `read:client.name` in one
    department comes out holding `read:client.name` in that department.
    """
    return caller.intersect(flow_ceiling)
