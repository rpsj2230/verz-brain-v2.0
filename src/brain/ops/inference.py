"""The one process that loads a model, and the whole of what this repository may ask of it.

Item 31 of `docs/needs-rupash.md` was decided on 2026-09-06 as Option A: parsing, embedding
and entity recognition run in a service of their own and the Brain asks it over the network.
The measurement that decided it is worth repeating, because every number below descends from
it: `docling` alone pulls 83 further packages including torch, 678 MB compressed and about
1.5 GB installed, into a container budgeted 512 MiB. Three models in three Brain containers
would be three copies of that stack. One service is one copy, and this module is the seam
between that service and everything here.

**It is an external system and it gets a connector's treatment, not a library's.** It can be
slow, it can be absent, and it can answer with something nobody expected, and not one of
those three may become a wrong answer. `brain.connectors.throttle` already owns how this
repository classifies a call to something outside itself, so the breaker and the retry are
imported rather than written again. The bucket is deliberately not applied, and
`NO_BUCKET_GUARDS_A_SOURCE_WITH_NO_QUOTA` says why. What is genuinely new here is the third
failure: a well-formed response that is wrong. See `decode_embeddings`.

**A refusal is a raised exception and never an empty result**, and the two are separated in
the code precisely so that they are identical to a person. See
`A_REFUSAL_IS_NOT_AN_EMPTY_RESULT`. On the parse leg the person-facing wording already
exists and is already right: `ParseCause.PARSER_UNAVAILABLE` says the file has not been read
yet and that nothing is wrong with it. On the retrieval leg an unreachable server has the
same effect as a rebuild in flight, which `brain.knowledge.embedding` already decided: no
vector leg, the lexical leg unaffected, and an answer that is thinner rather than wrong.

**The model is read off the response and never filled in from the request**, which is the
one subtle thing in this file. `brain.knowledge.embed_queue.writes_for` refuses a response
whose model disagrees with its batch, and that guard is worth nothing if the value it
compares against was copied from the batch a moment earlier: it would then be comparing the
batch with itself and passing for every model the server could possibly have used. So the
response states its model as three fields, `EmbeddingModel` is constructed from them, and
the comparison downstream is between two independently sourced values. See
`THE_MODEL_ON_THE_WIRE_IS_READ_FROM_THE_RESPONSE_AND_NEVER_FROM_THE_REQUEST`.

**Nothing about a response may widen what a caller sees, and the boundary is stated rather
than assumed.** The text sent to this service has already passed the permission layer:
`chunk_document` copied a document's permissions onto the chunk row, `EmbeddingUnit` carries
an id and a text and has no field that could hold a scope, and the request this module builds
carries exactly those two keys per input. In the other direction the far side cannot inject,
because `writes_for` refuses an id that was not in the batch. And the service has no
connection string to this system's database, so it cannot read a row it was not given.
See `THE_INFERENCE_SERVER_IS_DOWNSTREAM_OF_THE_GATE`.

**Capped twice, and the second cap is not a heap ceiling.** `docker-compose.langfuse.yml`
pairs each cgroup limit with a ceiling in the runtime's own units, `GOMEMLIMIT` for Go and
`--max-old-space-size` for Node. This service is Python, so `brain.knowledge.parse_budget`'s
argument applies unchanged: `RLIMIT_AS` bounds address space rather than heap and a process
given its container's limit fails on an import. There is a second reason here that is
specific to a model server and is the more interesting one: **the dominant term is not the
heap at all, it is the weights**, which are resident for the life of the process and are not
allocated per request. A heap ceiling above them caps nothing and one below them refuses to
load. So the second cap is a request ceiling, sized from what is left after the weights and
the runtime, and the thread caps that bound the only other thing that scales. See
`THE_SECOND_CAP_IS_THE_REQUEST_CEILING_BECAUSE_THE_WEIGHTS_ARE_NOT_THE_HEAP`.

**None of the sizing figures has been measured, and saying so is the point.** The weights are
arithmetic from published parameter counts at a stated precision, which is a floor and not a
measurement; the runtime reserve is a judgement of the same kind as
`brain.knowledge.parse_budget.PARSE_EXPANSION`, with nothing behind it. There is no such
server and no models pulled, so there is nothing on this host to measure. `ServedModel`
refuses to be constructed without prose saying where its figure came from, which is the
shape `brain.ops.pii.BuiltIn` uses for `why_not_local`.

**What this does to the budget, reported rather than resolved.** The component is 3072 MiB
and it takes `standard` from 256 MiB over to 3328 MiB over. That is a finding, not a reason
to shrink a component: `brain.ops.wiring` opens by saying that sizing to the remainder
produces numbers that add up and containers that are OOM-killed under the first real load.
Three ways out exist and all three are Rupash's: a second host, smaller weights on the
embedding model, or int8 quantisation, which trades answer quality for memory.

**Why presidio-analyzer does not move here.** It is already a container, already sized from
measurement, and it loads a spaCy pipeline rather than a transformer. The model item 31 named
that has nowhere to live is GLiNER, M32.2.1.2, which `brain.ops.pii` records as the reason
romanised Chinese names are not detectable by pattern. That one is served here.

**What has no caller yet, stated plainly rather than implied.** Three things do have one.
`inference_config_conflicts` is called by `brain.config.check`, before the port is bound,
which is the only moment a lite install pointed at somebody else's inference server can be
stopped. `inference_gaps` is called by `brain.ops.worker.preflight`, unconditionally rather
than on a component name, for the reason `embed_batch_gaps` is called there: any container
draining `system` may be handed an embedding batch, so every one of them has to be able to
send it. And the component and the compose file are held equal by
`tests/unit/test_inference_deployment.py`.

Two things do not. `embedding_request` and `decode_embeddings` are the two halves of a wire
contract, and nothing in this repository implements `EmbeddingService`, which
`brain.knowledge.embed_queue.NOTHING_IMPLEMENTS_THE_EMBEDDING_SERVICE` already says and this
module does not change: what is added here is the shape of what goes over the wire and the
refusals it has to survive, not a client. `parse_cause_for` has no caller because there is no
parser; Docling is not a dependency of this project and the leaf it serves is M7.2.1.

**And one route is deliberately left open at the deployment end.** `docker-compose.worker.yml`
does not join the `inference` network, because a service naming a network that only exists in
`docker-compose.inference.yml` fails to start whenever that file is not composed beside it. So
the entry belongs on the commit that gives the worker something to send, which is the commit
that implements the protocol. `docker-compose.parse-worker.yml` says the same thing about
routing a parse, and saying it is worth more than a route that looks finished.

Scope: domain logic. Nothing here opens a connection, loads a model or reads a clock. The
protocol an implementation has to satisfy is `EmbeddingService` in
`brain.knowledge.embed_queue`, and a second one is deliberately not declared here.

Task ids: none
"""

from __future__ import annotations

import enum
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from brain.connectors.throttle import CallOutcome
from brain.knowledge.embed_queue import (
    MIB,
    Embedded,
    EmbeddingBatch,
    batch_budget_bytes,
)
from brain.knowledge.embedding import EmbeddedVector, EmbeddingError, EmbeddingModel
from brain.knowledge.ingest import ParseCause
from brain.ops.wiring import (
    Wiring,
    WiringError,
    assert_known_profile,
    component,
    components_for,
)

# ------------------------------------------------------------------ written-down reasons

#: Where the permission boundary is, and which side of it this service sits on.
THE_INFERENCE_SERVER_IS_DOWNSTREAM_OF_THE_GATE: Final = (
    "Everything sent here has already passed the permission layer. A chunk's text reaches "
    "this service because chunk_document copied a document's permissions onto the row it "
    "will be written back to, and the request carries an id and a text and nothing else "
    "that could hold a scope, an owner or a visibility. So the service cannot widen "
    "anything in three separate ways, and each is structural rather than a convention. It "
    "is never asked a question, only handed text somebody was already entitled to. It has "
    "no connection string to this system's database, so it cannot read a row nobody sent "
    "it. And a response naming a chunk id that was not in the batch is refused by "
    "writes_for, which is the only route by which the far side could put a vector against a "
    "passage it was never given. What this service decides is what a piece of text looks "
    "like, which is the same thing brain.ops.pii says about a detector: never an "
    "authorisation boundary, in either direction."
)

#: Why an outage raises rather than returning nothing, and why a person cannot tell.
A_REFUSAL_IS_NOT_AN_EMPTY_RESULT: Final = (
    "An unreachable service and a service that answered with nothing are the same event to "
    "whoever is waiting and must be different events in the code, because one of them is "
    "allowed to be written down as a result. A batch that comes back empty is refused by "
    "writes_for as a response missing every id it asked for, and a batch that could not be "
    "sent raises here; neither writes a row, and that is what stops an outage becoming a "
    "corpus of chunks recorded as embedded by a model that never saw them. To a person the "
    "two are identical on purpose: on the parse leg both are "
    "ParseCause.PARSER_UNAVAILABLE, which says the file has not been read yet and that "
    "nothing is wrong with it, and on the retrieval leg both are an answer composed from "
    "the lexical leg alone, which brain.knowledge.embedding already argues is the right "
    "degradation because it is one somebody can see and reason about."
)

#: Why the response's model is parsed from the response rather than taken from the batch.
THE_MODEL_ON_THE_WIRE_IS_READ_FROM_THE_RESPONSE_AND_NEVER_FROM_THE_REQUEST: Final = (
    "writes_for refuses a response whose model disagrees with its batch, and that refusal is "
    "worth exactly nothing if the model it compares was copied out of the batch on the way "
    "in. The comparison is then between a value and itself: it passes for every set of "
    "weights the server could have been running, including the previous one it did not "
    "finish unloading, and the corpus ends up holding vectors from two spaces under one "
    "recorded identity, which is the failure brain.knowledge.embedding exists to prevent "
    "arriving through the check written to prevent it. So the response states name, revision "
    "and width as three fields, EmbeddingModel is constructed from them and validates all "
    "three, and a response that states no model is refused rather than defaulted. Three "
    "fields rather than the composite identity string because EmbeddingModel.identity is the "
    "only place that grammar is spelled, and a parser for it here would be a second copy of "
    "a rule, which is a rule that gets changed once."
)

#: Why the process-side cap is a request ceiling and not a ceiling in the runtime's units.
THE_SECOND_CAP_IS_THE_REQUEST_CEILING_BECAUSE_THE_WEIGHTS_ARE_NOT_THE_HEAP: Final = (
    "A cgroup limit is enforced by killing the process, so it protects the neighbours on a "
    "shared host and does nothing for the work in flight. The pair to it everywhere else "
    "here is a ceiling in the runtime's own units, and this runtime has none worth setting "
    "for two reasons rather than one. CPython has no heap ceiling at all: RLIMIT_AS bounds "
    "address space, which shared libraries and allocator arenas reserve without touching, so "
    "a process given its container's limit fails on an import. And a model server's "
    "dominant term is not the heap, it is the weights, which are mapped once and stay "
    "resident, so a ceiling above them caps nothing and one below them refuses to load. What "
    "is left is a decision taken before the work, which is the same conclusion "
    "brain.knowledge.parse_budget reached about a file: a request larger than the ceiling is "
    "refused before a model is called, the ceiling is what remains of the container after "
    "the weights and the runtime, and the number of requests served at once is one because "
    "the ceiling is the whole of that remainder. What it does not cover is a request under "
    "the ceiling whose activations are not, and that one is an OOM kill, because the "
    "allocation is inside a C extension and the kernel's answer is SIGKILL."
)

#: Why no failure of this service is ever reported as a fact about the document.
AN_OUTAGE_HERE_IS_NEVER_A_FACT_ABOUT_THE_DOCUMENT: Final = (
    "Once parsing runs here, every way this service can fail arrives at somebody who "
    "uploaded a file, and most of the wordings available say something about the file. "
    "ParseCause.CORRUPT tells them to re-export it, UNSUPPORTED tells them to convert it, "
    "OUT_OF_MEMORY tells them to split it, and TIMED_OUT tells them to split it if it "
    "happens twice. Not one of those is true when the server was restarting, and each of "
    "them costs somebody an afternoon acting on it. classify cannot separate a slow server "
    "from an absent one either, because a timeout and a connection failure are both "
    "UNAVAILABLE, so TIMED_OUT is deliberately not produced from an inference outcome even "
    "though it is the closest wording: the one sentence that is true whichever of the two "
    "happened is PARSER_UNAVAILABLE, which says the file has not been read yet and that "
    "nothing is wrong with it. Every non-OK outcome maps to it, and the mapping being "
    "uniform is the guard rather than a shortcut."
)

#: Why a token bucket is not put in front of this source and the breaker still is.
NO_BUCKET_GUARDS_A_SOURCE_WITH_NO_QUOTA: Final = (
    "brain.connectors.throttle puts a bucket, a breaker and a retry in front of every "
    "external system, and two of the three apply here unchanged. The bucket does not, and "
    "refusing it is a decision rather than an omission. A bucket exists to keep us inside "
    "somebody else's published allowance; this service is ours, has no allowance and "
    "publishes nothing, so limits_for would raise UnmeasuredSourceError and inventing a "
    "ceiling would produce a number that looks measured, sits in a console beside three that "
    "are, and is wrong in whichever direction somebody guessed. What actually bounds our "
    "volume is the slot count: embedding work is TrafficClass.SYSTEM, queue_name_for refuses "
    "per-task queues, and the queue runs one system job per worker at a time. The breaker "
    "and the retry do apply, because a service of ours can be down exactly as a service of "
    "theirs can, and an embedding call has no side effect at the far end, so is_retryable "
    "answers yes for UNAVAILABLE and QUOTA without a read-back."
)


# ------------------------------------------------------------------ what is served, and how big

#: The name of the component in `brain.ops.wiring` that runs this service. Named rather than
#: assumed for the reason `brain.knowledge.parse_budget.PARSE_WORKER_COMPONENT` is named: every
#: figure below is arithmetic against one container's limit and the wrong container's limit
#: gives an answer rather than an error.
INFERENCE_COMPONENT: Final = "inference-server"


class InferenceTask(enum.StrEnum):
    """What this service is asked to do. Closed, because the sizing is a sum over it.

    Three, and they are the three item 31 names. An open vocabulary would let a fourth model
    be served without appearing in the weights total, which is the one number the container's
    limit has to cover before it covers a single request.
    """

    #: Text to vectors. M7.3.3, and the only task with a declared seam in this repository.
    EMBEDDING = "embedding"
    #: A document to blocks, layout-aware. M7.2.1.
    PARSING = "parsing"
    #: Names the deterministic recognisers cannot see. M32.2.1.2.
    ENTITY_RECOGNITION = "entity_recognition"


@dataclass(frozen=True)
class ServedModel:
    """One model this service keeps resident, and where its footprint figure came from.

    `sizing_basis` is required prose and is the whole reason this is a class rather than a
    dictionary of numbers. Every figure here is a floor computed from something published or
    a judgement with nothing behind it, and a bare integer cannot tell a later reader which.
    `brain.ops.pii.BuiltIn` requires `why_not_local` for the same reason and it is the same
    kind of claim: a number in a memory budget is an assertion about a machine, and one whose
    origin nobody wrote down is an assertion nobody can check.

    `measured` is separate from the prose and defaults to False, so that describing a figure
    as measured is a deliberate edit rather than a sentence somebody wrote loosely.

    `WiringError` rather than `EmbeddingError` for both refusals, because this is a component
    declaration and not an embedding arrangement: the same family `Component` refuses a
    missing memory limit in, and for the same reason.
    """

    task: InferenceTask
    name: str
    #: Resident weights, in mebibytes. Not the download size and not the disk footprint: what
    #: has to be in memory at once for this model to answer.
    weights_mib: int
    sizing_basis: str
    measured: bool = False

    def __post_init__(self) -> None:
        if self.weights_mib < 1:
            msg = (
                f"model {self.name!r} declares no resident weights; a model that costs "
                "nothing to hold is a model the container was not sized for"
            )
            raise WiringError(msg)
        if not self.sizing_basis.strip():
            msg = (
                f"model {self.name!r} does not say where its {self.weights_mib} MiB came "
                "from; a memory figure with no stated origin cannot be checked by anybody"
            )
            raise WiringError(msg)


#: What the service holds resident, one entry per task.
#:
#: **Every figure is a floor and none is a measurement.** There is no such server on this
#: host and no weights pulled, so there is nothing to measure; the alternative to saying so
#: would be a number that looks like a result. Precision is stated per model rather than
#: assumed, because it is the multiplier: the same parameter count at fp32 is twice what it
#: is at fp16, and choosing the precision is choosing the container's size.
SERVED_MODELS: Final[tuple[ServedModel, ...]] = (
    ServedModel(
        task=InferenceTask.EMBEDDING,
        name="qwen3-embedding-0.6b",
        weights_mib=1152,
        sizing_basis=(
            "0.6e9 parameters at 2 bytes each, which is the fp16 precision the published "
            "weights are in, is 1.2e9 bytes and so 1145 MiB; rounded up to the next 128. "
            "Arithmetic from a parameter count is a floor: it excludes the tokeniser, the "
            "vocabulary tables and every activation the model needs to answer with"
        ),
    ),
    ServedModel(
        task=InferenceTask.ENTITY_RECOGNITION,
        name="gliner",
        weights_mib=832,
        sizing_basis=(
            "roughly 0.21e9 parameters on a DeBERTa-v3-base backbone at 4 bytes each is "
            "0.84e9 bytes and so 801 MiB; rounded up to the next 128. fp32 rather than fp16 "
            "because the published checkpoint is fp32 and nothing here has checked that a "
            "quantised copy returns the same character offsets, and a span that moves by one "
            "is the half-redacted name brain.ops.pii spends a paragraph on"
        ),
    ),
    ServedModel(
        task=InferenceTask.PARSING,
        name="docling-layout-and-tableformer",
        weights_mib=512,
        sizing_basis=(
            "a judgement and not arithmetic, because Docling ships several models and this "
            "project has never installed it, so there is no parameter count to multiply. "
            "Bounded rather than guessed freely: it is set below the two figures above, "
            "which are the models whose sizes are computable, and item 31 measured the "
            "package stack at about 1.5 GB installed before any weights are fetched"
        ),
    ),
)

#: What the cgroup counts and the weights do not: the interpreter, torch or the ONNX runtime,
#: the tokenisers, the HTTP server, and the allocator arenas a threaded process never returns.
#:
#: **A judgement with no measurement behind it**, in the same register as
#: `brain.knowledge.parse_budget.PARSE_EXPANSION`. It is the figure most likely to be wrong,
#: and it is wrong in the safe direction only while it is generous: too small and the request
#: ceiling below is computed from memory the runtime has already taken.
INFERENCE_RUNTIME_RESERVE_MIB: Final = 512

#: How many requests the service answers at once. One, and derived rather than chosen, exactly
#: as `brain.knowledge.parse_budget.PARSES_AT_ONCE` is: the ceiling below is the whole of what
#: is left after the weights and the reserve, so a second concurrent request would be allowed
#: the same whole remainder and the container has no second copy of it. It is also what a
#: CPU-bound model server wants anyway, because two requests on one set of threads interleave
#: rather than overlap and cost two activation working sets to do it.
REQUESTS_AT_ONCE: Final = 1


def weights_mib(models: Sequence[ServedModel] = SERVED_MODELS) -> int:
    """What this service holds resident before it is asked anything.

    A parameter with a default for the reason `brain.ops.queue.concurrency_gaps` gives about
    itself: a check that can only ever be run against the constant beside it cannot be shown
    to fail, and a check nobody has seen fail is a check nobody knows works.
    """
    return sum(model.weights_mib for model in models)


def served_model(task: InferenceTask) -> ServedModel:
    """The model serving this task, or a refusal naming the ones that exist.

    Refuses rather than returning None, matching `brain.ops.wiring.component`. A caller handed
    None writes `if model is None: return` and the task silently stops being served, which
    reads downstream as a service that answers slowly and then not at all.
    """
    for model in SERVED_MODELS:
        if model.task is task:
            return model
    msg = f"no model serves {task.value!r}; served: {[m.task.value for m in SERVED_MODELS]}"
    raise WiringError(msg)


def request_ceiling_bytes(
    *,
    server_component: str = INFERENCE_COMPONENT,
    reserve_mib: int = INFERENCE_RUNTIME_RESERVE_MIB,
    requests_at_once: int = REQUESTS_AT_ONCE,
    models: Sequence[ServedModel] = SERVED_MODELS,
) -> int:
    """The most one request may be allowed to cost, inside the container that serves it.

    The second cap, in the only units this runtime has. Derived from the component's own limit
    rather than set beside it, which is the rule `brain.knowledge.parse_budget.parse_budget_bytes`
    states and the reason is the same one squared here: two numbers governing one resource
    drift, and here the pair of them is the entire mechanism by which the second cap means
    anything at all.

    Can go negative, and deliberately is not clamped. A negative ceiling is the honest
    arithmetic for a container that cannot hold its own weights, and `inference_gaps` reports
    it in those words; clamping it to zero would turn a container that cannot start into one
    that refuses every request, which looks like a configuration problem rather than a sizing
    one.
    """
    limit_mib = component(server_component).memory_mib
    remainder_mib = limit_mib - weights_mib(models) - reserve_mib
    if requests_at_once < 1:
        # Guarded rather than divided by, because the caller's mistake is worth reporting as
        # itself. `inference_gaps` turns this into a sentence; returning the remainder here
        # keeps the arithmetic total so that the caller sees one finding rather than a
        # ZeroDivisionError standing in for one.
        return remainder_mib * MIB
    return (remainder_mib // requests_at_once) * MIB


# --------------------------------------------------- the profile flag, refusing (M32.1.1.4)

#: Environment settings that name somewhere to send text for a model to read. Any one of them
#: set on an install that deploys no inference server is the failure
#: `inference_config_conflicts` exists for, and it is a worse failure than the trace one it is
#: modelled on: a stray `LANGFUSE_HOST` ships spans, and a stray inference URL ships the text
#: of the client's own documents.
INFERENCE_DESTINATION_SETTINGS: Final = ("inference_url",)


def runs_inference_server(profile: str) -> bool:
    """Whether this profile deploys somewhere for text to be sent.

    Derived from `COMPONENTS` rather than declared a second time, matching
    `brain.ops.wiring.runs_trace_ledger`. A profile gains an inference server by a component
    naming it, which is the same edit that puts it in the budget, so the two cannot disagree.
    """
    assert_known_profile(profile)
    return any(c.name == INFERENCE_COMPONENT for c in components_for(profile))


def inference_config_conflicts(profile: str, values: Mapping[str, str]) -> tuple[str, ...]:
    """Inference destinations configured on an install that deploys no inference server.

    **The profile flag doing something rather than describing something**, in the shape
    `brain.ops.wiring.trace_config_conflicts` established, and for a heavier reason. A lite
    install runs no worker and no object store, so it ingests nothing and embeds nothing;
    what it can still carry is an `INFERENCE_URL` copied out of a standard install's
    environment file, pointing at a host that belongs to somebody else. Spans are metadata
    about a request. What goes to this address is the text of the document itself.

    Checked at startup against the declaration, where it fails loudly, rather than trusted to
    whoever copies an environment file between two installs. Returns every conflict rather
    than the first, matching `brain.config.check`: a misconfiguration found one variable at a
    time is a sequence of restarts.
    """
    assert_known_profile(profile)
    if runs_inference_server(profile):
        return ()
    return tuple(
        f"{setting} is set and profile {profile!r} deploys no inference server, so the text "
        f"of this client's documents would be sent to a host nobody here chose. Unset it, or "
        f"deploy a profile that runs one."
        for setting in INFERENCE_DESTINATION_SETTINGS
        if (values.get(setting) or "").strip()
    )


# ------------------------------------------------------------------ the wire, outbound

#: The keys one embedding request carries, and the whole of them. Asserted by test rather than
#: left to review: this tuple is the structural half of
#: `THE_INFERENCE_SERVER_IS_DOWNSTREAM_OF_THE_GATE`, because a request that cannot grow a key
#: cannot grow one that names a scope.
REQUEST_KEYS: Final[tuple[str, ...]] = ("model", "inputs")

#: The keys one input carries. An id and a text, which is exactly `EmbeddingUnit`'s two
#: fields, and nothing that could say who may read the passage they came from.
INPUT_KEYS: Final[tuple[str, ...]] = ("chunk_id", "text")

#: The keys a model block carries, in both directions. Three fields rather than the composite
#: identity string, so that `EmbeddingModel` is the only thing that ever spells the grammar.
MODEL_KEYS: Final[tuple[str, ...]] = ("name", "revision", "dimensions")

#: The key holding the vectors in a response, and the key holding one vector's numbers.
VECTORS_KEY: Final = "vectors"
VALUES_KEY: Final = "values"


def _model_block(model: EmbeddingModel) -> Mapping[str, object]:
    """A model as three fields. Read-only, so a caller cannot add a fourth on the way out."""
    return MappingProxyType(
        {"name": model.name, "revision": model.revision, "dimensions": model.dimensions}
    )


def embedding_request(batch: EmbeddingBatch) -> Mapping[str, object]:
    """What one batch looks like on the wire. Read-only, and carrying nothing else.

    Takes an `EmbeddingBatch` rather than a sequence of strings, matching the direction
    `EmbeddingService` is narrow in: the batch has already been checked against a memory
    budget, and a request built from loose strings would put that bound on the far side of a
    network call where this process can neither enforce it nor see it fail.

    `MappingProxyType` for the reason `EmbeddingWrite.columns` uses one, and with a sharper
    edge here. A mapping handed out of a value object is a mapping somebody mutates, and the
    mutation this one is protected against is the one that adds a department or a visibility
    "so the server can filter", which would send a permission to a process that has no
    business holding one and would make the response something a caller could read a decision
    out of.
    """
    return MappingProxyType(
        {
            "model": _model_block(batch.model),
            "inputs": tuple(
                MappingProxyType({"chunk_id": unit.chunk_id, "text": unit.text})
                for unit in batch.units
            ),
        }
    )


# ------------------------------------------------------------------ the wire, inbound


class InferenceRefused(EmbeddingError):  # noqa: N818 - the taxonomy in core.errors has no suffixes
    """The service answered, and the answer cannot be written down.

    An `EmbeddingError` rather than a family of its own, because everything a bad response
    can produce belongs to the embed leg and a caller wrapping that leg should catch one
    thing. Its own name inside the family, because this is the case an operator has to be
    able to separate from a budget refusal: a batch that was too large is our arithmetic, and
    this is the far side saying something nobody expected.
    """


def _model_from(payload: Mapping[str, object]) -> EmbeddingModel:
    """The model this response says produced its vectors, or a refusal.

    Constructed from three fields rather than parsed out of an identity string. See
    `THE_MODEL_ON_THE_WIRE_IS_READ_FROM_THE_RESPONSE_AND_NEVER_FROM_THE_REQUEST` for why it
    comes off the wire at all, and `MODEL_KEYS` for why it is three fields: `EmbeddingModel`
    validates the name, the revision and the width, so the constructor is the parser and
    there is no second spelling of the grammar to drift from the first.
    """
    block = payload.get("model")
    if not isinstance(block, Mapping):
        msg = (
            "the response states no model, so nothing can say which weights produced these "
            "vectors; filling it in from the request would make the check downstream compare "
            "the batch with itself and pass for every model the server could have run"
        )
        raise InferenceRefused(msg)
    missing = [key for key in MODEL_KEYS if key not in block]
    if missing:
        msg = (
            f"the response's model states {missing} nowhere; a model identity missing any of "
            f"{list(MODEL_KEYS)} cannot be told apart from the same name with different "
            "weights behind it, which is the model change that has no symptom"
        )
        raise InferenceRefused(msg)
    dimensions = block["dimensions"]
    if not isinstance(dimensions, int) or isinstance(dimensions, bool):
        msg = f"the response's model gives {dimensions!r} as a vector width, which is not one"
        raise InferenceRefused(msg)
    try:
        return EmbeddingModel(
            name=str(block["name"]), revision=str(block["revision"]), dimensions=dimensions
        )
    except EmbeddingError as exc:
        msg = f"the response names a model this system cannot record: {exc}"
        raise InferenceRefused(msg) from exc


def _values_from(chunk_id: str, raw: object) -> tuple[float, ...]:
    """One vector's numbers, refused for the three ways they can be poison.

    **Not a shape check.** `EmbeddedVector` already refuses a vector whose length disagrees
    with the model that claims it, and repeating that here would be a second copy of a rule.
    What is checked here is what only a wire response can carry, because nothing else in this
    system produces a float this module has not computed.

    A NaN anywhere makes every distance against that row NaN. PostgreSQL orders NaN above
    every real number, so the row never ranks, never appears, and never fails: the passage is
    in the corpus, indexed, cited by nothing, and no query reports it. An infinity does the
    same by a different route. And an all-zero vector is the shape a failed encode takes when
    something on the far side caught an exception and returned a default; its cosine distance
    to anything is undefined, and `EmbeddingUnit` has already refused empty text, so there is
    no legitimate way for one to arrive.
    """
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        msg = f"chunk {chunk_id!r} came back with {type(raw).__name__} where a vector was due"
        raise InferenceRefused(msg)
    values: list[float] = []
    for one in raw:
        if isinstance(one, bool) or not isinstance(one, (int, float)):
            msg = f"chunk {chunk_id!r} has {one!r} among its numbers, which is not one"
            raise InferenceRefused(msg)
        number = float(one)
        if not math.isfinite(number):
            msg = (
                f"chunk {chunk_id!r} came back with {number} in its vector; every distance "
                "against that row is then NaN, PostgreSQL sorts NaN above every real number, "
                "and the passage sits in the corpus indexed and unreachable with nothing "
                "anywhere reporting it"
            )
            raise InferenceRefused(msg)
        values.append(number)
    if values and not any(values):
        msg = (
            f"chunk {chunk_id!r} came back as a vector of zeros, which is what a failed "
            "encode returns when something on the far side caught it and defaulted; its "
            "cosine distance to anything is undefined, and the text it was made from cannot "
            "have been empty because EmbeddingUnit refuses that"
        )
        raise InferenceRefused(msg)
    return tuple(values)


def decode_embeddings(batch: EmbeddingBatch, payload: Mapping[str, object]) -> tuple[Embedded, ...]:
    """One response, read into the values `writes_for` checks against the batch.

    This is the half of the contract that has to survive a server answering with something
    nobody expected, and it deliberately stops short of deciding anything. It does not compare
    ids against the batch, it does not compare the model against the batch, and it does not
    order the result: all three belong to `writes_for`, which already refuses the four ways a
    response can disagree with the batch it answers. Doing any of them here would be a second
    copy of a rule, and the copy is the one that gets changed.

    What it does is the part `writes_for` cannot do, which is to produce values whose model
    came from somewhere other than the batch. See
    `THE_MODEL_ON_THE_WIRE_IS_READ_FROM_THE_RESPONSE_AND_NEVER_FROM_THE_REQUEST`.

    The batch is a parameter and is read for one thing only, which is how many chunks were
    asked about when a refusal has to say so. That narrowness is deliberate: a decoder that
    consulted the batch for anything else would be a decoder that could quietly agree with it.
    """
    model = _model_from(payload)
    vectors = payload.get(VECTORS_KEY)
    if not isinstance(vectors, Sequence) or isinstance(vectors, (str, bytes)):
        msg = (
            f"the response carries no {VECTORS_KEY!r} list for the {len(batch.units)} "
            "chunk(s) it was asked about; an absent list and an empty one are both a batch "
            "that wrote nothing, and neither may be recorded as one that wrote something"
        )
        raise InferenceRefused(msg)
    decoded: list[Embedded] = []
    for entry in vectors:
        if not isinstance(entry, Mapping):
            msg = f"the response holds {entry!r} where one chunk's vector was due"
            raise InferenceRefused(msg)
        chunk_id = str(entry.get("chunk_id") or "")
        if not chunk_id:
            msg = (
                "a vector came back naming no chunk; a vector matched to a chunk by its "
                "position in a list pairs the wrong text with the wrong numbers, and every "
                "row downstream looks well formed"
            )
            raise InferenceRefused(msg)
        values = _values_from(chunk_id, entry.get(VALUES_KEY))
        try:
            vector = EmbeddedVector(model=model, values=values)
        except EmbeddingError as exc:
            raise InferenceRefused(str(exc)) from exc
        decoded.append(Embedded(chunk_id=chunk_id, vector=vector))
    return tuple(decoded)


# ------------------------------------------------------------------ what a person is told


def parse_cause_for(outcome: CallOutcome) -> ParseCause | None:
    """What an uploader is told when a parse ran into this service rather than their file.

    None for a call that worked, because there is nothing to tell anybody. Every other
    outcome is `PARSER_UNAVAILABLE` and the uniformity is the guard rather than a shortcut:
    see `AN_OUTAGE_HERE_IS_NEVER_A_FACT_ABOUT_THE_DOCUMENT`. `TIMED_OUT` is the wording that
    looks closest and is not produced, because `classify` folds a slow server and an absent
    one into one outcome and that wording tells somebody to split a file that is fine.

    Nothing calls this yet. Docling is not a dependency of this project, so there is no parse
    to fail; the mapping is written where the outage is understood rather than left to be
    invented at the call site by whoever writes the parser, which is how a person ends up
    being told their document is corrupt because a container restarted.
    """
    if outcome is CallOutcome.OK:
        return None
    return ParseCause.PARSER_UNAVAILABLE


# ------------------------------------------------------------------ the deployment checks


def inference_gaps(
    *,
    server_component: str = INFERENCE_COMPONENT,
    reserve_mib: int = INFERENCE_RUNTIME_RESERVE_MIB,
    requests_at_once: int = REQUESTS_AT_ONCE,
    models: Sequence[ServedModel] = SERVED_MODELS,
) -> tuple[str, ...]:
    """Every reason this deployment cannot serve what this repository will ask it, in words.

    Called by `brain.ops.worker.preflight`, unconditionally rather than on a component name,
    for the reason `embed_batch_gaps` is called there: a slot is a slot, embedding work is
    `TrafficClass.SYSTEM`, and any container draining that queue may be handed a batch to
    send.

    Five checks, and each catches a different way the two caps stop being two.

    The tasks first. A task with no model is a request that can never be answered, and the
    failure has no error in it: the caller waits, the timeout fires, the outcome is
    `UNAVAILABLE`, and a deployment that is missing a model is indistinguishable from one
    that is merely down. `brain.ops.pii.configuration_gaps` makes the same check about a
    detection kind with no recogniser.

    Then whether the container holds its own weights at all, which is the check that fires
    when somebody adds a fourth model or moves one to fp32.

    Then the concurrency, which is `PARSES_AT_ONCE`'s argument one system along: the ceiling
    is the whole of the remainder, so more than one request at a time is that many times the
    container's memory promised out of one container's worth.

    Then the ceiling against the container, which is what makes it a second cap rather than a
    restatement of the first. A ceiling at or above the cgroup limit leaves the kill as the
    only enforcement.

    Then the two ends of the batch, which is the check this function exists for and the one
    edited in two files by two people for two reasons. `batch_budget_bytes` is derived from
    `MIB_PER_SLOT` and knows nothing about models; the ceiling is derived from a container
    limit and knows nothing about queues. If the ceiling is the smaller of the two, every full
    batch is refused by the server after the planner has built it, which presents as an
    embedding leg that works on small documents and fails on large ones.

    And the wiring, which is not a memory check at all and is here because this is the one
    function anything calls. `Wiring.NONE` is what stops the service being able to read a row
    nobody sent it; see `THE_INFERENCE_SERVER_IS_DOWNSTREAM_OF_THE_GATE`.

    Returns all of them rather than the first, matching `brain.ops.worker.preflight`: a
    deployment wrong in two ways is one where fixing either leaves it still wrong.
    """
    findings: list[str] = []
    served = {model.task for model in models}
    unserved = sorted(task.value for task in InferenceTask if task not in served)
    if unserved:
        findings.append(
            f"no model is declared for {unserved}, so a request for it can never be "
            "answered; the caller waits, the call times out, and a deployment missing a "
            "model looks exactly like one that is merely down"
        )

    limit_mib = component(server_component).memory_mib
    resident_mib = weights_mib(models) + reserve_mib
    ceiling = request_ceiling_bytes(
        server_component=server_component,
        reserve_mib=reserve_mib,
        requests_at_once=requests_at_once,
        models=models,
    )
    if resident_mib >= limit_mib:
        findings.append(
            f"{server_component!r} is {limit_mib} MiB and holds {weights_mib(models)} MiB of "
            f"weights plus a {reserve_mib} MiB runtime, which is {resident_mib} MiB before it "
            "is asked anything; the container cannot load what it is meant to serve and the "
            "kernel ends it during startup rather than under load"
        )
    if requests_at_once < 1:
        findings.append(
            f"{server_component!r} is set to serve {requests_at_once} request(s) at once, "
            "which is a service that answers nothing while reporting a listening socket"
        )
    elif requests_at_once > REQUESTS_AT_ONCE:
        findings.append(
            f"{server_component!r} would serve {requests_at_once} requests at once and the "
            f"ceiling is {ceiling // MIB} MiB each, which is {requests_at_once} times what is "
            f"left of the container; the ceiling is the whole remainder, so only "
            f"{REQUESTS_AT_ONCE} request(s) fit at a time"
        )

    if ceiling >= limit_mib * MIB:
        findings.append(
            f"a request ceiling of {ceiling // MIB} MiB against a {limit_mib} MiB limit on "
            f"{server_component!r} is a process told it may use everything the cgroup counts; "
            "the cgroup counts the weights and the runtime too, and it enforces by killing"
        )

    batch_bytes = batch_budget_bytes()
    if batch_bytes > ceiling:
        findings.append(
            f"a batch may cost {batch_bytes // MIB} MiB and {server_component!r} accepts "
            f"{ceiling // MIB} MiB per request; every full batch would be refused by the "
            "server after the planner had built it, which reads as an embedding leg that "
            "works on small documents and fails on large ones"
        )

    wiring = component(server_component).wiring
    if wiring is not Wiring.NONE:
        findings.append(
            f"{server_component!r} is wired {wiring.value!r}, so it holds a connection string "
            "to this system's database; it is handed text that has already passed the "
            "permission layer and must not be able to read a row nobody sent it"
        )
    return tuple(findings)
