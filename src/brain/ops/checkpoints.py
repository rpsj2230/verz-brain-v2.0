"""Where a graph's saved state goes, and the two ways that goes wrong without an error.

A durable graph writes its state after every step so a run can be resumed rather than
restarted. That state is a row in Postgres, and this module is the decision about which
Postgres, which schema, and on which connection. It holds no client: the saver is
constructed elsewhere from `CheckpointerConfig`, in the same split `brain.ops.limits` and
`brain.ops.limit_store` make and for the same reason, which is that a module that opens a
socket cannot be tested for the case that is always wrong.

Three things are load-bearing, and two of them are silent.

**The checkpointer does not go behind the transaction pooler, and this is the fourth time
that sentence has been written in this repository.** `brain.migrate` moved to
`pg_advisory_xact_lock`; `brain.session` sets `prepare_threshold=None`; `brain.ops.queue`
refuses a queue URL that names the pooler. The checkpointer's version is the prepared
statement: the saver prepares its statements server-side and pipelines them, and behind a
transaction pooler the backend that executes a statement is not the backend that prepared
it. That fails, which is better than the queue's version, but it fails in production only,
because a development machine has no pooler. `connection_refusals` asks
`brain.ops.queue.pooler_url_findings` rather than owning a second copy of the detection.

**A checkpoint is a payload, and payloads have a schema they are allowed to be in.** See
`A_CHECKPOINT_IS_A_PAYLOAD_NOT_A_TRACE`. The saved state is the run so far: the question, the
passages retrieved for it, the tool results. It is not metadata about a run, so `obs` is
wrong; it is not something the system learnt, so `mem` is wrong; it is not a transcript, so
`chat` is wrong. `agent` is where the artifacts of an agent run live and it is where this
goes.

**What stops a checkpoint becoming a copy of somebody's data is a refusal at the write
boundary, and it is deliberately not the mask `brain.ops.tracing` applies to a span.** That
module masks `payload_in` and `payload_out` before the span is constructed, and it can,
because a masked span is still a useful trace: an operator wants the shape, the outcome and
the latency, and never wanted the question. A masked checkpoint is not a degraded
checkpoint, it is a broken one. A resume from `[masked:str/medium]` either fails or, worse,
continues from a string that reads like state, and the second is a wrong answer with a full
audit trail behind it.

So the structural protection is the one `brain.ops.queue.Job` uses and not the one
`brain.ops.tracing.mask` uses: **a checkpoint carries references, and a value that is content
rather than a reference is refused rather than shortened.** `MAX_ARGUMENT_CHARS` is imported
from the queue rather than chosen again here, because a job row and a checkpoint row are the
same question asked twice. `PERSISTABLE_CHANNELS` is the closed set of state a graph may
save, and `checkpoint_refusals` is a pure function over a proposed write, so "the retrieved
passages never reach the saver" is a test rather than a review comment.

Three rules and each catches a different way the state grows a payload. A channel nobody
declared is refused outright, because the failure is a graph author adding a working field
and the store keeping it: default-deny is what `brain.core.field_policy` and
`brain.ops.tracing.SAFE_ATTRIBUTES` both do for the same reason. A value over the length is
content. A list or a dictionary is refused whatever its length, which is the rule
`tracing._keep` reached from the other side: a container under a declared name is where
somebody puts a record while meaning to put a summary, and the retrieved passages are a list.

**A re-driven job resolves entitlements afresh, so what it may see can narrow or widen
between attempts, and both are correct.** `E_run(caller, agent) = E(caller) ∩ agent_ceiling`
is evaluated at the attempt, never at the enqueue. Narrowing is the one that must be
guaranteed: a grant revoked between two attempts is a grant deleted, and a queue row or a
checkpoint that still carried the earlier answer would be the one place in this system where
a revocation does not take effect. Widening is the honest consequence of the same rule and it
is harmless, because the caller is entitled to it now; the alternative is a stored copy of a
permission decision outliving the decision, which is what this repository refuses everywhere
else.

The reason that argument holds is the refusal above rather than anything at resume time. **A
checkpoint that carried retrieved passages would be a widening no re-resolution could
undo**, because the rows are already inside the state and nothing downstream would ask again.
Carrying only references means a resume must re-fetch, a re-fetch goes through the gate, and
the gate resolves the caller's reach at that moment. The re-check is not a step somebody has
to remember; it is the only way the state can be reconstituted at all.

`may_resume` is the smaller half of the same rule: a checkpoint is not a bearer token. It
belongs to the principal the run was started for, and a resume by anybody else is refused
whatever their own entitlements are, because the state was assembled under a reach that was
not theirs.

**The saver's tables are not ours, and `CHECKPOINT_SCHEMA` is the whole of what we can do
about it.** See `THE_CHECKPOINT_TABLES_ARE_NOT_OURS`. The library creates them itself, so
Alembic does not own them, they arrive with no row-level security, and no migration can
enable it on a table that does not exist yet. What the schema choice buys is that
`brain.ops.sweeps.sweep_rls` reads `brain.db.SCHEMAS` and therefore looks in `agent`: the
tables land somewhere that check can see, and the gap becomes a red sweep instead of an
absence. The default lands them in `public`, which that sweep does not enumerate.

**What does not exist, and this is the part to read before believing anything above.**
`langgraph` is not in `uv.lock` and nothing here builds a graph: `src/brain/agents/` is a
docstring and no code. So no saver is constructed from this configuration, no checkpoint has
ever been written, and M32.4.1.2 is not claimed. Two of the functions below have **no caller
anywhere in this repository**: `checkpoint_refusals` and `may_resume` are the write boundary
and the resume boundary of a saver that does not exist, and saying so is the point rather
than a caveat on it. This repository's most common defect is a mechanism that is correct,
tested, documented and invoked from nowhere, and the way it survives review is by being
described in a sentence that sounds like it is wired.

What is wired today: `brain.ops.worker.preflight` validates a checkpointer URL before the
worker starts, so an install pointed at the pooler is refused at the door rather than
discovered by a resume that never resumes, and it now also asks `channel_policy_gaps`, so a
declared allowlist that has drifted into holding a content channel stops a container instead
of being found by reading the constant.

Deliberately absent: a retention rule. It is a real requirement of a checkpoint store, it is
its own WBS leaf, and writing it here without the thing it acts on would be a mechanism with
nothing to call it. The resume-time entitlement re-check is no longer on this list, and it is
worth saying why rather than quietly moving it: it turned out not to be a step at all.
Carrying references means the state cannot be reconstituted without going back through the
gate, so the re-check is what a resume *is*, and the thing that had to be built was the
refusal that keeps the state to references.

What this serves is the leaf named in the paragraph above, and it is deliberately not
claimed. The id is not repeated on the line below, because that line is parsed for ids and
a sentence saying a leaf is not claimed reads to the parser exactly like claiming it.

Task ids: none
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from brain.db import SCHEMAS
from brain.ops.queue import MAX_ARGUMENT_CHARS, pooler_url_findings

# ------------------------------------------------------------------ written-down reasons
#: Why the saved state is not filed with traces, and not with memory either.
A_CHECKPOINT_IS_A_PAYLOAD_NOT_A_TRACE = (
    "A checkpoint is the run so far: the question, the passages retrieved for it, the tool "
    "results, and whatever the graph has decided. That is business data with the caller's "
    "permissions no longer attached to it, which is exactly what brain.ops.tracing keeps out "
    "of spans and what brain.ops.queue.Job keeps out of queue arguments. So it cannot go in "
    "obs, whose whole retention argument is that it holds metadata and no payloads, and it "
    "cannot go in mem, which holds what the system learnt rather than what happened, and it "
    "is not a transcript so it is not chat. agent holds the artifacts of an agent run, and a "
    "checkpoint is one."
)

#: Why no migration creates these tables, and what the schema choice buys instead.
THE_CHECKPOINT_TABLES_ARE_NOT_OURS = (
    "The saver creates its own tables on first use and versions their shape with the "
    "library. Alembic therefore does not own them: there is no revision that creates them, "
    "no downgrade that drops them, and no CREATE we can hang an ENABLE ROW LEVEL SECURITY "
    "off. Nor can a migration enable it afterwards, because migrations run at startup and "
    "the tables do not exist until the first run. What is left is where they land. "
    "brain.ops.sweeps.sweep_rls enumerates brain.db.SCHEMAS and nothing else, so tables in a "
    "named schema are reported as having no row-level security and tables in public are not "
    "reported at all. The choice is between a check that fails until somebody acts and no "
    "check at all, and this repository has already decided that one: DENIED and ABSENT must "
    "be distinguishable to an operator even when they are not to a caller."
)

# ------------------------------------------------------------------------ the placement
#: The schema the saver's tables must be created in. See the two constants above.
CHECKPOINT_SCHEMA: Final = "agent"


def search_path_option(schema: str = CHECKPOINT_SCHEMA) -> str:
    """The libpq `options` string that puts the saver's tables in `schema`.

    A `search_path` on the connection rather than a schema argument, because the saver's DDL
    does not take one: it creates unqualified tables, and an unqualified table lands in the
    first schema on the path.

    **`public` is left off the path entirely rather than appended to ours.** A path of
    `agent,public` creates new tables in `agent` and finds existing ones in `public`, so an
    install that has already run once with the default keeps reading the tables nothing
    enumerates and the fix silently does nothing. Leaving it off means such an install fails
    on a missing table, which is a sentence somebody can act on.
    """
    return f"-c search_path={schema}"


class CheckpointerError(Exception):
    """Raised when a checkpointer is described in a way that cannot be deployed."""


@dataclass(frozen=True)
class CheckpointerConfig:
    """Everything the saver is constructed from, and nothing it could be misconfigured by.

    `prepare_threshold` and `pipeline` are fields rather than constants so that a
    deployment can be *read* and found wrong. They are not settings anybody should change:
    both are refused at their unsafe value in `__post_init__`, and the reason each is unsafe
    is different, which is why there are two of them rather than one "safe mode" flag.
    """

    url: str
    schema: str = CHECKPOINT_SCHEMA
    #: psycopg's server-side prepare threshold. None means never prepare. Kept as an
    #: explicit field because `brain.session` sets exactly this on the application engine
    #: and a checkpointer that quietly did not would be the same bug in a second place.
    prepare_threshold: int | None = None
    #: psycopg pipeline mode. Off: it batches statements onto one connection and depends on
    #: the backend staying the same across them, which is the assumption a pooler breaks.
    pipeline: bool = False

    def __post_init__(self) -> None:
        if not self.url.strip():
            msg = (
                "the checkpointer has no connection string; a saver with nowhere to save "
                "resumes nothing and says so only when a run is already lost"
            )
            raise CheckpointerError(msg)
        if self.schema not in SCHEMAS:
            msg = (
                f"schema {self.schema!r} is not one brain.db.SCHEMAS names, so "
                "brain.ops.sweeps.sweep_rls does not look in it and a checkpoint table with "
                f"no row-level security there is never reported. Known: {sorted(SCHEMAS)}"
            )
            raise CheckpointerError(msg)
        if self.prepare_threshold is not None:
            msg = (
                "the checkpointer prepares statements server-side, which fails on any "
                "connection that may be handed a different backend between statements; "
                "brain.session sets prepare_threshold=None for the same reason"
            )
            raise CheckpointerError(msg)
        if self.pipeline:
            msg = (
                "pipeline mode batches statements onto one connection and assumes the "
                "backend does not change under it, which is the assumption transaction "
                "pooling exists to break"
            )
            raise CheckpointerError(msg)

    @property
    def connect_options(self) -> str:
        """The libpq `options` string this configuration implies."""
        return search_path_option(self.schema)


def connection_refusals(url: str, *, app_url: str = "") -> tuple[str, ...]:
    """Every reason this connection string is wrong for a checkpointer.

    Returns all of them, matching `brain.ops.queue.queue_url_refusals` and
    `brain.config.check`: a misconfiguration found one variable at a time is a sequence of
    restarts.

    The pooler detection is `brain.ops.queue.pooler_url_findings` and is not repeated here.
    What this adds is the consequence, which is genuinely different: the queue's pooler
    failure is a LISTEN that stops delivering and raises nothing, and the checkpointer's is a
    prepared statement executed on a backend that never saw the prepare. The first is
    invisible, the second is an error in production and nowhere else.
    """
    findings: list[str] = []
    if app_url and url == app_url:
        findings.append(
            "the checkpointer is using the application's own connection string, which goes "
            "through the transaction pooler. The saver prepares its statements server-side "
            "and pipelines them, and a pooler in transaction mode hands the next statement "
            "to a backend that never saw the prepare. Give it a session-mode or direct URL."
        )
    return (*findings, *pooler_url_findings(url))


# ------------------------------------------------------------------ what may be saved
#: Why a checkpoint is refused rather than masked, which is the opposite of what a span gets.
A_MASKED_CHECKPOINT_IS_A_BROKEN_ONE = (
    "brain.ops.tracing masks payload_in and payload_out before a span leaves the process, "
    "and it can, because a trace was never wanted for its payload: the shape, the outcome "
    "and the latency survive masking and are the whole of what an operator needs. A "
    "checkpoint is the opposite. It exists to be read back, so masking it does not degrade "
    "it, it breaks it: a resume from a masked value either fails or continues from a string "
    "that reads like state, and the second is a wrong answer with a full audit trail behind "
    "it. So the boundary here refuses the write instead of shortening it, which is what "
    "brain.ops.queue.Job does to a job argument for the same reason."
)

#: Why re-resolving is right even though it can hand two attempts different reach.
ENTITLEMENT_IS_RESOLVED_AT_THE_ATTEMPT = (
    "E_run(caller, agent) = E(caller) intersect agent_ceiling is evaluated when the attempt "
    "runs, never when the job was enqueued, so two attempts of one job can see different "
    "things. Narrowing is the guarantee: revocation in this system is the deletion of a "
    "grant, and a checkpoint or a queue row carrying the earlier answer would be the one "
    "place a revocation does not take effect. Widening is the same rule read the other way "
    "and is harmless, because the caller is entitled to it now; the alternative is a stored "
    "copy of a permission decision outliving the decision. Neither is a step anybody "
    "performs at resume time: a checkpoint carries references, so the state cannot be "
    "reconstituted without going back through the gate, and going back through the gate is "
    "what resolves the reach."
)

#: The state channels a graph may write to the checkpoint store. Closed, and short on
#: purpose: everything not named is refused, so the cost of forgetting one is a graph that
#: cannot save until somebody adds it and argues for it, and the cost of adding a wrong one
#: is business data in a store with its own retention and no row-level security.
#:
#: Every entry is a reference or a decision. There is no entry for retrieved passages, tool
#: results or a draft answer, and that is the whole of the protection: those are the three
#: things a graph accumulates that are copies of what somebody was allowed to see.
PERSISTABLE_CHANNELS: Final[frozenset[str]] = frozenset(
    {
        #: Which run this is. The trace reference, not the trace.
        "run_id",
        #: Who it is for, as an identifier. `may_resume` is the only thing that reads it.
        "principal_id",
        #: Which agent is the lens. Its ceiling is looked up, never stored.
        "agent_id",
        #: Where the graph had got to. A node name is system vocabulary.
        "node",
        #: How many times round. An integer, and the reason a resume is not a loop.
        "step",
        #: What the caller asked, by reference. The question itself is a payload and is
        #: fetched through the gate like everything else.
        "question_id",
        #: Which records the run has decided it needs, as identifiers.
        "record_refs",
        #: Which tool it is part-way through calling, by registered name.
        "pending_tool",
    }
)

#: Channels whose names read as state and whose values are always a copy of somebody's data.
#: Not the mechanism, which is the allowlist above; this is what `channel_policy_gaps` checks
#: the allowlist against, so a name that ought never to be admissible cannot be added to it
#: quietly during a debugging session.
CONTENT_CHANNELS: Final[frozenset[str]] = frozenset(
    {"passages", "documents", "retrieved", "messages", "answer", "draft", "tool_results"}
)


def checkpoint_refusals(channels: Mapping[str, object]) -> tuple[str, ...]:
    """Every reason this state may not be written to the checkpoint store.

    Returns all of them rather than the first, matching `connection_refusals` and
    `brain.config.check`: a graph author who has put three payloads in their state should
    learn that once rather than three times.

    **This has no caller.** No saver is constructed anywhere in this repository, so nothing
    passes state through here today. It is written now because the rule is what makes the
    entitlement argument above true, and a boundary added after the first saver is a boundary
    added after the first checkpoint has been written.

    Order matters only in that the allowlist is asked first. A channel nobody declared is
    refused whatever its value, so a graph author who adds a working field gets the same
    answer for an empty one as for a full one, and does not learn that emptying it helps.
    """
    findings: list[str] = []
    for name, value in channels.items():
        if name not in PERSISTABLE_CHANNELS:
            findings.append(
                f"channel {name!r} is not one a checkpoint may hold, so it is refused "
                "whatever it contains; the saver's tables have their own retention and no "
                f"row-level security. Declared: {sorted(PERSISTABLE_CHANNELS)}"
            )
            continue
        if isinstance(value, (list, tuple, set, frozenset, dict)):
            findings.append(
                f"channel {name!r} holds a {type(value).__name__}, which is where somebody "
                "puts a record while meaning to put a summary; a checkpoint holds references "
                "and the passages a run retrieved are a list"
            )
            continue
        if isinstance(value, str) and len(value) > MAX_ARGUMENT_CHARS:
            findings.append(
                f"channel {name!r} is {len(value)} characters; over {MAX_ARGUMENT_CHARS} it "
                "is content rather than a reference, which is the bound "
                "brain.ops.queue.Job applies to a job argument and for the same reason"
            )
    return tuple(findings)


def channel_policy_gaps(channels: Iterable[str] | None = None) -> tuple[str, ...]:
    """Every way the declared allowlist has stopped being an allowlist.

    Two checks. An empty set means no graph can save anything, which presents as a
    checkpointer that is configured, connected and silently useless. And no declared channel
    may be one of `CONTENT_CHANNELS`, which is the check that has to exist because the
    allowlist is the whole protection: adding `passages` to it during a debugging session is
    one line, reads as making the saver work, and moves every retrieved row into the store.

    A parameter defaulting to the declared set, for the reason `brain.ops.queue.concurrency_gaps`
    takes one: a check that can only be run against the constant beside it cannot be shown to
    fail, and a check nobody has seen fail is a check nobody knows works.

    This one **is** called: `brain.ops.worker.preflight` asks it whenever a checkpointer URL
    is configured, so a drifted allowlist stops a container rather than waiting to be read.
    """
    declared = PERSISTABLE_CHANNELS if channels is None else frozenset(channels)
    findings: list[str] = []
    if not declared:
        findings.append(
            "no state channel is persistable, so a graph can save nothing and the "
            "checkpointer is configured, connected and unable to resume anything"
        )
    findings.extend(
        f"channel {name!r} is declared persistable and is a channel that holds a copy of "
        "what somebody was allowed to see; the allowlist is the whole of the protection"
        for name in sorted(declared & CONTENT_CHANNELS)
    )
    return tuple(findings)


@dataclass(frozen=True)
class CheckpointHeader:
    """Who a saved run belongs to. The only part of a checkpoint this module reads.

    Two fields and no entitlement set. Storing the reach the run was assembled under would
    make the row a permission decision that outlives the decision, and the resume would then
    have a choice about whether to believe it. See `ENTITLEMENT_IS_RESOLVED_AT_THE_ATTEMPT`:
    there is nothing to believe, because the state is references and reconstituting it goes
    through the gate.
    """

    run_id: str
    principal_id: str

    def __post_init__(self) -> None:
        for name in ("run_id", "principal_id"):
            if not str(getattr(self, name)).strip():
                msg = (
                    f"checkpoint header has no {name}; a saved run nobody owns is a run "
                    "anybody may resume"
                )
                raise CheckpointerError(msg)


def may_resume(header: CheckpointHeader, principal_id: str) -> bool:
    """Whether this caller may resume this run.

    Exact identity, and no fallback to entitlements. A checkpoint is not a bearer token and
    it is not a record either: the state inside it was assembled under one person's reach,
    and a second person with a wider reach still did not ask the question. A check that
    admitted anybody who could see the referenced records would let a manager resume a
    subordinate's half-finished run and receive an answer composed for somebody else.

    **This has no caller**, for the same reason `checkpoint_refusals` has none: there is no
    resume, because there is no graph.
    """
    return bool(principal_id.strip()) and header.principal_id == principal_id
