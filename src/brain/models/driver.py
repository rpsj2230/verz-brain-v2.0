"""The one seam between our routing policy and whatever SDK actually talks to a provider.

`routing.py` decides *which* deployment answers. Nothing in it knows how to call one, and
that separation is deliberate: the moment a provider SDK's types appear in the policy
layer, swapping the SDK becomes a change to the policy layer, and the policy layer is the
part with the residency guarantee in it.

Three decisions live here.

**A provider SDK is a driver, never a proxy.** Running LiteLLM's proxy server would put a
second process, with its own configuration file, its own credential store, its own
version, and its own outage, between the gate and every model call. It would also become
the place model policy is really configured, so the console's matrix would quietly become
advisory. Worse, a proxy terminates the request outside our process, which means the
residency skip in `RoutingChain.select` is enforced on one side of a hop whose other side
is configured somewhere else. The SDK is imported as a library, in-process, behind
`ModelDriver`, and the routing decision has already been made by the time it is reached.

**One router per pool, because tag filtering does not work in SDK mode.** LiteLLM's Router
supports tag-based deployment filtering when it runs as the proxy; driven as an SDK it
selects from the whole deployment list it was constructed with. A single Router holding
every tier's deployments would therefore be free to serve a HEAVY request from the SMALL
pool, which is the one direction `permits_tier_escalation` exists to forbid, and it would
do it silently. So the pool is chosen by our code first and a separate Router instance is
constructed per pool. `PoolRouter` cannot express a cross-pool selection: it holds one
tier's rungs and refuses any deployment id it does not serve.

**A failure may only be described in transport facts.** `DriverFailure` has fields for a
status code, a timeout, a dead connection and a context overflow, and deliberately no
field for the reply, its length, or anyone's opinion of it. That is the same closure
`routing.trigger_for` enforces by its signature, held one layer lower so an adapter author
cannot widen it: the only route from a failed call to a fallback decision runs through
`DriverFailure.trigger`, which calls `trigger_for` and nothing else.

Nothing here imports a provider SDK, opens a socket, or reads a credential. The concrete
adapter is not built (see `CONCRETE_ADAPTER_NOT_BUILT`); this module is the shape it will
have to fit, so that building it is one file and swapping it is the same file.

Task ids: M5.1.1, M5.1.3, M5.1.4
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from brain.core.errors import Degraded
from brain.core.lane import Lane
from brain.models.routing import (
    ANSWER_LANE_WALL_CLOCK_BUDGET_SECONDS,
    FallbackTrigger,
    RoutingChain,
    RoutingRung,
    Tier,
    trigger_for,
)

# ------------------------------------------------------------------- written-down reasons
#: Why the SDK is imported as a library and the proxy server is not run. Written down for
#: the same purpose as `routing.QUALITY_FALLBACK_REJECTED`: a rule with no argument
#: attached is a rule the next person deletes, and "just run their proxy, it is one
#: container" is a genuinely attractive suggestion until the consequences are listed.
LITELLM_IS_A_DRIVER_NOT_A_PROXY = (
    "The provider SDK is used in-process as a driver. Running its proxy server would add a "
    "second process with its own config, credentials, version and outage between the gate "
    "and every model call; model policy would really live in that config rather than in "
    "the console's matrix; and the request would terminate outside the process that "
    "enforces residency, so the skip would be enforced on one side of a hop configured on "
    "the other."
)

#: Why the pool is chosen before the Router, rather than by filtering one Router.
TAG_FILTERING_IS_INOPERATIVE_IN_SDK_MODE = (
    "LiteLLM's tag-based deployment filtering is a proxy-mode feature. Driven as an SDK the "
    "Router selects from the whole deployment list it was built with, so one Router holding "
    "every tier could serve a HEAVY request from the SMALL pool and would do it silently. "
    "One Router per pool makes that unrepresentable rather than merely discouraged."
)

#: Stated plainly so nobody reads this module as finished. What exists is the seam: the
#: protocol our code depends on, the per-lane call policy, and the per-pool router. What
#: does not exist is a class that actually speaks to Anthropic. Building it is one new
#: file implementing `ModelDriver`; nothing else in the codebase should need to change,
#: and if it does, this seam was drawn in the wrong place.
CONCRETE_ADAPTER_NOT_BUILT = (
    "No concrete provider adapter exists yet. This module defines the protocol, the "
    "per-lane call policy and the per-pool router only. Nothing here imports a provider "
    "SDK or performs I/O."
)


# --------------------------------------------------------------------------- wire shapes
class Role(enum.StrEnum):
    """Who said a turn. Three roles, because a fourth would be a provider's vocabulary
    leaking through the seam and every other adapter would then have to fake it."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True)
class DriverMessage:
    role: Role
    content: str


@dataclass(frozen=True)
class TokenUsage:
    """What the call cost, in the provider's own count rather than our estimate.

    `cached_input_tokens` is separate because prompt caching is worth roughly five times
    what the routing lever is worth at this workload, and a usage record that folds cached
    reads into ordinary input tokens cannot show whether the cache is working.
    """

    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0


@dataclass(frozen=True)
class DriverRequest:
    """One call, fully determined before the adapter sees it.

    The deployment is already chosen. An adapter that could pick a different one would be
    a second router sitting underneath the first, and the trace would describe a chain that
    was not the chain that ran.
    """

    deployment_id: str
    model: str
    messages: tuple[DriverMessage, ...]
    timeout_seconds: float
    max_output_tokens: int | None = None
    #: Provider-specific knobs, passed through untouched. Typed as strings rather than
    #: `Any` so a caller cannot smuggle a callback or a client object through here and
    #: quietly make the request non-serialisable, which is what stops it being replayable
    #: from a trace.
    extra: Mapping[str, str] = MappingProxyType({})

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            msg = f"request to {self.deployment_id!r} has a non-positive timeout"
            raise ValueError(msg)
        if not self.messages:
            msg = f"request to {self.deployment_id!r} carries no messages"
            raise ValueError(msg)


@dataclass(frozen=True)
class DriverResponse:
    """A successful call. Names the deployment that actually served it.

    `deployment_id` is echoed rather than assumed, because an adapter that silently
    retries against a different endpoint would otherwise be invisible: the attempt row
    would name the rung we asked for, and the bill would name something else.
    """

    deployment_id: str
    model: str
    text: str
    usage: TokenUsage
    finish_reason: str


@dataclass(frozen=True)
class DriverFailure:
    """A failed call, in transport facts only.

    Look at what cannot be put here: there is no field for the reply, its length, its
    score, or a judgement about it. `routing.trigger_for` closes the trigger set by its
    signature; this type closes it one layer lower, at the place an adapter author is
    actually writing code, so "the answer looked wrong so I set a trigger" has nowhere to
    be expressed.
    """

    deployment_id: str
    #: HTTP status, where there was one. None for a call that never got a response.
    status: int | None = None
    timed_out: bool = False
    connection_failed: bool = False
    context_exceeded: bool = False
    #: Free text for the log and the trace. Never parsed, and never consulted by
    #: `trigger`: a decision that reads an error string is a decision that changes when a
    #: provider rewords a message.
    detail: str = ""

    @property
    def trigger(self) -> FallbackTrigger | None:
        """Whether the chain may move on, or None when it must stop here.

        Delegates rather than deciding. One implementation of the fallback rule, in the
        policy module, is the only way the rule stays the same in the executor and in the
        tests that assert it.
        """
        return trigger_for(
            status=self.status,
            timed_out=self.timed_out,
            connection_failed=self.connection_failed,
            context_exceeded=self.context_exceeded,
        )


class ProviderUnavailable(Degraded):
    """The provider did not answer.

    Degraded, not Failed. The taxonomy's promise for Degraded is that we say a source was
    unreachable and never substitute something, which is exactly the honest outcome when
    every rung in a chain has failed. Raising Failed here would put a provider outage in
    the same bucket as a bug in our own code, and the two go to different people.
    """

    public_message = "I could not reach the model needed to answer that."

    def __init__(self, failure: DriverFailure, *, public_message: str | None = None) -> None:
        self.failure = failure
        detail = (
            f"{failure.deployment_id}: status={failure.status} timed_out={failure.timed_out} "
            f"connection_failed={failure.connection_failed} "
            f"context_exceeded={failure.context_exceeded} {failure.detail}".strip()
        )
        super().__init__(detail, public_message=public_message)


# ------------------------------------------------------------------------- the protocol
@runtime_checkable
class ModelDriver(Protocol):
    """Everything our code is allowed to know about calling a model.

    Synchronous on purpose. The domain layer is synchronous and pure all the way from
    `classify_tier` to the executor; only the FastAPI edge is async. An async protocol here
    would make every caller of it async, which means the routing executor, which means the
    functions that call it, all the way up. The cost of the alternative is one threadpool
    hop per provider call, and at roughly 0.1 requests per second that is not measurable.
    Revisit if the task lane ever fans out enough for thread count to matter.

    Failure is raised, not returned. A `DriverResponse | DriverFailure` union reads well
    and then somebody forgets to check the tag on one branch and a failure is composed into
    an answer. `ProviderUnavailable` carries the `DriverFailure` so nothing is lost.
    """

    #: The provider this adapter speaks for, matching `Deployment.provider`. An attribute
    #: rather than a method because it is a constant of the adapter, and `DriverRegistry`
    #: keys on it.
    provider: str

    def complete(self, request: DriverRequest) -> DriverResponse:
        """Make one call. Raises `ProviderUnavailable` on any transport failure."""
        ...


# ------------------------------------------------------- per-provider, per-lane overrides
@dataclass(frozen=True)
class CallPolicy:
    """The timeout, attempt count and concurrency one call actually runs under."""

    timeout_seconds: float
    attempts: int
    max_concurrency: int

    @property
    def worst_case_seconds(self) -> float:
        return self.attempts * self.timeout_seconds


@dataclass(frozen=True)
class LaneOverride:
    """A per-lane adjustment to a rung's defaults. None means "leave the rung's value".

    Two fields, not the whole of `CallPolicy`. `max_concurrency` is deliberately absent:
    it is a property of the deployment's capacity, not of who is waiting, and letting the
    task lane raise it would let overnight work eat the answer lane's headroom on the same
    endpoint.
    """

    timeout_seconds: float | None = None
    attempts: int | None = None


@dataclass(frozen=True)
class ProviderClient:
    """One provider's client configuration, with its per-lane overrides.

    This is the M5.1.3 object. It holds no credential and no session; the adapter owns
    those. What it holds is the part that is policy rather than plumbing, so it can be
    edited in the console and asserted in a test without a provider being reachable.
    """

    provider: str
    lanes: Mapping[Lane, LaneOverride] = MappingProxyType({})

    def policy_for(self, rung: RoutingRung, lane: Lane) -> CallPolicy:
        """The rung's defaults with this lane's overrides applied.

        Refuses the fast lane outright. Tier NONE means no model saw the question, and
        every guarantee downstream of the fast lane (empty tool catalogue, reads restricted
        to projected tables) was built on that. A driver call on the fast lane is a bug, and
        returning a plausible policy for it would let the bug run.
        """
        if lane is Lane.FAST:
            msg = (
                "the fast lane takes no model, so there is no call policy for it; a driver "
                "call on the fast lane is a bug, not a configuration gap"
            )
            raise ValueError(msg)
        if rung.deployment.provider != self.provider:
            msg = (
                f"client for provider {self.provider!r} was asked for a policy on rung "
                f"{rung.deployment.id!r}, which is served by {rung.deployment.provider!r}"
            )
            raise ValueError(msg)
        override = self.lanes.get(lane, LaneOverride())
        timeout = (
            rung.timeout_seconds if override.timeout_seconds is None else (override.timeout_seconds)
        )
        attempts = rung.attempts if override.attempts is None else override.attempts
        if timeout <= 0:
            msg = f"lane {lane} override for {self.provider!r} sets a non-positive timeout"
            raise ValueError(msg)
        if attempts < 1:
            # Same reason `RoutingRung` refuses it: the way to remove a rung is to remove
            # it. A zero-attempt override is a rung that silently never runs while reading
            # in the console as configured.
            msg = f"lane {lane} override for {self.provider!r} sets {attempts} attempts"
            raise ValueError(msg)
        return CallPolicy(
            timeout_seconds=timeout,
            attempts=attempts,
            max_concurrency=rung.max_concurrency,
        )


def answer_lane_worst_case_seconds(
    chain: RoutingChain,
    tier: Tier,
    clients: Mapping[str, ProviderClient],
) -> float:
    """The longest an answer-lane request can take on this tier, overrides included.

    `RoutingChain.worst_case_seconds` computes this from the rungs alone. Once a per-lane
    override can lengthen a timeout, that number stops being the truth, and the compounding
    (rungs times attempts times timeout) is invisible in any single row of the console
    editor. A provider with no client configured falls back to its rung's own values.
    """
    total = 0.0
    for rung in chain.rungs_for(tier):
        client = clients.get(rung.deployment.provider)
        policy = (
            CallPolicy(
                timeout_seconds=rung.timeout_seconds,
                attempts=rung.attempts,
                max_concurrency=rung.max_concurrency,
            )
            if client is None
            else client.policy_for(rung, Lane.ANSWER)
        )
        total += policy.worst_case_seconds
    return total


def check_answer_lane_budget(
    chain: RoutingChain,
    tier: Tier,
    clients: Mapping[str, ProviderClient],
    *,
    budget_seconds: float = ANSWER_LANE_WALL_CLOCK_BUDGET_SECONDS,
) -> None:
    """Raise if the overrides have pushed the answer lane past its wall-clock budget.

    Called when configuration changes, not per request. A person is waiting on this lane,
    and the failure mode being prevented is an operator raising one rung's timeout to help
    a slow provider and thereby taking the whole chain to a minute, which nobody notices
    until somebody complains that the bot is slow.
    """
    worst = answer_lane_worst_case_seconds(chain, tier, clients)
    if worst > budget_seconds:
        msg = (
            f"answer-lane overrides put tier {tier} at {worst:.1f}s worst case, over the "
            f"{budget_seconds:.1f}s budget; shorten a timeout or drop a rung"
        )
        raise ValueError(msg)


# --------------------------------------------------------------------- one router per pool
@dataclass(frozen=True)
class PoolRouter:
    """The deployments one Router instance is constructed with: exactly one tier's rungs.

    The M5.1.4 object. Its whole job is to make a cross-pool selection unrepresentable
    rather than merely discouraged, which is why there is no method here taking a tag, a
    label, or a predicate. See `TAG_FILTERING_IS_INOPERATIVE_IN_SDK_MODE`.
    """

    tier: Tier
    rungs: tuple[RoutingRung, ...]

    def __post_init__(self) -> None:
        for rung in self.rungs:
            if rung.tier is not self.tier:
                msg = (
                    f"pool router for {self.tier} was given rung {rung.deployment.id!r} "
                    f"from {rung.tier}; a router that spans pools is the failure this type "
                    f"exists to prevent"
                )
                raise ValueError(msg)
        positions = [r.position for r in self.rungs]
        if positions != sorted(positions):
            msg = f"pool router for {self.tier} was given rungs out of position order"
            raise ValueError(msg)

    @property
    def deployment_ids(self) -> tuple[str, ...]:
        return tuple(r.deployment.id for r in self.rungs)

    def serves(self, deployment_id: str) -> bool:
        return deployment_id in self.deployment_ids

    def rung_for(self, deployment_id: str) -> RoutingRung:
        """The rung, or a loud refusal. Never a rung from another pool.

        Refusing rather than returning None is the point. A None here would be checked at
        the first call site and silently coalesced at the second, and the second one is
        where a HEAVY request gets served by whatever the SMALL pool had lying around.
        """
        for rung in self.rungs:
            if rung.deployment.id == deployment_id:
                return rung
        msg = (
            f"pool router for {self.tier} does not serve {deployment_id!r}; it serves "
            f"{self.deployment_ids}"
        )
        raise ValueError(msg)


def routers_for(chain: RoutingChain) -> Mapping[Tier, PoolRouter]:
    """One router per non-empty pool, and none for a pool with no rungs.

    A tier with no rungs gets no router rather than an empty one. An empty Router is an
    object that accepts calls and fails on all of them, which surfaces as a provider
    outage; the honest surface for "this tier is not configured" is a missing key, which
    `RoutingChain.select` already reports as an empty chain with no skips.
    """
    return MappingProxyType(
        {
            tier: PoolRouter(tier=tier, rungs=chain.rungs_for(tier))
            for tier in Tier
            if chain.rungs_for(tier)
        }
    )


@dataclass(frozen=True)
class DriverRegistry:
    """Which adapter speaks for which provider, and which router serves which pool.

    Keyed by provider rather than by deployment: an adapter is per SDK, and one SDK serves
    every deployment of that provider. Keying by deployment would mean a new region needed
    a new adapter registration, which is how a region gets added to the matrix and then
    fails at the exact moment the chain reaches it.
    """

    drivers: Mapping[str, ModelDriver] = MappingProxyType({})
    routers: Mapping[Tier, PoolRouter] = MappingProxyType({})

    def driver_for(self, rung: RoutingRung) -> ModelDriver:
        """The adapter for this rung's provider, or a Degraded refusal.

        Degraded rather than a bare KeyError because "we hold no client for this provider"
        is the same class of event as that provider being down: the request cannot be
        served here and the honest answer is to say so and let the chain move on.
        """
        driver = self.drivers.get(rung.deployment.provider)
        if driver is None:
            msg = (
                f"no driver registered for provider {rung.deployment.provider!r}, so rung "
                f"{rung.deployment.id!r} cannot be called"
            )
            raise ProviderUnavailable(
                DriverFailure(deployment_id=rung.deployment.id, connection_failed=True, detail=msg)
            )
        return driver

    def router_for(self, tier: Tier) -> PoolRouter:
        router = self.routers.get(tier)
        if router is None:
            msg = f"no router is configured for tier {tier}"
            raise ValueError(msg)
        return router
