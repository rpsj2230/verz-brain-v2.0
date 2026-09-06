"""The only thing here that speaks to the inference server, and it decides nothing.

`brain.ops.inference` holds the wire contract and `brain.knowledge.embed_policy` holds every
decision taken about an embedding before a socket is opened. This is the third piece and it is
deliberately the thinnest: it implements `brain.knowledge.embed_queue.EmbeddingService`, which
`NOTHING_IMPLEMENTS_THE_EMBEDDING_SERVICE` has said nothing does since that seam was declared.
The split is the one the layout table states about `brain.ops.limits` and
`brain.ops.limit_store`, and the reason given there applies unchanged: the cases worth testing
on an embedding leg are a server that does not answer, a server that answers with something
nobody expected, and a batch that fails partway, and not one of them can be tested through a
module that opens a connection.

**So nothing in this module is a decision.** Which model, which width, how long a request may
take, what an outage means to the person waiting, whether a vector may be written: every one of
those is in `embed_policy`, imported rather than repeated. What is here is a URL, a POST, the
translation of a transport failure into `CallOutcome`, and the breaker that
`NO_BUCKET_GUARDS_A_SOURCE_WITH_NO_QUOTA` says applies. If a rule appears in this file, it is
in the wrong file.

**A read-only mapping is not JSON**, which is the one thing about this seam that is easy to
get wrong late. `embedding_request` returns `MappingProxyType` all the way down, on purpose:
it is what stops a caller adding a department to a request on its way out. `json.dumps`
refuses a `mappingproxy`, so the conversion happens here, at the last possible moment, in the
module that owns the wire. Doing it in `embedding_request` would have meant handing out a
mutable dictionary to protect a serialiser, which is trading the guard for the convenience.

**The retry is the queue and is deliberately not here.** `brain.connectors.throttle.retry_delay`
exists and is not called: a loop that slept and tried again would hold a queue slot for the
length of an outage while the queue believed the job was running, and the job is `Redrive.SAFE`
precisely so the queue can be the retry. What is used from that module is `classify`, so a
timeout and a connection failure reach the breaker as the same outcome, and `record_outcome`,
so a 429 never counts as ill health.

**A 4xx is not an outage and is raised as a different thing.** `CallOutcome.REJECTED` means
this repository sent something the server would not accept, and it will send the same thing
again at full cost; that is a contract failure and reads as `InferenceRefused`, the same class
`decode_embeddings` raises. `UNAVAILABLE` and `QUOTA` are `EmbeddingUnavailable`, which is what
`outage_response` is written about. The two are separate so an operator is not sent to look at
a server that is fine.

**Nothing calls anything in this module.** `make_client` needs `Settings.inference_url`, which
is empty on every install today because `docker-compose.inference.yml` names an image that does
not exist and could not run on this host if it did; `embed` needs a batch, and nothing enqueues
one because `brain.ops.queue.NO_DRIVER_IS_INSTALLED`. It has never been run against a server,
only against a fake, and the fake exercises the refusals rather than standing in for a service.
That is the same refusal `docker-compose.inference.yml` makes about M7.3.3 and this module does
not change it.

Scope: the client half. This module opens a connection and reads a clock, and it is the only
one on this leg that may.

Task ids: none
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Protocol, cast

import httpx
import structlog

from brain.connectors.throttle import CallOutcome, classify, connector_breaker, record_outcome
from brain.knowledge.embed_policy import (
    EMBED_TIMEOUT_SECONDS,
    EmbeddingUnavailable,
    accept_vectors,
)
from brain.knowledge.embed_queue import Embedded, EmbeddingBatch
from brain.models.routing import CircuitBreaker
from brain.ops.inference import InferenceRefused, decode_embeddings, embedding_request

log = structlog.get_logger()

#: The path an embedding request is posted to. One place, because the server and this client
#: are two programs and a path spelled differently is a 404 that classifies as `REJECTED`,
#: which reads as this system having sent something malformed.
EMBED_PATH: Final = "/embed"

#: The name the breaker is keyed by. `CircuitBreaker.deployment_id` holds a connector name
#: here, which reads oddly and is the cost `ONE_BUCKET_AND_ONE_BREAKER` accepts rather than
#: keep a second state machine.
BREAKER_NAME: Final = "inference-server"


def embed_url(base_url: str) -> str:
    """Where a batch is posted, or a refusal naming the setting that is empty.

    Refuses an empty address rather than defaulting to localhost. A default here would be a
    client that starts, posts the text of a client's documents at whatever answers on this
    host, and is discovered by the answers getting quietly worse, which is the argument
    `docker-compose.inference.yml` makes about `INFERENCE_IMAGE` having no default either.
    """
    address = base_url.strip()
    if not address:
        msg = (
            "no inference address is configured, so there is nowhere to send text to be "
            "embedded; set INFERENCE_URL on a profile that deploys an inference server, "
            "which brain.ops.inference.inference_config_conflicts checks at startup"
        )
        raise EmbeddingUnavailable(msg)
    return f"{address.rstrip('/')}{EMBED_PATH}"


def jsonable(value: object) -> object:
    """The request as something `json.dumps` will accept, and nothing else changed.

    `embedding_request` hands out `MappingProxyType` at every level so a caller cannot add a
    key to it, and the standard encoder refuses a `mappingproxy` outright. Converting here
    rather than there keeps the guard where it protects something: a request that could grow a
    key could grow one naming a scope, which is the structural half of
    `THE_INFERENCE_SERVER_IS_DOWNSTREAM_OF_THE_GATE`.

    Strings and bytes are checked before the sequence branch, because both are sequences and
    a string taken apart into a list of characters is a request that serialises and means
    nothing.
    """
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (str, bytes, bytearray)):
        return value
    if isinstance(value, Sequence):
        return [jsonable(item) for item in value]
    return value


class InferenceResponse(Protocol):
    """What this client reads off a response, and not one field more.

    `httpx.Response` satisfies it structurally. Narrow because it is also the seam the tests
    run against, which is the argument `brain.ops.limit_store.WindowPipeline` makes about
    itself: a fake with two members is a fake nobody makes clever enough to hide a bug.
    """

    @property
    def status_code(self) -> int: ...

    def json(self) -> object: ...


class TransportError(Exception):
    """The request did not complete, so there is no status and no body to classify.

    Declared here rather than letting `httpx`'s own exceptions cross the seam, so a fake in a
    test does not have to import an HTTP library to fail. `timed_out` is carried because it is
    the one distinction the transport can make and this module cannot: `classify` folds it
    back into `UNAVAILABLE` a line later, and that folding is a decision written down in
    `AN_OUTAGE_HERE_IS_NEVER_A_FACT_ABOUT_THE_DOCUMENT` rather than an accident of catching.
    """

    def __init__(self, detail: str, *, timed_out: bool = False) -> None:
        super().__init__(detail)
        self.timed_out = timed_out


class InferenceTransport(Protocol):
    """Whatever posts a body and returns a response. One method, one direction."""

    def post(
        self, url: str, *, json: Mapping[str, object] | object, timeout: float
    ) -> InferenceResponse: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class InferenceEmbeddingClient:
    """`EmbeddingService`, over HTTP, holding a breaker and no policy at all.

    Mutable rather than frozen because the breaker is state and `CircuitBreaker` is a value:
    every transition returns a new one, so the field is reassigned. That is the same shape
    `brain.ops.limit_store.ValkeyWindowStore` has for its health counters and for the same
    reason, which is that the object is a client and clients accumulate.

    The clock is injected for the reason `CircuitBreaker` gives about `now` being a parameter
    everywhere it appears: a breaker whose cooldown can only be observed by waiting is a
    breaker nobody tests the half-open probe of.
    """

    transport: InferenceTransport
    url: str
    timeout_seconds: float = EMBED_TIMEOUT_SECONDS
    breaker: CircuitBreaker = field(default_factory=lambda: connector_breaker(BREAKER_NAME))
    clock: Callable[[], datetime] = _utc_now

    def embed(self, batch: EmbeddingBatch) -> tuple[Embedded, ...]:
        """One batch, sent and read back, or one of two refusals. Never a partial answer.

        The breaker is claimed before the request and released by the outcome, which is
        `try_admit`'s claim-and-return: a half-open probe that nothing released would leave
        the breaker permanently admitting one caller that never arrives.

        Nothing here decides what the caller does about a failure. `outage_response` holds
        that, per leg, because an outage while ingesting and one while answering a question
        are different events, and a client that raised the same thing for both would leave the
        difference to whoever wrote the `except`.
        """
        self.breaker, admitted = self.breaker.try_admit(self.clock())
        if not admitted:
            msg = (
                "the inference server's breaker is open, so this batch was not sent; the "
                "breaker exists to stop a queue of embedding jobs from spending a slot each "
                "on a server that is already known to be down"
            )
            raise EmbeddingUnavailable(msg)

        try:
            response = self.transport.post(
                self.url, json=jsonable(embedding_request(batch)), timeout=self.timeout_seconds
            )
        except TransportError as exc:
            outcome = classify(timed_out=exc.timed_out, connection_failed=not exc.timed_out)
            self.breaker = record_outcome(self.breaker, outcome, now=self.clock())
            # The address is not logged, and neither is the exception's own text: an inference
            # URL is where a client's documents go, and the transport's message carries it.
            log.warning("inference server unreachable", timed_out=exc.timed_out)
            msg = (
                f"the inference server did not answer for {len(batch.units)} chunk(s); "
                "nothing was written, and a timeout and a refused connection are the same "
                "event here because neither says anything about the text that was sent"
            )
            raise EmbeddingUnavailable(msg) from exc

        outcome = classify(status=response.status_code)
        self.breaker = record_outcome(self.breaker, outcome, now=self.clock())
        if outcome is CallOutcome.REJECTED:
            # Not an outage. The server is answering and will answer the same way again, so
            # this is the contract failing rather than the deployment, and it is raised as the
            # class `decode_embeddings` raises for the same reason.
            msg = (
                f"the inference server refused the request with {response.status_code}; the "
                "request was wrong rather than the server being unwell, so sending it again "
                "produces the same status at the same cost"
            )
            raise InferenceRefused(msg)
        if outcome is not CallOutcome.OK:
            msg = (
                f"the inference server answered {response.status_code} for "
                f"{len(batch.units)} chunk(s) and nothing was written"
            )
            raise EmbeddingUnavailable(msg)

        payload = response.json()
        if not isinstance(payload, Mapping):
            msg = (
                f"the inference server answered with {type(payload).__name__} where an "
                "embedding response was due; an answer that cannot be read is not an empty "
                "one, and neither may be recorded as a batch that wrote something"
            )
            raise InferenceRefused(msg)
        return accept_vectors(decode_embeddings(batch, payload))


@dataclass(frozen=True)
class _HttpxTransport:
    """`httpx` behind `InferenceTransport`, so its exceptions do not cross this seam.

    The whole of what it adds is the translation: `httpx` raises a tree of its own, and a
    client that caught `httpx.HTTPError` directly would make every fake in every test import
    an HTTP library in order to fail.
    """

    client: Any

    def post(
        self, url: str, *, json: Mapping[str, object] | object, timeout: float
    ) -> InferenceResponse:
        try:
            response = self.client.post(url, json=json, timeout=timeout)
        except httpx.TimeoutException as exc:
            # The type name and nothing else, matching `brain.ops.limit_store`: an httpx
            # message carries the URL, and this URL is where a client's documents go.
            raise TransportError(type(exc).__name__, timed_out=True) from exc
        except httpx.HTTPError as exc:
            raise TransportError(type(exc).__name__) from exc
        # A cast at a library boundary, in the shape `brain.ops.limit_store.make_store` uses:
        # `httpx.Response` satisfies the protocol structurally and asking mypy to prove it
        # about a library's overloads buys nothing the protocol does not already state.
        return cast(InferenceResponse, response)


def make_client(
    *, base_url: str, timeout_seconds: float = EMBED_TIMEOUT_SECONDS
) -> InferenceEmbeddingClient:
    """An embedding client pointed at a deployed inference server. Nothing calls this.

    The timeout is passed to `httpx` per request rather than set on the client, so the figure
    that governs it is `embed_policy.EMBED_TIMEOUT_SECONDS` and there is no second one on a
    connection object for it to disagree with.
    """
    return InferenceEmbeddingClient(
        transport=_HttpxTransport(client=httpx.Client()),
        url=embed_url(base_url),
        timeout_seconds=timeout_seconds,
    )
