"""The concrete driver: the one place in this system that actually speaks to a provider.

`driver.py` drew the seam and stopped there on purpose. It holds the protocol, the
per-lane call policy and the per-pool router, and `CONCRETE_ADAPTER_NOT_BUILT` says
plainly that nothing in it imports an SDK or performs I/O. This module is the other side
of that seam, and it is the only file in the codebase that may.

Five decisions live here. Each one prevents a failure that is silent, which is to say a
failure whose first symptom is a bill, a leaked key, or an answer nobody knows is wrong.

**The SDK sits behind one injected callable.** `Transport` is a single function from a
`DriverRequest` to a `Completion`, and every provider call in the system goes through it.
A test supplies a fake and nothing opens a socket; the real import lives in exactly one
function, `litellm_transport`. The alternative, importing the SDK at module scope, makes
it a hard dependency of importing `brain.models` at all, so the test suite needs a package
installed and a network reachable before it can collect. It also puts the provider's types
one import away from the policy layer, which is the thing the seam exists to prevent.

**The SDK is a driver and never a proxy, and never its own router.** We construct the call,
we set the timeout, we count the attempt, we record the trace. Handing the SDK a router
with a fallback list and letting it choose would move the fallback policy inside a
dependency: our tests could not see it, our traces could not explain it, and the console's
matrix would quietly become advisory. `litellm_model_list` therefore asks for zero retries
and declares no fallbacks, and `SdkDriver.complete` makes exactly one transport call. One
call in, one outcome out; retrying is the executor's job because the breaker has to see
every attempt.

**Anything we do not recognise is a failure, not a trigger.** `trigger_for` closes the
fallback set by its signature and `DriverFailure` closes it again by its fields. The
translation from a provider's exception to a `DriverFailure` is the third place that
closure has to hold, and it is the weakest, because an SDK upgrade can invent an exception
shape overnight. An unrecognised error therefore lands in the branch that stops the chain.
Mapping the unknown onto `connection_failed=True` reads like resilience and is a retry
loop that grows every time a provider names a new error.

**A content-policy refusal is not a trigger, whichever way it arrives.** It arrives two
ways in practice: as an ordinary 200 whose finish reason says the model declined, and as a
raised error on providers that reject at the edge. The first is a successful call and is
returned as one, so the chain stops because it has an answer. The second is turned into a
failure carrying no status and no transport flag at all, so `trigger_for` returns None
whatever the provider attached to it. See `A_REFUSAL_IS_NOT_A_TRIGGER`; the rule itself is
`routing.CONTENT_POLICY_REFUSAL_IS_NOT_A_TRIGGER`, decided on 5 September.

**Nothing from the request reaches a log, a trace or an exception message.** An adapter
that includes its request in an error is the most common way an API key ends up in a log
aggregator, and it does not take a mistake: SDK exceptions routinely stringify the request
they failed on, headers included, so `raise ... from exc` is enough to publish one. So the
detail on a failure is *composed* from values we already hold (the deployment id, the
exception's class name, a status, an error name from a closed list) rather than filtered
from the provider's text, the same allowlist argument `audit.ledger.redact_details` makes
at greater length: a denylist of things that look like secrets is unbounded, an allowlist
of things that are known safe is closed. We also never hold the credential in the first
place. The SDK reads it from the environment, so it is not in a request object, cannot be
in a trace, and cannot be in an exception we build.

Task ids: M5.1.1, M5.1.4
"""

from __future__ import annotations

import importlib
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

import structlog

from brain.models.driver import (
    DriverFailure,
    DriverRegistry,
    DriverRequest,
    DriverResponse,
    PoolRouter,
    ProviderUnavailable,
    TokenUsage,
    routers_for,
)
from brain.models.routing import RoutingChain, Tier

log = structlog.get_logger()

# ------------------------------------------------------------------- written-down reasons
#: Why the import is in one function rather than at the top of this file.
SDK_IMPORT_LIVES_IN_ONE_PLACE = (
    "The provider SDK is imported inside `litellm_transport` and nowhere else. A module-"
    "scope import makes the SDK a hard dependency of importing `brain.models`, so the "
    "tests need the package installed and the policy layer sits one import away from a "
    "provider's types. One function means one thing to fake in a test and one thing to "
    "change when the SDK is swapped."
)

#: Why an unfamiliar error stops the chain instead of moving it on.
AN_UNRECOGNISED_FAILURE_IS_NOT_A_TRIGGER = (
    "An error shape we do not recognise is a failure, never a retry. Mapping the unknown "
    "onto a connection error reads as resilience and is an unbounded retry loop: every "
    "time a provider invents an error name, every request that hits it silently costs the "
    "whole chain. The chain moves on only for facts named in `FallbackTrigger`."
)

#: Why a refusal never reaches the next rung, in either of the two shapes it arrives in.
A_REFUSAL_IS_NOT_A_TRIGGER = (
    "A content-policy refusal is a property of the request, not of the provider's health. "
    "Arriving as a 200 it is an answer and is returned as one. Arriving as an error it "
    "becomes a failure with no status and no transport flag, so the trigger is None "
    "whatever the provider attached. Retrying it either reproduces it at full cost or "
    "shops for a provider willing to say yes, which is the quality trigger in disguise."
)

#: Why the failure detail is built rather than filtered.
NOTHING_FROM_THE_REQUEST_IN_AN_EXCEPTION = (
    "A failure's detail is composed from values we already hold: the deployment id, the "
    "exception's class name, a status, an error name from a closed list. It never includes "
    "the provider's message, the prompt, or the extra mapping. SDK exceptions stringify the "
    "request they failed on, headers included, so passing one through, or chaining it "
    "with `raise ... from exc`, publishes a credential to whatever reads the log."
)

#: Why one transport is built per pool rather than one filtered at call time.
ONE_TRANSPORT_PER_POOL = (
    "Tag filtering is a proxy-mode feature; driven as an SDK a Router selects from the "
    "whole deployment list it was constructed with. So each pool gets its own Router "
    "built from its own tier's deployment ids, and a cross-pool selection is not "
    "expressible rather than merely discouraged. See "
    "`driver.TAG_FILTERING_IS_INOPERATIVE_IN_SDK_MODE`."
)


# ------------------------------------------------------------------------- the wire shape
@dataclass(frozen=True)
class Completion:
    """What a transport returns on success, in our vocabulary rather than a provider's.

    `served_model` is what the provider says answered, which is frequently a version-
    stamped form of what we asked for. It is carried because the bill names it.
    """

    text: str
    finish_reason: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    served_model: str = ""


@runtime_checkable
class Transport(Protocol):
    """The one callable that may touch a network, injected everywhere it is used.

    Synchronous, matching `ModelDriver`, and for the same reason: the domain layer is
    synchronous from `classify_tier` to the executor, and an async transport would make
    every caller of it async all the way up.
    """

    def __call__(self, request: DriverRequest) -> Completion: ...


# ------------------------------------------------------- what a transport may raise at us
class TransportError(Exception):
    """Base for the closed set of failures a transport is expected to raise.

    Closed on purpose. `failure_from` recognises these and nothing else, and everything it
    does not recognise becomes a failure with no trigger. A transport author who needs a
    new shape adds it here, next to the mapping that decides what it means, rather than
    discovering that an unfamiliar error was already being retried.

    Carries no message by default. Whatever the provider said stays at the boundary that
    caught it; see `NOTHING_FROM_THE_REQUEST_IN_AN_EXCEPTION`.
    """


class TransportTimeoutError(TransportError):
    """The call did not answer inside the timeout we gave it."""


class TransportConnectionError(TransportError):
    """The call never reached a provider: DNS, TLS, refused, reset."""


class TransportStatusError(TransportError):
    """The provider answered with an HTTP status that is not a success.

    `code` is the provider's own short error name where there is one. Validated rather
    than trusted, because it is the single field on this type that originates outside our
    process and it ends up in a log line.
    """

    def __init__(self, status: int, *, code: str = "") -> None:
        super().__init__(f"provider returned {status}")
        self.status = status
        self.code = safe_code(code)


class ContextWindowExceededError(TransportError):
    """The request did not fit. The one failure permitted to change tier, and only upward."""


class ContentPolicyRefusedError(TransportError):
    """The provider declined on content grounds rather than failing.

    Takes a status because providers attach one and a transport author will have it to
    hand. `failure_from` deliberately drops it: a refusal that arrived as a 503 must not
    become a `PROVIDER_ERROR` trigger and be reproduced, at full cost, on the next rung.
    """

    def __init__(self, *, status: int | None = None) -> None:
        super().__init__("provider declined on content policy")
        self.status = status


class ProviderSdkMissingError(RuntimeError):
    """The SDK this adapter drives is not installed.

    A configuration fault at wiring time, not a request outcome, so it is a `RuntimeError`
    like `config.assert_valid` rather than a member of the error taxonomy. Reporting it as
    `Degraded` would put "nobody added the dependency" in the same bucket as "the provider
    is down", and those two go to different people on different days.
    """


# ------------------------------------------------------------------ composing a failure
#: Provider error names we are willing to repeat verbatim in a log line.
#:
#: A closed set of values rather than a pattern, and the difference is the whole point. An
#: API key is `[a-z0-9-]+` too, and so is a bearer token, so a rule that admitted anything
#: name-shaped would pass a credential straight into the log store while reading in review
#: as though it sanitised something. This is the same allowlist argument
#: `audit.ledger.redact_details` makes at greater length: a denylist of things that look
#: like secrets is unbounded and fails silently, an allowlist of things already known to
#: be safe is closed and fails by dropping a log field nobody will miss.
KNOWN_PROVIDER_CODES: frozenset[str] = frozenset(
    {
        "api_error",
        "authentication_error",
        "context_window_exceeded",
        "invalid_request_error",
        "not_found_error",
        "overloaded_error",
        "permission_error",
        "rate_limit_error",
        "request_too_large",
        "service_unavailable",
        "timeout",
    }
)

#: Finish reasons that mean the model declined rather than the call failing. Matched
#: case-insensitively against the provider's own vocabulary, which differs per provider and
#: is the reason this is a set rather than one constant.
REFUSAL_FINISH_REASONS: frozenset[str] = frozenset(
    {
        "content_filter",
        "content_policy",
        "refusal",
        "safety",
        "prohibited_content",
        "recitation",
        "blocklist",
    }
)


def safe_code(code: str) -> str:
    """The provider's error name if it is one we already know, else nothing at all."""
    candidate = code.strip().casefold()
    return candidate if candidate in KNOWN_PROVIDER_CODES else ""


def is_refusal(finish_reason: str) -> bool:
    """Whether a *successful* call came back as a declined one."""
    return finish_reason.strip().casefold() in REFUSAL_FINISH_REASONS


def failure_from(exc: BaseException, *, deployment_id: str) -> DriverFailure:
    """Translate whatever was raised into transport facts, defaulting to no trigger.

    The ordering is the rule. The refusal branch runs before anything looks at a status,
    so a refusal cannot borrow a trigger from the status the provider attached to it. The
    final branch is the important one: an exception type nobody here has heard of produces
    a failure with every flag unset, which `trigger_for` reads as None, which stops the
    chain. See `AN_UNRECOGNISED_FAILURE_IS_NOT_A_TRIGGER`.
    """
    kind = type(exc).__name__
    if isinstance(exc, ContentPolicyRefusedError):
        # Deliberately dropping exc.status. A refusal that arrived as a 429 or a 503 would
        # otherwise be classified as rate limiting or a provider error and reproduced, at
        # full cost, on a rung that will decline in exactly the same way.
        return DriverFailure(
            deployment_id=deployment_id,
            detail=f"{kind}: declined on content policy, so the chain stops here",
        )
    if isinstance(exc, TransportTimeoutError):
        return DriverFailure(deployment_id=deployment_id, timed_out=True, detail=kind)
    if isinstance(exc, TransportConnectionError):
        return DriverFailure(deployment_id=deployment_id, connection_failed=True, detail=kind)
    if isinstance(exc, ContextWindowExceededError):
        return DriverFailure(deployment_id=deployment_id, context_exceeded=True, detail=kind)
    if isinstance(exc, TransportStatusError):
        suffix = f" code={exc.code}" if exc.code else ""
        return DriverFailure(
            deployment_id=deployment_id,
            status=exc.status,
            detail=f"{kind}{suffix}",
        )
    # Unrecognised. The class name and nothing else: it locates the code path without
    # carrying a message that may hold the request, and it does not set a flag, so the
    # chain stops rather than paying for a retry against a fault we cannot describe.
    return DriverFailure(
        deployment_id=deployment_id,
        detail=f"unrecognised transport error {kind}; not a fallback trigger",
    )


# ------------------------------------------------------------------------- the adapter
@dataclass(frozen=True)
class SdkDriver:
    """One provider, one transport, exactly one call per request.

    Satisfies `ModelDriver`. There is no retry here and no second endpoint: attempts are
    counted by `CallPolicy` and executed by the executor, because a retry inside the
    adapter is a failure the breaker never sees, and a breaker that is shown one failure
    where three happened opens on the fourth incident instead of the second.
    """

    provider: str
    transport: Transport

    def complete(self, request: DriverRequest) -> DriverResponse:
        """Make one call. Raises `ProviderUnavailable` on any transport failure."""
        started = time.perf_counter()
        try:
            completion = self.transport(request)
        except Exception as exc:
            # Broad on purpose, and it is the narrow alternative that is dangerous: an SDK
            # exception that escapes this method reaches the executor as an unhandled
            # error rather than as a chain outcome, so the request becomes a 500 instead
            # of a Degraded and the breaker records nothing at all.
            failure = failure_from(exc, deployment_id=request.deployment_id)
            self._trace(request, failure=failure, elapsed=time.perf_counter() - started)
            # `from None`, not `from exc`. Chaining keeps the SDK's exception, whose text
            # routinely contains the request it failed on, in `__cause__`, where every
            # traceback formatter and log aggregator will render it.
            raise ProviderUnavailable(failure) from None

        response = DriverResponse(
            # The id we asked for, never one the transport chose. An adapter that answered
            # from a different endpoint would be invisible: the attempt row would name the
            # rung we planned and the invoice would name something else.
            deployment_id=request.deployment_id,
            # The model the provider says served it, which is usually a version-stamped
            # form of what we asked for. Considered and rejected: failing the call on a
            # mismatch. Snapshot ids differ from catalogue ids on every provider we have
            # looked at, so that rule would refuse ordinary healthy traffic.
            model=completion.served_model or request.model,
            text=completion.text,
            usage=TokenUsage(
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
                cached_input_tokens=completion.cached_input_tokens,
            ),
            finish_reason=completion.finish_reason,
        )
        self._trace(request, response=response, elapsed=time.perf_counter() - started)
        return response

    def _trace(
        self,
        request: DriverRequest,
        *,
        response: DriverResponse | None = None,
        failure: DriverFailure | None = None,
        elapsed: float,
    ) -> None:
        """One line per call, built from named fields rather than from the request.

        Note what is absent: the messages, the extra mapping, the reply text. The trace
        exists to answer "which rung, how long, what came back", and every one of those is
        answerable without a single character the caller supplied. A trace that logged the
        prompt would put the company's own data, and anything a caller smuggled into
        `extra`, into the log store, which has a different retention and a different
        audience from the answer it describes.
        """
        log.info(
            "model call",
            provider=self.provider,
            deployment_id=request.deployment_id,
            model=request.model,
            timeout_seconds=request.timeout_seconds,
            elapsed_ms=round(elapsed * 1000, 1),
            ok=failure is None,
            status=None if failure is None else failure.status,
            trigger=None if failure is None else failure.trigger,
            detail=None if failure is None else failure.detail,
            refused=None if response is None else is_refusal(response.finish_reason),
            finish_reason=None if response is None else response.finish_reason,
            input_tokens=None if response is None else response.usage.input_tokens,
            output_tokens=None if response is None else response.usage.output_tokens,
            cached_input_tokens=None if response is None else response.usage.cached_input_tokens,
        )


# --------------------------------------------------------------- one router per pool
#: How a pool's Router instance is built. Takes the tier and that tier's deployment ids,
#: and nothing else, so the factory has no way to reach a deployment from another pool.
RouterFactory = Callable[[Tier, tuple[str, ...]], Transport]


def transports_per_pool(
    chain: RoutingChain,
    make_router: RouterFactory,
) -> Mapping[Tier, Transport]:
    """One Router instance per non-empty pool. Never one Router holding every tier.

    This is M5.1.4 made concrete. The factory is called once per pool with only that
    pool's deployment ids, so a Router that could serve a HEAVY request from the SMALL
    pool is never constructed in the first place. Filtering one shared Router at call time
    is the alternative, and it is the thing that does not work in SDK mode.
    """
    return MappingProxyType(
        {
            tier: make_router(tier, router.deployment_ids)
            for tier, router in routers_for(chain).items()
        }
    )


@dataclass(frozen=True)
class PoolDispatcher:
    """Our own dispatcher: pool first, then rung, then the adapter for its provider.

    Every model call in the system goes through here, which is what makes "the SDK is a
    driver, never a proxy" a property of the code rather than an intention. The pool is
    resolved before anything else, and `PoolRouter.rung_for` refuses an id it does not
    serve, so a request cannot be answered from a tier it was not routed to.
    """

    registry: DriverRegistry

    def dispatch(self, tier: Tier, request: DriverRequest) -> DriverResponse:
        """Call the rung this request names, in this tier, or refuse loudly."""
        rung = self.registry.router_for(tier).rung_for(request.deployment_id)
        if request.model != rung.model:
            # A bug, not an outage, so it raises rather than becoming a chain failure.
            # The rung's model is half the price-book key: serving a different one meters
            # the request against the wrong price and the discrepancy surfaces as an
            # unexplained invoice a month later.
            msg = (
                f"request names model {request.model!r} on rung {rung.deployment.id!r}, "
                f"which serves {rung.model!r}"
            )
            raise ValueError(msg)
        return self.registry.driver_for(rung).complete(request)


# ------------------------------------------------------------------ the one import site
def litellm_model_list(router: PoolRouter) -> list[dict[str, Any]]:
    """One pool's Router constructor argument, and nothing from another pool.

    Two things are deliberately absent. There is no credential: the SDK reads the key from
    the environment, so it never enters a structure of ours and therefore cannot reach a
    trace or an exception. And there is no `fallbacks` entry, with `num_retries` pinned to
    zero, because retrying and falling back are our policy: an SDK that retried inside one
    call would hide attempts from the breaker and spend the answer lane's wall-clock
    budget on rungs nobody planned.
    """
    return [
        {
            # Keyed by deployment id rather than by model name. Two deployments of one
            # model in different regions is the ordinary case, and the residency guarantee
            # is the difference between them.
            "model_name": rung.deployment.id,
            "litellm_params": {
                "model": rung.model,
                "timeout": rung.timeout_seconds,
                "num_retries": 0,
            },
        }
        for rung in router.rungs
    ]


#: The SDK's exception class names, mapped to ours. Matched by name because importing the
#: types would put the SDK back at module scope, which is the one thing this module is
#: arranged to avoid. A name that is not in here is unrecognised, and unrecognised stops
#: the chain, so an SDK upgrade that renames an exception costs a fallback rather than
#: silently starting an unbounded retry.
LITELLM_ERROR_NAMES: Mapping[str, str] = MappingProxyType(
    {
        "Timeout": "timeout",
        "APITimeoutError": "timeout",
        "APIConnectionError": "connection",
        "APIError": "connection",
        "ServiceUnavailableError": "status",
        "InternalServerError": "status",
        "RateLimitError": "status",
        "AuthenticationError": "status",
        "BadRequestError": "status",
        "NotFoundError": "status",
        "PermissionDeniedError": "status",
        "ContextWindowExceededError": "context",
        "ContentPolicyViolationError": "refusal",
    }
)


def translate_sdk_error(exc: BaseException) -> TransportError:
    """One SDK exception, in our vocabulary. Unknown names stay unknown.

    Reads only `status_code` and the class name off the exception, and never its message:
    an SDK error's text contains the request that produced it, and this is the boundary
    where that text stops.
    """
    name = type(exc).__name__
    status = getattr(exc, "status_code", None)
    kind = LITELLM_ERROR_NAMES.get(name)
    if kind == "timeout":
        return TransportTimeoutError()
    if kind == "connection":
        return TransportConnectionError()
    if kind == "context":
        return ContextWindowExceededError()
    if kind == "refusal":
        return ContentPolicyRefusedError(status=status if isinstance(status, int) else None)
    if kind == "status" and isinstance(status, int):
        return TransportStatusError(status, code=getattr(exc, "code", "") or "")
    # Deliberately a bare TransportError rather than a connection failure: the base class
    # carries no flag, so `failure_from` produces no trigger and the chain stops.
    return TransportError()


def completion_from_sdk(raw: Any) -> Completion:
    """The SDK's response object, in our vocabulary.

    Defensive on every field. A response missing a usage block, or carrying a null content
    because the model emitted only tool calls, is ordinary rather than exceptional, and an
    adapter that raised on it would report a working provider as an outage.
    """
    choices = getattr(raw, "choices", None) or []
    first = choices[0] if choices else None
    message = getattr(first, "message", None)
    usage = getattr(raw, "usage", None)
    cached = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0)
    return Completion(
        text=getattr(message, "content", None) or "",
        finish_reason=getattr(first, "finish_reason", None) or "",
        input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        cached_input_tokens=int(cached or 0),
        served_model=str(getattr(raw, "model", "") or ""),
    )


def _load_litellm() -> Any:
    """Import the SDK, or say exactly what is missing. The only import site in the system.

    It is not a declared dependency today, so this raises rather than pretending. An
    adapter that silently degraded to a stub here would report a working chain that
    answers nothing, which is worse than not starting.
    """
    try:
        return importlib.import_module("litellm")
    except ImportError as exc:
        msg = (
            "litellm is not installed, so no provider can be called. Add it to "
            "pyproject.toml dependencies, or inject a Transport of your own: this "
            "adapter is complete without it and the SDK is the only missing part."
        )
        raise ProviderSdkMissingError(msg) from exc


def litellm_transport(router: PoolRouter) -> Transport:
    """One pool's Router, driven as a library, wrapped as a `Transport`.

    The SDK import, the SDK's types and the SDK's exceptions all stop at this function.
    Everything above it deals in `DriverRequest`, `Completion` and `TransportError`, which
    is what makes the whole chain testable with no network and swappable with one file.
    """
    sdk = _load_litellm()
    instance = sdk.Router(model_list=litellm_model_list(router), num_retries=0)

    def send(request: DriverRequest) -> Completion:
        try:
            raw = instance.completion(
                # The deployment id, so the Router picks the endpoint we routed to rather
                # than choosing among every deployment that serves this model.
                model=request.deployment_id,
                messages=[{"role": m.role.value, "content": m.content} for m in request.messages],
                timeout=request.timeout_seconds,
                max_tokens=request.max_output_tokens,
                num_retries=0,
                **dict(request.extra),
            )
        except Exception as exc:
            raise translate_sdk_error(exc) from None
        return completion_from_sdk(raw)

    return send
