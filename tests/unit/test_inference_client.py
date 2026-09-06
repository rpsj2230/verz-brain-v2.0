"""The client half: what reaches the wire, what comes back, and what a failure is called.

Three of these are written a particular way and the reason is worth reading first.

**The wire test serialises the request for real.** `embedding_request` hands out
`MappingProxyType` at every level, `json.dumps` refuses one, and asserting that the converted
payload encodes is only half the property: the other half is that the unconverted one does not,
which is what says the conversion is load-bearing rather than decorative. Both are asserted.

**The failure tests count the transport's calls.** A breaker that opens and a breaker that does
not both raise the same class from `embed`, and only whether the request left the process
separates them. Every breaker assertion here is about the call count or about the breaker's own
counter, never about the exception.

**The keys on the wire are asserted against `brain.ops.inference`**, which is where the contract
is declared, and in both directions. Asserting only that the expected keys are present would
pass with a `department` beside them, which is the one addition anybody would make in good
faith and the one the permission boundary is structural against.

Task ids: none
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from brain.knowledge.embed_policy import (
    COLUMN_DIMENSIONS,
    EMBED_TIMEOUT_SECONDS,
    EmbeddingUnavailable,
    served_embedding_model,
)
from brain.knowledge.embed_queue import EmbeddingBatch, EmbeddingUnit, writes_for
from brain.models.routing import BREAKER_CONSECUTIVE_FAILURES, BreakerState
from brain.ops.inference import (
    INPUT_KEYS,
    MODEL_KEYS,
    REQUEST_KEYS,
    VALUES_KEY,
    VECTORS_KEY,
    InferenceRefused,
    embedding_request,
)
from brain.ops.inference_client import (
    EMBED_PATH,
    InferenceEmbeddingClient,
    TransportError,
    embed_url,
    jsonable,
)
from brain.ops.queue import stale_after

MODEL = served_embedding_model(revision="v1.0.0")
A_URL = "http://inference:8080/embed"
FIXED_NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


def _unit_values() -> list[float]:
    return [1.0 / math.sqrt(COLUMN_DIMENSIONS)] * COLUMN_DIMENSIONS


def _batch(*chunk_ids: str) -> EmbeddingBatch:
    return EmbeddingBatch(
        model=MODEL,
        units=tuple(EmbeddingUnit(chunk_id=one, text=f"text for {one}") for one in chunk_ids),
    )


def _answer(batch: EmbeddingBatch, values: list[float] | None = None) -> dict[str, Any]:
    """A well-formed response for this batch, in the shape `decode_embeddings` reads."""
    return {
        "model": {
            "name": batch.model.name,
            "revision": batch.model.revision,
            "dimensions": batch.model.dimensions,
        },
        VECTORS_KEY: [
            {"chunk_id": unit.chunk_id, VALUES_KEY: values if values else _unit_values()}
            for unit in batch.units
        ],
    }


@dataclass
class FakeResponse:
    status_code: int
    payload: object = None

    def json(self) -> object:
        return self.payload


@dataclass
class FakeTransport:
    """Records what was posted and answers with whatever the test set up.

    Deliberately without any HTTP behaviour of its own. The one thing it models that a lambda
    would not is that a request either produced a response or did not, which is the distinction
    the whole outage path is built on.
    """

    response: FakeResponse | None = None
    raises: TransportError | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def post(self, url: str, *, json: object, timeout: float) -> FakeResponse:
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if self.raises is not None:
            raise self.raises
        assert self.response is not None
        return self.response


def _client(transport: FakeTransport) -> InferenceEmbeddingClient:
    return InferenceEmbeddingClient(transport=transport, url=A_URL, clock=lambda: FIXED_NOW)


# ------------------------------------------------------------------ the address


def test_an_address_nobody_configured_is_refused_rather_than_defaulted() -> None:
    """A default address is a client that starts, posts the text of a client's documents at
    whatever answers on this host, and is found out by the answers getting quietly worse. It is
    the same refusal `docker-compose.inference.yml` makes about having no default image.

    Delete this and an empty `INFERENCE_URL` becomes localhost, which is somebody else's
    process on a shared host."""
    with pytest.raises(EmbeddingUnavailable, match="INFERENCE_URL"):
        embed_url("   ")


def test_the_address_is_joined_to_the_path_without_producing_a_double_slash() -> None:
    """The positive case, and the trailing slash is not a nicety: `http://host//embed` is a
    different path to most servers and returns a 404, which `classify` reads as `REJECTED`,
    which reads as this system having sent something malformed.

    Delete this and an address copied out of a browser with its trailing slash breaks the
    embedding leg with a message blaming the request."""
    assert embed_url("http://inference:8080") == "http://inference:8080/embed"
    assert embed_url("http://inference:8080/") == "http://inference:8080/embed"
    assert EMBED_PATH == "/embed"


# ------------------------------------------------------------------ what reaches the wire


def test_a_request_reaches_the_wire_as_something_json_will_encode() -> None:
    """Both halves. The converted payload has to encode, and the unconverted one has to not:
    without the second assertion this test would pass with `jsonable` returning its argument,
    and the guard would be decoration.

    Delete this and the read-only mappings that keep a scope out of a request become a
    `TypeError` at the first real request, discovered after everything else was built."""
    batch = _batch("k_a.0000")

    assert json.loads(json.dumps(jsonable(embedding_request(batch))))
    with pytest.raises(TypeError):
        json.dumps(embedding_request(batch))


def test_the_request_on_the_wire_carries_the_declared_keys_and_no_others() -> None:
    """Asserted in both directions against `brain.ops.inference`, which is where the contract
    is declared. The permission boundary here is structural: a request that cannot grow a key
    cannot grow one naming a scope, and a test asserting only that the expected keys are
    present would pass with a department beside them.

    Delete this and the client can add a field to the body for the far side's convenience, and
    the far side starts receiving something it could make a decision with."""
    transport = FakeTransport(response=FakeResponse(200, _answer(_batch("k_a.0000"))))
    _client(transport).embed(_batch("k_a.0000"))

    body = transport.calls[0]["json"]
    assert set(body) == set(REQUEST_KEYS)
    assert set(body["model"]) == set(MODEL_KEYS)
    assert all(set(one) == set(INPUT_KEYS) for one in body["inputs"])


def test_the_client_sends_the_model_the_batch_named_and_holds_none_of_its_own() -> None:
    """The client is the half that holds no policy, and the model is the clearest case: it
    comes off the batch, so a client cannot embed with something the caller did not ask for.

    Delete this and a default model can be given to the client, at which point the identity
    recorded beside a vector is the one somebody configured rather than the one that was
    asked for."""
    batch = _batch("k_a.0000")
    transport = FakeTransport(response=FakeResponse(200, _answer(batch)))
    _client(transport).embed(batch)

    assert transport.calls[0]["json"]["model"] == {
        "name": batch.model.name,
        "revision": batch.model.revision,
        "dimensions": batch.model.dimensions,
    }


def test_the_timeout_on_the_wire_is_short_enough_that_the_queue_will_not_redrive_the_job() -> None:
    """Asserted against the queue's own orphan threshold rather than against the constant the
    client defaults to, which would compare a figure with itself.

    Delete this and the client can be given a timeout of its own, and a slow server has the
    same batch sent twice with nothing in the log saying so."""
    transport = FakeTransport(response=FakeResponse(200, _answer(_batch("k_a.0000"))))
    _client(transport).embed(_batch("k_a.0000"))

    assert transport.calls[0]["timeout"] == EMBED_TIMEOUT_SECONDS
    assert transport.calls[0]["timeout"] < stale_after().total_seconds()


# ------------------------------------------------------------------ the answer


def test_a_well_formed_answer_becomes_vectors_matched_to_the_chunks_that_were_sent() -> None:
    """The positive case for the whole client. Every refusal below is satisfied by a client
    that refuses everything, and this is the one that says it can also work.

    It goes on to `writes_for`, which is what the caller would do, so the assertion is that the
    result is usable rather than merely non-empty.

    Delete this and the client can be broken in a way that only shows up when there is a real
    server to notice it with."""
    batch = _batch("k_a.0000", "k_a.0001")
    transport = FakeTransport(response=FakeResponse(200, _answer(batch)))

    embedded = _client(transport).embed(batch)
    writes = writes_for(batch, embedded)

    assert [one.chunk_id for one in embedded] == ["k_a.0000", "k_a.0001"]
    assert [write.chunk_id for write in writes] == ["k_a.0000", "k_a.0001"]


def test_a_vector_the_far_side_did_not_normalise_never_reaches_the_corpus() -> None:
    """The client applies the policy's acceptance rule rather than handing decoded vectors
    straight back, which is the only place that rule can be applied to a real response.

    Delete this and the check exists in `embed_policy` and is called by nothing, which is a
    guard that is correct, tested and never invoked."""
    batch = _batch("k_a.0000")
    transport = FakeTransport(
        response=FakeResponse(200, _answer(batch, values=[1.0] * COLUMN_DIMENSIONS))
    )

    with pytest.raises(InferenceRefused, match="normalise"):
        _client(transport).embed(batch)


def test_a_body_that_is_not_a_mapping_is_refused_rather_than_read_as_an_empty_batch() -> None:
    """A server answering 200 with a list, or with null, is a server answering with something
    nobody expected, and the difference between that and an empty batch matters: one of the two
    could otherwise be recorded as a batch that wrote nothing and completed.

    Delete this and a malformed body reaches `decode_embeddings` as something it has to guess
    about."""
    transport = FakeTransport(response=FakeResponse(200, ["not", "a", "mapping"]))

    with pytest.raises(InferenceRefused, match="list"):
        _client(transport).embed(_batch("k_a.0000"))


# ------------------------------------------------------------------ when it goes wrong


@pytest.mark.parametrize("timed_out", [True, False])
def test_a_server_that_never_answered_is_an_outage_whichever_way_it_failed(
    timed_out: bool,
) -> None:
    """A timeout and a refused connection are folded into one outcome on purpose: neither says
    anything about the text that was sent, and `AN_OUTAGE_HERE_IS_NEVER_A_FACT_ABOUT_THE_DOCUMENT`
    is the argument for not letting the difference reach a person as `TIMED_OUT`.

    Delete this and a slow server can be reported differently from an absent one, which is a
    wording telling somebody to split a file that is fine."""
    transport = FakeTransport(raises=TransportError("boom", timed_out=timed_out))
    client = _client(transport)

    with pytest.raises(EmbeddingUnavailable):
        client.embed(_batch("k_a.0000"))
    assert client.breaker.consecutive_failures == 1


def test_a_server_that_refused_the_request_is_not_reported_as_an_outage() -> None:
    """A 4xx is this repository sending something the server will not accept, and it will be
    refused identically next time at full cost. Calling it an outage sends an operator to look
    at a server that is fine, and it is why `is_retryable` says no to `REJECTED`.

    Delete this and a permanent contract failure is retried by the queue until the redrive cap,
    three times, with the same result."""
    transport = FakeTransport(response=FakeResponse(400))
    client = _client(transport)

    with pytest.raises(InferenceRefused, match="400"):
        client.embed(_batch("k_a.0000"))
    assert client.breaker.consecutive_failures == 0


def test_a_server_that_is_unwell_counts_against_the_breaker_and_a_busy_one_does_not() -> None:
    """The distinction `A_QUOTA_REFUSAL_IS_NOT_ILL_HEALTH` exists for, asserted on the breaker's
    own counter rather than on which exception came out. A 429 counted as ill health opens the
    circuit whenever the service is popular.

    Delete this and every refusal feeds the breaker, so the busiest install is the one whose
    embedding leg is intermittently unavailable."""
    unwell = _client(FakeTransport(response=FakeResponse(503)))
    with pytest.raises(EmbeddingUnavailable):
        unwell.embed(_batch("k_a.0000"))

    busy = _client(FakeTransport(response=FakeResponse(429)))
    with pytest.raises(EmbeddingUnavailable):
        busy.embed(_batch("k_a.0000"))

    assert unwell.breaker.consecutive_failures == 1
    assert busy.breaker.consecutive_failures == 0


def test_a_batch_is_not_sent_at_all_once_the_breaker_has_opened() -> None:
    """The point of the breaker on this leg. Embedding work is queued, so a dead server would
    otherwise be handed every batch in the backlog, one queue slot at a time, and each would
    wait for the full timeout before failing.

    The assertion is the transport's call count, because the exception is the same either way.

    Delete this and the breaker can be dropped from the client entirely with every other test
    here still passing."""
    transport = FakeTransport(response=FakeResponse(503))
    client = _client(transport)

    for _attempt in range(BREAKER_CONSECUTIVE_FAILURES):
        with pytest.raises(EmbeddingUnavailable):
            client.embed(_batch("k_a.0000"))
    sent_before = len(transport.calls)

    with pytest.raises(EmbeddingUnavailable, match="breaker"):
        client.embed(_batch("k_a.0000"))

    assert client.breaker.state is BreakerState.OPEN
    assert len(transport.calls) == sent_before


def test_a_busy_server_is_still_asked_after_as_many_refusals_as_would_open_the_breaker() -> None:
    """The other side of the same rule, and the one that would be missing if the breaker were
    fed everything. The count of requests that left the process is the whole assertion.

    Delete this and quota refusals can be fed to the breaker without any test noticing, which
    takes a working service out of rotation on evidence that it is working."""
    transport = FakeTransport(response=FakeResponse(429))
    client = _client(transport)

    for _attempt in range(BREAKER_CONSECUTIVE_FAILURES + 1):
        with pytest.raises(EmbeddingUnavailable):
            client.embed(_batch("k_a.0000"))

    assert len(transport.calls) == BREAKER_CONSECUTIVE_FAILURES + 1
    assert client.breaker.state is BreakerState.CLOSED
