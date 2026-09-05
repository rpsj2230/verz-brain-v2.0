"""The concrete driver: one call out, one outcome back, nothing of the request left behind.

Task ids: M5.1.1, M5.1.4
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

from brain.models.adapter import (
    LITELLM_ERROR_NAMES,
    Completion,
    ContentPolicyRefusedError,
    ContextWindowExceededError,
    PoolDispatcher,
    ProviderSdkMissingError,
    SdkDriver,
    TransportConnectionError,
    TransportError,
    TransportStatusError,
    TransportTimeoutError,
    completion_from_sdk,
    failure_from,
    is_refusal,
    litellm_model_list,
    litellm_transport,
    safe_code,
    transports_per_pool,
)
from brain.models.driver import (
    DriverMessage,
    DriverRegistry,
    DriverRequest,
    DriverResponse,
    ModelDriver,
    ProviderUnavailable,
    Role,
    routers_for,
)
from brain.models.routing import FallbackTrigger, Tier, seed_chain

PRIMARY = "anthropic-sonnet-global"
MODEL = "claude-sonnet-5"


def a_request(
    *,
    deployment_id: str = PRIMARY,
    model: str = MODEL,
    extra: dict[str, str] | None = None,
) -> DriverRequest:
    return DriverRequest(
        deployment_id=deployment_id,
        model=model,
        messages=(DriverMessage(role=Role.USER, content="how many hours are left on SNM"),),
        timeout_seconds=12.0,
        extra=extra or {},
    )


def a_completion(**overrides: Any) -> Completion:
    fields: dict[str, Any] = {
        "text": "Fourteen.",
        "finish_reason": "stop",
        "input_tokens": 900,
        "output_tokens": 12,
        "cached_input_tokens": 800,
        "served_model": "",
    }
    fields.update(overrides)
    return Completion(**fields)


class Fake:
    """A transport that records what it was asked and answers however the test says."""

    def __init__(self, answer: Completion | BaseException) -> None:
        self.answer = answer
        self.calls: list[DriverRequest] = []

    def __call__(self, request: DriverRequest) -> Completion:
        self.calls.append(request)
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def driver_over(answer: Completion | BaseException) -> tuple[SdkDriver, Fake]:
    transport = Fake(answer)
    return SdkDriver(provider="anthropic", transport=transport), transport


# --------------------------------------------------------------------- the success path
def test_the_response_names_the_deployment_we_routed_to() -> None:
    """If the adapter could report a different endpoint from the one the chain planned,
    a silent retry elsewhere would be invisible: the attempt row would name the rung we
    chose and the invoice would name something else."""
    driver, _ = driver_over(a_completion())
    assert driver.complete(a_request()).deployment_id == PRIMARY


def test_the_model_the_provider_says_it_used_is_the_one_recorded() -> None:
    """Providers answer with a version-stamped id, and that is the id on the bill. Echoing
    what we asked for instead would make the price-book key disagree with the invoice."""
    driver, _ = driver_over(a_completion(served_model="claude-sonnet-5-20260601"))
    assert driver.complete(a_request()).model == "claude-sonnet-5-20260601"


def test_a_provider_that_names_no_model_falls_back_to_the_one_we_asked_for() -> None:
    """Deleting this makes an empty model string reach the meter, which prices at zero."""
    driver, _ = driver_over(a_completion(served_model=""))
    assert driver.complete(a_request()).model == MODEL


def test_cached_reads_are_kept_apart_from_ordinary_input_tokens() -> None:
    """Prompt caching is worth roughly five times the routing lever at this workload. A
    usage record that folds cached reads into input tokens cannot show whether it works."""
    driver, _ = driver_over(a_completion(input_tokens=900, cached_input_tokens=800))
    usage = driver.complete(a_request()).usage
    assert (usage.input_tokens, usage.cached_input_tokens) == (900, 800)


def test_the_adapter_satisfies_the_seam_it_was_built_against() -> None:
    """The whole point of `driver.py` was that building the concrete adapter is one file
    and nothing else changes. If this fails, the seam was drawn in the wrong place."""
    driver, _ = driver_over(a_completion())
    assert isinstance(driver, ModelDriver)
    assert isinstance(driver.complete(a_request()), DriverResponse)


# --------------------------------------------------------------------- failure mapping
@pytest.mark.parametrize(
    ("error", "trigger"),
    [
        (TransportTimeoutError(), FallbackTrigger.TIMEOUT),
        (TransportConnectionError(), FallbackTrigger.CONNECTION_ERROR),
        (TransportStatusError(429), FallbackTrigger.RATE_LIMITED),
        (TransportStatusError(503), FallbackTrigger.PROVIDER_ERROR),
        (ContextWindowExceededError(), FallbackTrigger.CONTEXT_EXCEEDED),
    ],
)
def test_each_recognised_failure_lands_on_its_own_trigger(
    error: BaseException, trigger: FallbackTrigger
) -> None:
    """The five shapes the chain is allowed to move on. Without this mapping the executor
    receives a failure with no trigger for everything and the fallback chain is dead
    configuration that never runs."""
    assert failure_from(error, deployment_id=PRIMARY).trigger is trigger


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_a_client_error_stops_the_chain(status: int) -> None:
    """A 4xx that is not a 429 is our request being wrong, and the next rung gets the same
    request. Retrying spends money to receive the same error twice."""
    assert failure_from(TransportStatusError(status), deployment_id=PRIMARY).trigger is None


def test_an_unrecognised_error_is_a_failure_and_never_a_retry() -> None:
    """The property that has to hold across an SDK upgrade. A new exception shape must
    land in the branch that stops the chain; mapping the unknown onto a connection error
    reads as resilience and is an unbounded retry loop that grows with every error a
    provider invents."""
    failure = failure_from(ValueError("something new"), deployment_id=PRIMARY)
    assert failure.trigger is None
    assert failure.connection_failed is False
    assert failure.timed_out is False
    assert failure.status is None


def test_a_transport_error_with_no_subclass_is_also_unrecognised() -> None:
    """The base class carries no flag on purpose, so an SDK error we could not translate
    stops the chain instead of borrowing the meaning of whichever branch caught it."""
    assert failure_from(TransportError(), deployment_id=PRIMARY).trigger is None


def test_a_failure_reaches_the_caller_as_degraded_not_failed() -> None:
    """A provider outage and a bug in our own code go to different people. Raising Failed
    here would put them in one bucket and the on-call rotation would chase the wrong one."""
    driver, _ = driver_over(TransportTimeoutError())
    with pytest.raises(ProviderUnavailable) as caught:
        driver.complete(a_request())
    assert caught.value.failure.trigger is FallbackTrigger.TIMEOUT


# ------------------------------------------------------------------ content-policy rules
def test_a_content_policy_refusal_does_not_reach_the_next_model() -> None:
    """Decided on 5 September, and the reason is in `CONTENT_POLICY_REFUSAL_IS_NOT_A
    _TRIGGER`: a refusal is a property of the request, so the next rung reproduces it at
    full cost, and where a cross-provider rung does answer, the chain has shopped for a
    provider willing to say yes. The status is dropped deliberately: a refusal delivered
    as a 503 must not be classified as a provider error."""
    failure = failure_from(ContentPolicyRefusedError(status=503), deployment_id=PRIMARY)
    assert failure.trigger is None
    assert failure.status is None


def test_a_refusal_that_arrives_as_an_ordinary_answer_stays_an_answer() -> None:
    """The common shape: a 200 whose finish reason says the model declined. Turning it
    into a failure would send the chain to the next rung for a call that succeeded, which
    is the same fallback the decision above forbids, arriving by the other door."""
    driver, transport = driver_over(
        a_completion(text="I can't help with that.", finish_reason="content_filter")
    )
    response = driver.complete(a_request())
    assert response.finish_reason == "content_filter"
    assert is_refusal(response.finish_reason)
    assert len(transport.calls) == 1


@pytest.mark.parametrize("reason", ["stop", "length", "tool_calls", ""])
def test_an_ordinary_finish_reason_is_not_read_as_a_refusal(reason: str) -> None:
    """A refusal check that over-matched would mark truncated answers as declined and
    route them to abstention, which is a wrong answer with an explanation attached."""
    assert is_refusal(reason) is False


# ------------------------------------------------------------------- no retry in here
def test_the_adapter_makes_exactly_one_call_per_request() -> None:
    """Attempts are counted by `CallPolicy` and executed by the executor. A retry inside
    the adapter is an attempt the breaker never sees, so a breaker shown one failure where
    three happened opens on the fourth incident instead of the second."""
    driver, transport = driver_over(TransportConnectionError())
    with pytest.raises(ProviderUnavailable):
        driver.complete(a_request())
    assert len(transport.calls) == 1


def test_our_timeout_is_the_one_handed_to_the_transport() -> None:
    """The timeout comes from the rung and the lane override, not from the SDK's default.
    Letting the SDK own it means the answer lane's wall-clock budget is enforced by a
    dependency's configuration file."""
    driver, transport = driver_over(a_completion())
    driver.complete(a_request())
    assert transport.calls[0].timeout_seconds == 12.0


# ----------------------------------------------------------------- one router per pool
def test_one_router_is_built_per_pool_and_never_one_holding_every_tier() -> None:
    """M5.1.4. Tag filtering is a proxy-mode feature, so a Router driven as an SDK selects
    from whatever list it was constructed with. One Router holding every tier could serve
    a HEAVY request from the SMALL pool, silently."""
    seen: list[tuple[Tier, tuple[str, ...]]] = []

    def factory(tier: Tier, ids: tuple[str, ...]) -> Any:
        seen.append((tier, ids))
        return Fake(a_completion())

    chain = seed_chain()
    pools = transports_per_pool(chain, factory)

    assert set(pools) == {Tier.MAIN, Tier.HEAVY}
    for tier, ids in seen:
        assert ids == tuple(r.deployment.id for r in chain.rungs_for(tier))
    every_id = {r.deployment.id for r in chain.rungs}
    assert all(set(ids) != every_id for _, ids in seen), "one Router was given every pool"


def test_a_pool_with_no_rungs_gets_no_router() -> None:
    """An empty Router accepts calls and fails all of them, which surfaces as a provider
    outage. The honest surface for an unconfigured tier is a missing key."""
    pools = transports_per_pool(seed_chain(), lambda _tier, _ids: Fake(a_completion()))
    assert Tier.SMALL not in pools
    assert Tier.NONE not in pools


# -------------------------------------------------------------------- the dispatcher
def registry_over(answer: Completion | BaseException) -> tuple[DriverRegistry, Fake]:
    driver, transport = driver_over(answer)
    return (
        DriverRegistry(drivers={"anthropic": driver}, routers=routers_for(seed_chain())),
        transport,
    )


def test_the_dispatcher_calls_the_rung_the_request_names() -> None:
    registry, transport = registry_over(a_completion())
    response = PoolDispatcher(registry=registry).dispatch(Tier.MAIN, a_request())
    assert response.deployment_id == PRIMARY
    assert len(transport.calls) == 1


def test_the_dispatcher_refuses_a_deployment_from_another_pool() -> None:
    """The whole of M5.1.4 at the call site. A HEAVY deployment reached through the MAIN
    pool is the cross-pool selection the per-pool Router exists to make impossible."""
    registry, transport = registry_over(a_completion())
    heavy = a_request(deployment_id="anthropic-opus-global", model="claude-opus-5")
    with pytest.raises(ValueError, match="does not serve"):
        PoolDispatcher(registry=registry).dispatch(Tier.MAIN, heavy)
    assert transport.calls == [], "the call went out before the pool was checked"


def test_the_dispatcher_refuses_a_request_naming_a_model_the_rung_does_not_serve() -> None:
    """The rung's model is half the price-book key. Serving a different one meters the
    request against the wrong price, and the discrepancy surfaces as an invoice nobody can
    explain a month later."""
    registry, _ = registry_over(a_completion())
    with pytest.raises(ValueError, match="which serves"):
        PoolDispatcher(registry=registry).dispatch(Tier.MAIN, a_request(model="claude-opus-5"))


def test_a_provider_with_no_adapter_registered_is_reported_as_degraded() -> None:
    """Holding no client for a provider is the same class of event as that provider being
    down: the request cannot be served here and the honest answer is to say so."""
    registry = DriverRegistry(drivers={}, routers=routers_for(seed_chain()))
    with pytest.raises(ProviderUnavailable, match="no driver registered"):
        PoolDispatcher(registry=registry).dispatch(Tier.MAIN, a_request())


# ------------------------------------------------------------------ the SDK boundary
def test_the_model_list_asks_the_sdk_for_no_retries_and_declares_no_fallbacks() -> None:
    """The SDK is a driver, never a router of its own. A fallback list here moves the
    policy inside a dependency, where our tests cannot see it and our traces cannot
    explain it, and the console's matrix becomes advisory."""
    entries = litellm_model_list(routers_for(seed_chain())[Tier.MAIN])
    assert [e["model_name"] for e in entries] == [
        "anthropic-sonnet-global",
        "anthropic-sonnet-us-east-1",
    ]
    for entry in entries:
        assert entry["litellm_params"]["num_retries"] == 0
        assert "fallbacks" not in entry
        assert "fallbacks" not in entry["litellm_params"]


def test_the_model_list_carries_no_credential() -> None:
    """The SDK reads the key from the environment, so it never enters a structure of ours
    and therefore cannot reach a trace, a log or an exception we build."""
    blob = repr(litellm_model_list(routers_for(seed_chain())[Tier.HEAVY])).lower()
    for word in ("api_key", "apikey", "authorization", "secret", "token", "bearer"):
        assert word not in blob


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Timeout", TransportTimeoutError),
        ("APIConnectionError", TransportConnectionError),
        ("ContextWindowExceededError", ContextWindowExceededError),
        ("ContentPolicyViolationError", ContentPolicyRefusedError),
        ("RateLimitError", TransportStatusError),
    ],
)
def test_the_sdk_error_names_translate_onto_our_closed_set(name: str, expected: type) -> None:
    """Matched by class name because importing the SDK's exception types would put the SDK
    back at module scope, which is the one thing this module is arranged to avoid."""
    from brain.models.adapter import translate_sdk_error

    exc = type(name, (Exception,), {})()
    exc.status_code = 429  # type: ignore[attr-defined]
    assert isinstance(translate_sdk_error(exc), expected)


def test_an_sdk_error_nobody_has_seen_before_translates_to_the_base_class() -> None:
    """Which produces no trigger, so a renamed exception in a new SDK release costs one
    fallback rather than starting an unbounded retry against an error we cannot describe."""
    from brain.models.adapter import translate_sdk_error

    translated = translate_sdk_error(type("BrandNewError", (Exception,), {})())
    assert type(translated) is TransportError
    assert failure_from(translated, deployment_id=PRIMARY).trigger is None


def test_every_name_we_translate_is_one_we_have_a_meaning_for() -> None:
    """A table entry pointing at a kind the translator does not handle would fall through
    to the unknown branch while reading as though it were mapped."""
    assert set(LITELLM_ERROR_NAMES.values()) == {
        "timeout",
        "connection",
        "status",
        "context",
        "refusal",
    }


def test_a_response_missing_its_usage_block_is_ordinary_rather_than_an_outage() -> None:
    """A model that emitted only tool calls returns null content, and some responses carry
    no usage at all. An adapter that raised on either would report a working provider as
    down."""
    empty = completion_from_sdk(object())
    assert empty == Completion(text="", finish_reason="", served_model="")


def test_a_full_sdk_response_is_read_field_by_field() -> None:
    """Pins the shape we read. A silent change here produces zero-token usage records,
    which price at nothing and make the cost report quietly wrong."""

    class Bag:
        def __init__(self, **kw: Any) -> None:
            self.__dict__.update(kw)

    raw = Bag(
        model="claude-sonnet-5-20260601",
        choices=[Bag(message=Bag(content="Fourteen."), finish_reason="stop")],
        usage=Bag(
            prompt_tokens=900, completion_tokens=12, prompt_tokens_details=Bag(cached_tokens=800)
        ),
    )
    assert completion_from_sdk(raw) == a_completion(served_model="claude-sonnet-5-20260601")


def test_asking_for_an_sdk_that_is_not_installed_says_exactly_that(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The adapter is complete without the SDK, and an adapter that quietly degraded to a
    stub here would report a working chain that answers nothing."""

    def refuse(name: str) -> Any:
        raise ImportError(name)

    monkeypatch.setattr(importlib, "import_module", refuse)
    with pytest.raises(ProviderSdkMissingError, match="not installed"):
        litellm_transport(routers_for(seed_chain())[Tier.MAIN])


# ------------------------------------------------------------------------ the log code
@pytest.mark.parametrize(
    ("raw", "kept"),
    [
        ("rate_limit_error", "rate_limit_error"),
        ("RATE_LIMIT_ERROR", "rate_limit_error"),
        ("  overloaded_error  ", "overloaded_error"),
        # Name-shaped and a credential. A charset rule would admit this one.
        ("sk-ant-api03-abcdefghij", ""),
        ("hf_qwertyuiopasdfghjkl", ""),
        ("something_new_from_the_provider", ""),
        ("", ""),
    ],
)
def test_only_an_error_name_we_already_know_survives_into_a_log_line(raw: str, kept: str) -> None:
    """A closed set of values, not a pattern. An API key is `[a-z0-9-]+` too, so a rule
    that admitted anything name-shaped would pass a credential into the log store while
    reading in review as though it sanitised something. The cost of the closed set is a
    missing log field for an error nobody has catalogued yet."""
    assert safe_code(raw) == kept
