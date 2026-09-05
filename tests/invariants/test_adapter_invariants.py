"""Rules the concrete driver must never break.

Every one of these fails silently if it is deleted. Nothing errors, no answer looks wrong,
and the system quietly stops being the thing it was designed as.

1. **Nothing opens a socket.** The transport is injected, so a test drives the whole
   dispatcher with the socket module booby-trapped and nothing reaches a network. An
   adapter that resolved a hostname or opened a pool at import time would make the test
   suite depend on the network being up to decide whether our routing logic is correct.
2. **A provider failure lands on the closed trigger set, or on nothing.** The unknown must
   stop the chain. An unrecognised error mapped onto a retry is an unbounded loop that
   grows every time a provider invents a name for something.
3. **A content-policy refusal never reaches the next model.** In either shape it arrives
   in, and whatever status the provider attached to it.
4. **No part of the request reaches a log, a trace or an exception.** This is the one that
   puts a key in a log aggregator, and it does not take a mistake to do it: SDK exceptions
   stringify the request they failed on, so `raise ... from exc` is enough.
5. **One call out per call in.** A retry inside the adapter is an attempt the breaker
   never sees.

Task ids: M5.1.1, M5.1.4
"""

from __future__ import annotations

import ast
import inspect
import re
import socket
from typing import Any

import pytest
from structlog.testing import capture_logs

from brain.models import adapter as adapter_module
from brain.models.adapter import (
    A_REFUSAL_IS_NOT_A_TRIGGER,
    AN_UNRECOGNISED_FAILURE_IS_NOT_A_TRIGGER,
    KNOWN_PROVIDER_CODES,
    NOTHING_FROM_THE_REQUEST_IN_AN_EXCEPTION,
    ONE_TRANSPORT_PER_POOL,
    SDK_IMPORT_LIVES_IN_ONE_PLACE,
    Completion,
    ContentPolicyRefusedError,
    PoolDispatcher,
    SdkDriver,
    TransportError,
    TransportStatusError,
    failure_from,
    transports_per_pool,
)
from brain.models.driver import (
    DriverMessage,
    DriverRegistry,
    DriverRequest,
    ProviderUnavailable,
    Role,
    routers_for,
)
from brain.models.routing import (
    FALLBACK_TRIGGER_VALUES,
    FallbackTrigger,
    Tier,
    seed_chain,
)

pytestmark = pytest.mark.invariant

PRIMARY = "anthropic-sonnet-global"
MODEL = "claude-sonnet-5"

#: Stands in for an API key, a bearer token and a prompt in one string, so a single
#: assertion covers all three leak paths.
SECRET = "sk-ant-api03-DO-NOT-LOG-THIS"


def a_request(*, extra: dict[str, str] | None = None, text: str = "hello") -> DriverRequest:
    return DriverRequest(
        deployment_id=PRIMARY,
        model=MODEL,
        messages=(DriverMessage(role=Role.USER, content=text),),
        timeout_seconds=12.0,
        extra=extra or {},
    )


class Fake:
    def __init__(self, answer: Completion | BaseException) -> None:
        self.answer = answer
        self.calls: list[DriverRequest] = []

    def __call__(self, request: DriverRequest) -> Completion:
        self.calls.append(request)
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def a_completion(**overrides: Any) -> Completion:
    fields: dict[str, Any] = {"text": "ok", "finish_reason": "stop"}
    fields.update(overrides)
    return Completion(**fields)


def driver_over(answer: Completion | BaseException) -> tuple[SdkDriver, Fake]:
    transport = Fake(answer)
    return SdkDriver(provider="anthropic", transport=transport), transport


# ------------------------------------------------------------------ 1. nothing dials out
def test_a_whole_dispatch_opens_no_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """The transport is injected precisely so this holds. Without it the test suite needs
    the network to be up before it can tell us whether our routing logic is correct, and a
    provider outage becomes a red build with no bug behind it."""

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        msg = "the adapter opened a socket"
        raise AssertionError(msg)

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)

    driver, _ = driver_over(a_completion())
    registry = DriverRegistry(drivers={"anthropic": driver}, routers=routers_for(seed_chain()))
    PoolDispatcher(registry=registry).dispatch(Tier.MAIN, a_request())
    transports_per_pool(seed_chain(), lambda _t, _ids: Fake(a_completion()))

    failing, _ = driver_over(TransportStatusError(503))
    with pytest.raises(ProviderUnavailable):
        failing.complete(a_request())


def test_no_provider_sdk_is_imported_when_this_module_is_imported() -> None:
    """The import lives inside `litellm_transport` and nowhere else. At module scope it
    becomes a hard dependency of importing `brain.models` at all, so collecting the test
    suite would need the package installed, and the policy layer would sit one import away
    from a provider's types."""
    tree = ast.parse(inspect.getsource(adapter_module))
    top_level: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            top_level.add(node.module.split(".")[0])
    assert top_level.isdisjoint(
        {"litellm", "anthropic", "openai", "httpx", "requests", "aiohttp", "socket", "urllib3"}
    )
    assert "hard dependency" in SDK_IMPORT_LIVES_IN_ONE_PLACE


# ------------------------------------------------------- 2. the trigger set stays closed
@pytest.mark.parametrize(
    "exc",
    [
        ValueError("a bug in our own code"),
        RuntimeError("something the SDK invented last Tuesday"),
        TransportError(),
        type("OverloadedError", (Exception,), {})(),
        type("APIStatusError", (Exception,), {})(),
        type("TransportTimeout", (Exception,), {})(),  # nearly one of ours, and not one
        KeyError("model_list"),
    ],
)
def test_an_unrecognised_failure_is_never_a_retry_trigger(exc: BaseException) -> None:
    """The property that has to survive an SDK upgrade. A new error shape must land in the
    branch that stops the chain: mapping the unknown onto a connection error reads as
    resilience and is a retry loop that grows every time a provider names a new fault, at
    double the cost of every affected request and with the real failure buried."""
    failure = failure_from(exc, deployment_id=PRIMARY)
    assert failure.trigger is None
    assert (failure.timed_out, failure.connection_failed, failure.context_exceeded) == (
        False,
        False,
        False,
    )
    assert failure.status is None


def test_every_trigger_the_adapter_can_produce_is_a_member_of_the_closed_set() -> None:
    """`trigger_for` owns the rule and this adapter must not have a second opinion. A
    trigger invented here would be a fallback the policy layer never authorised, and the
    tests that assert the policy would still pass."""
    produced = {
        failure_from(exc, deployment_id=PRIMARY).trigger
        for exc in (
            adapter_module.TransportTimeoutError(),
            adapter_module.TransportConnectionError(),
            adapter_module.ContextWindowExceededError(),
            TransportStatusError(429),
            TransportStatusError(500),
            TransportStatusError(400),
            ContentPolicyRefusedError(status=503),
            ValueError("unknown"),
        )
    }
    assert {t.value for t in produced if t is not None} <= FALLBACK_TRIGGER_VALUES


def test_the_reason_the_unknown_stops_the_chain_is_written_down() -> None:
    """A rule with no argument attached is a rule the next person deletes, and "treat
    unknown errors as retryable, it is more robust" is a genuinely attractive suggestion
    until the consequence is stated."""
    assert "unbounded retry loop" in AN_UNRECOGNISED_FAILURE_IS_NOT_A_TRIGGER


# --------------------------------------------------------- 3. a refusal is not an outage
@pytest.mark.parametrize("status", [None, 400, 429, 500, 503])
def test_a_content_policy_refusal_does_not_reach_the_next_model(status: int | None) -> None:
    """Decided on 5 September. A refusal is a property of the request, not of the
    provider's health: the next rung receives the same prompt and declines the same way,
    so the chain spends its whole fallback budget to arrive at an identical answer, and
    where a cross-provider rung does answer, what it has done is shop for a provider
    willing to say yes. The status is dropped deliberately, because a refusal delivered as
    a 503 would otherwise be classified as a provider error and retried."""
    failure = failure_from(ContentPolicyRefusedError(status=status), deployment_id=PRIMARY)
    assert failure.trigger is None
    assert failure.status is None
    assert failure.connection_failed is False
    assert failure.timed_out is False


def test_a_refusal_delivered_as_an_answer_is_not_turned_into_a_failure() -> None:
    """The other shape it arrives in, and the more common one. A 200 whose finish reason
    says the model declined is a successful call; failing it here would send the chain to
    the next rung by the other door, which is the same forbidden fallback."""
    driver, transport = driver_over(
        a_completion(text="I can't help with that.", finish_reason="content_filter")
    )
    response = driver.complete(a_request())
    assert response.finish_reason == "content_filter"
    assert len(transport.calls) == 1, "a refusal was retried"


def test_the_refusal_rule_is_written_down_with_its_reason() -> None:
    assert "shops for a provider willing to say yes" in A_REFUSAL_IS_NOT_A_TRIGGER


# ------------------------------------------------- 4. nothing of the request gets logged
def test_no_part_of_the_request_reaches_the_exception() -> None:
    """The most common way a key ends up in a log aggregator. The prompt, the extra
    mapping and the provider's own message all carry secrets in practice, and an exception
    is copied verbatim into every log line and every error tracker that catches it."""
    driver, _ = driver_over(TransportStatusError(500, code=SECRET))
    with pytest.raises(ProviderUnavailable) as caught:
        driver.complete(a_request(text=f"my key is {SECRET}", extra={"api_key": SECRET}))

    rendered = f"{caught.value!s} {caught.value.failure.detail} {caught.value.detail}"
    assert SECRET not in rendered
    assert "my key is" not in rendered
    assert "api_key" not in rendered


def test_the_sdks_own_exception_is_not_chained_onto_ours() -> None:
    """`raise ... from None`, not `from exc`. Chaining keeps the SDK's exception in
    `__cause__`, where every traceback formatter and every log aggregator renders its
    text, and that text routinely contains the request it failed on, headers included."""
    driver, _ = driver_over(TransportStatusError(500, code=SECRET))
    with pytest.raises(ProviderUnavailable) as caught:
        driver.complete(a_request())
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


def test_the_trace_records_the_call_and_never_its_contents() -> None:
    """The trace outlives the answer it describes, is kept for longer, and is read by more
    people. It is therefore the worst place in the system to put the company's own data or
    anything a caller smuggled through `extra`."""
    driver, _ = driver_over(a_completion(text=f"the answer is {SECRET}"))
    with capture_logs() as events:
        driver.complete(a_request(text=f"my key is {SECRET}", extra={"api_key": SECRET}))

    assert events, "the call was not traced at all"
    blob = repr(events)
    assert SECRET not in blob
    assert "my key is" not in blob
    # The trace still has to be useful, or somebody will add the prompt back.
    assert events[0]["deployment_id"] == PRIMARY
    assert events[0]["ok"] is True


def test_a_failed_call_is_traced_without_its_request_either() -> None:
    """The failure path is the one where a helpful engineer adds the prompt, because that
    is the case where they want it."""
    driver, _ = driver_over(TransportStatusError(429, code="rate_limit_error"))
    with capture_logs() as events, pytest.raises(ProviderUnavailable):
        driver.complete(a_request(text=f"my key is {SECRET}", extra={"api_key": SECRET}))

    blob = repr(events)
    assert SECRET not in blob
    assert "my key is" not in blob
    assert events[0]["trigger"] is FallbackTrigger.RATE_LIMITED
    assert events[0]["ok"] is False


def test_no_error_name_we_repeat_verbatim_could_be_a_credential() -> None:
    """The allowlist is only as good as its contents. A closed set with a token-shaped
    entry in it is a denylist wearing an allowlist's name."""
    assert all(re.fullmatch(r"[a-z][a-z_]{2,31}", code) for code in KNOWN_PROVIDER_CODES)


def test_the_reason_the_request_stays_out_of_the_error_is_written_down() -> None:
    assert "publishes a credential" in NOTHING_FROM_THE_REQUEST_IN_AN_EXCEPTION


# ------------------------------------------------------------------- 5. one call per call
def test_the_adapter_never_retries_on_its_own() -> None:
    """Attempts belong to `CallPolicy` and to the executor. A retry here is an attempt the
    breaker never sees, so a breaker shown one failure where three happened opens on the
    fourth incident rather than the second, and the wall-clock budget for the answer lane
    is spent by a dependency nobody is measuring."""
    for answer in (
        adapter_module.TransportTimeoutError(),
        adapter_module.TransportConnectionError(),
        TransportStatusError(503),
        ContentPolicyRefusedError(),
        ValueError("unknown"),
    ):
        driver, transport = driver_over(answer)
        with pytest.raises(ProviderUnavailable):
            driver.complete(a_request())
        assert len(transport.calls) == 1, f"{type(answer).__name__} was retried in the adapter"


def test_a_pool_router_is_never_handed_another_pools_deployments() -> None:
    """M5.1.4 as a structural fact rather than a convention. Tag filtering is inoperative
    in SDK mode, so a Router built with every tier's deployments is free to serve a HEAVY
    request from the SMALL pool, and it does it silently."""
    chain = seed_chain()
    seen: dict[Tier, tuple[str, ...]] = {}

    def factory(tier: Tier, ids: tuple[str, ...]) -> Any:
        seen[tier] = ids
        return Fake(a_completion())

    transports_per_pool(chain, factory)
    for tier, ids in seen.items():
        for other in Tier:
            if other is tier:
                continue
            foreign = {r.deployment.id for r in chain.rungs_for(other)}
            assert not (set(ids) & foreign), f"the {tier} Router was given a {other} deployment"
    assert "not expressible rather than merely discouraged" in ONE_TRANSPORT_PER_POOL
