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

**The saver's tables are not ours, and `CHECKPOINT_SCHEMA` is the whole of what we can do
about it.** See `THE_CHECKPOINT_TABLES_ARE_NOT_OURS`. The library creates them itself, so
Alembic does not own them, they arrive with no row-level security, and no migration can
enable it on a table that does not exist yet. What the schema choice buys is that
`brain.ops.sweeps.sweep_rls` reads `brain.db.SCHEMAS` and therefore looks in `agent`: the
tables land somewhere that check can see, and the gap becomes a red sweep instead of an
absence. The default lands them in `public`, which that sweep does not enumerate.

**What does not exist.** `langgraph` is not in `uv.lock` and nothing here builds a graph:
`src/brain/agents/` is a docstring and no code. So no saver is constructed
from this configuration, no checkpoint has ever been written, and M32.4.1.2 is not claimed.
What is real today is that `brain.ops.worker.preflight` validates a checkpointer URL before
the worker starts, so an install that has been pointed at the pooler is refused at the door
rather than discovered by a resume that never resumes. `brain.knowledge.uploads` states the
same kind of gap the same way: the four lines that do not exist are named rather than
implied.

Deliberately absent: a retention rule and a resume-time entitlement re-check. Both are real
requirements of a checkpoint store, both are their own WBS leaves, and writing them here
without the thing they act on would be a mechanism with nothing to call it. A checkpoint
holds state assembled under an entitlement resolved at the time the run started, which is
the argument for both, and it belongs beside the resume that has to make the check.

Task ids: none. M32.4.1.2 is what this serves and is deliberately not claimed; see the
paragraph above.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from brain.db import SCHEMAS
from brain.ops.queue import pooler_url_findings

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
