"""Fetching live, in parallel, inside a budget, and saying what could not be reached.

Federation is the tier that is never stored, so every question that touches it pays for it in
wall-clock time while somebody watches a spinner. That single fact shapes everything here.

**One critical path, not one call.** The earlier rule was "at most one live fetch per
question", and it was too rigid: a real question touches the CRM, the helpdesk and the ledger.
The correct formulation is that independent calls fan out concurrently and the budget covers
the *slowest*, not the sum. `FanOutPlan.critical_path_ms` is that arithmetic, and it is the
longest chain of dependent calls rather than the longest single call, because a call that
needs another call's answer cannot start until it has it.

**Parallelism must not be a way around the ceilings.** Xero is 5,000 a day per tenant, shared
with every other integration the client runs, and firing eight calls at once spends eight of
them. So `CallBudget` counts calls per question and per source, and the plan is checked before
anything runs rather than while it is running: a budget that refuses the fifth call has
already made four, and against a per-tenant daily ceiling those four are gone.

**A cache miss must not become a stampede.** Twenty agent runs asking the same question at the
same moment produce twenty identical fetches, which is how a connector that was comfortably
inside its ceiling produces a 429 on the one morning everybody arrives at once. `SingleFlight`
lets the first caller fetch and the rest wait for it.

Coalescing is only safe because of a property established elsewhere: a connector never
decides what a caller may see. Every follower receives the same unredacted rows the leader
fetched, and each is redacted separately against their own entitlement. If a connector
filtered by entitlement, sharing one fetch between two callers would hand the second caller
the first caller's permissions, and it would look exactly like a cache working well. See
`COALESCING_IS_SAFE_BECAUSE_CONNECTORS_DO_NOT_DECIDE`.

**Degradation names a source only when the asker could already see it.** "I could not reach
the finance ledger" tells somebody a finance ledger exists and that we are connected to it,
which is the disclosure `brain.core.redaction.ChannelPayload` suppresses `source` to prevent.
So `PartialAnswer.notice` takes the set of sources the caller's own catalogue already
disclosed, names those, and folds everything else into the constant message on
`brain.core.errors.Degraded`. The trace gets the full list, which is the same split the
redactor makes between a payload and a trace.

Scope: domain logic. Nothing here opens a connection or starts a thread. A plan is a value
somebody else executes; `now` is always a parameter.

Task ids: M11.5.1, M11.5.2, M11.5.3, M11.5.4, M11.5.5
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Final

from brain.core.errors import Degraded

# ------------------------------------------------------------------ written-down reasons
#: Why one fetch may serve several callers.
COALESCING_IS_SAFE_BECAUSE_CONNECTORS_DO_NOT_DECIDE = (
    "A connector returns everything it fetched and the redactor removes what is not covered, "
    "so one fetch's rows are the same rows for every caller and the difference between two "
    "callers appears afterwards, in the redactor, per caller. If a connector filtered by "
    "entitlement, a coalesced fetch would hand the follower the leader's permissions and it "
    "would look exactly like a cache working well. This is the property that makes "
    "single-flight a performance decision rather than a security one."
)

#: Why a plan is checked before anything runs.
THE_BUDGET_IS_CHECKED_BEFORE_THE_FIRST_CALL = (
    "A budget enforced call by call has already spent everything up to the refusal. Against "
    "Xero's 5,000 a day per tenant, shared with every other integration the client runs, "
    "those calls do not come back, and the question that was refused halfway has produced "
    "cost and no answer. The plan is known before it runs, so the arithmetic can be done "
    "first; the running check stays as well, for the calls a plan did not anticipate."
)

#: Why a degradation notice is not simply honest about which system was down.
NAMING_A_SOURCE_IS_A_DISCLOSURE = (
    "'I could not reach the finance ledger' says a finance ledger exists and that we are "
    "connected to it. Asked repeatedly with different phrasings it enumerates the company's "
    "systems for somebody entitled to none of them. So a notice names only the sources the "
    "asker's own catalogue already disclosed, and everything else becomes the same sentence "
    "an unreachable source has always produced. The trace keeps the full list, in the same "
    "split the redactor makes between a payload and a trace."
)


# ------------------------------------------------------------------------ the timeouts
#: The live fetch path's timeout, in milliseconds (M11.5.1).
#:
#: Eight hundred is a decision about the answer lane rather than about any source. The answer
#: lane targets under four seconds at p50, a question routinely touches two or three sources,
#: and a model call sits after them; a live fetch that spent two seconds would leave the model
#: nothing. It is also, deliberately, above the fast lane's entire p95 target of 500ms, which
#: is the arithmetic that says a federated fetch is never in the fast lane. That is not a
#: limitation of this module: it is why the projection exists.
FEDERATION_TIMEOUT_MS: Final = 800

#: The task lane's default, from the architecture's own table. Nobody is watching a spinner
#: there, so the constraint is the source's patience rather than a person's.
CONNECTOR_TIMEOUT_MS: Final = 5_000

#: The wall clock a fan-out may occupy on the answer path. Two federated timeouts: enough for
#: one dependent call after another, which is the deepest chain a question should need. A
#: plan deeper than that is a plan that should have been two questions.
FANOUT_BUDGET_MS: Final = FEDERATION_TIMEOUT_MS * 2


class FederationError(Exception):
    """A fan-out was described in a shape that cannot be run.

    Outside the user-facing taxonomy, for the reason `brain.core.redaction.UntypedShapeError`
    gives about its own: this is a bug in whoever assembled the plan, and it should fail where
    the plan is built rather than degrade an answer at request time. `Degraded` is what a
    person eventually sees, and it is raised by the executor, not here.
    """


# ------------------------------------------------------------------ the plan (M11.5.2)
@dataclass(frozen=True)
class SourceCall:
    """One call to one source, and what it has to wait for.

    `depends_on` is the only reason a plan is a graph rather than a list. The ordinary case is
    empty: the CRM, the helpdesk and the ledger know nothing about each other and all three
    start at once. The case that needs it is resolution, where a Freshdesk company id is what
    the Xero lookup takes as an argument, and pretending those two are independent produces a
    plan whose measured latency is right and whose second call cannot be made.
    """

    call_id: str
    connector: str
    entity: str
    timeout_ms: int = FEDERATION_TIMEOUT_MS
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.call_id.strip():
            msg = "a call needs an id; a plan addresses its calls by one"
            raise FederationError(msg)
        if self.timeout_ms <= 0:
            msg = f"call {self.call_id!r} has a non-positive timeout, which never completes"
            raise FederationError(msg)
        if self.call_id in self.depends_on:
            msg = f"call {self.call_id!r} depends on itself"
            raise FederationError(msg)


@dataclass(frozen=True)
class FanOutPlan:
    """Every source call one question needs, and what may run beside what.

    Validated on construction rather than on execution. A plan with a cycle in it deadlocks
    the executor, and a deadlock at request time is a person watching a spinner until a
    timeout somewhere else rescues them, which reports as a slow source rather than as the
    bug it is.
    """

    calls: tuple[SourceCall, ...]

    def __post_init__(self) -> None:
        if not self.calls:
            msg = "a fan-out plan with no calls is not a plan"
            raise FederationError(msg)
        by_id: dict[str, SourceCall] = {}
        for call in self.calls:
            if call.call_id in by_id:
                msg = f"two calls share the id {call.call_id!r}; a dependency on it is ambiguous"
                raise FederationError(msg)
            by_id[call.call_id] = call
        for call in self.calls:
            missing = sorted(set(call.depends_on) - set(by_id))
            if missing:
                msg = (
                    f"call {call.call_id!r} depends on {missing}, which are not in this plan; "
                    "an executor would either wait forever or start it too early"
                )
                raise FederationError(msg)
        self._assert_acyclic(by_id)

    @staticmethod
    def _assert_acyclic(by_id: Mapping[str, SourceCall]) -> None:
        """Depth-first with a colouring, so the error can name the cycle it found.

        Naming it matters more than finding it: a plan is assembled from an agent's tool
        graph, and "there is a cycle" sends somebody to read the whole graph while
        "a depends on b depends on a" is a one-line fix.
        """
        visiting: set[str] = set()
        done: set[str] = set()
        stack: list[str] = []

        def walk(call_id: str) -> None:
            if call_id in done:
                return
            if call_id in visiting:
                cycle = " depends on ".join([*stack[stack.index(call_id) :], call_id])
                msg = f"the plan has a cycle: {cycle}"
                raise FederationError(msg)
            visiting.add(call_id)
            stack.append(call_id)
            for parent in by_id[call_id].depends_on:
                walk(parent)
            stack.pop()
            visiting.discard(call_id)
            done.add(call_id)

        for call_id in by_id:
            walk(call_id)

    def waves(self) -> tuple[tuple[str, ...], ...]:
        """Calls grouped into rounds, each round runnable concurrently.

        This is what makes "one critical path with parallel I/O" concrete: everything in one
        wave starts together, and the wave costs its slowest member rather than its sum. A
        plan of six independent calls is one wave, which is the shape most questions have.
        """
        by_id = {call.call_id: call for call in self.calls}
        placed: dict[str, int] = {}
        for call_id in by_id:
            self._depth(call_id, by_id, placed)
        depth = max(placed.values())
        return tuple(
            tuple(sorted(c for c, d in placed.items() if d == level)) for level in range(depth + 1)
        )

    def critical_path_ms(self) -> int:
        """The wall clock this plan needs: the longest chain, never the sum (M11.5.2).

        The sum is what a sequential executor would take and is the number a reviewer reaches
        for. Reporting it would make every multi-source question look impossible and push
        somebody to cut a source that costs nothing in parallel.
        """
        by_id = {call.call_id: call for call in self.calls}
        costs: dict[str, int] = {}

        def cost(call_id: str) -> int:
            if call_id in costs:
                return costs[call_id]
            call = by_id[call_id]
            upstream = max((cost(p) for p in call.depends_on), default=0)
            costs[call_id] = upstream + call.timeout_ms
            return costs[call_id]

        return max(cost(call_id) for call_id in by_id)

    def sequential_ms(self) -> int:
        """What this plan would cost run one at a time. Kept for the comparison, not for use.

        An operator asking why a question is slow wants both numbers: the two together say
        whether the latency is the source's or the plan's shape.
        """
        return sum(call.timeout_ms for call in self.calls)

    def assert_within(self, budget_ms: int = FANOUT_BUDGET_MS) -> None:
        """Refuse a plan the answer lane cannot afford, before anything is called."""
        critical = self.critical_path_ms()
        if critical > budget_ms:
            msg = (
                f"the plan's critical path is {critical}ms against a budget of {budget_ms}ms; "
                f"its longest dependent chain is {self._longest_chain()}. Independent calls "
                "cost nothing extra, so this is depth rather than breadth and the fix is "
                "fewer calls that wait on each other"
            )
            raise FederationError(msg)

    def _longest_chain(self) -> str:
        by_id = {call.call_id: call for call in self.calls}
        best: dict[str, tuple[int, tuple[str, ...]]] = {}

        def chain(call_id: str) -> tuple[int, tuple[str, ...]]:
            if call_id in best:
                return best[call_id]
            call = by_id[call_id]
            upstream = max(
                (chain(p) for p in call.depends_on), default=(0, ()), key=lambda pair: pair[0]
            )
            best[call_id] = (upstream[0] + call.timeout_ms, (*upstream[1], call_id))
            return best[call_id]

        winner = max((chain(call_id) for call_id in by_id), key=lambda pair: pair[0])
        return " then ".join(winner[1])

    @staticmethod
    def _depth(call_id: str, by_id: Mapping[str, SourceCall], placed: dict[str, int]) -> int:
        if call_id in placed:
            return placed[call_id]
        parents = by_id[call_id].depends_on
        depth = 0 if not parents else 1 + max(FanOutPlan._depth(p, by_id, placed) for p in parents)
        placed[call_id] = depth
        return depth

    def calls_by_source(self) -> Mapping[str, int]:
        counts: dict[str, int] = {}
        for call in self.calls:
            counts[call.connector] = counts.get(call.connector, 0) + 1
        return MappingProxyType(counts)


# ---------------------------------------------------------------- the budgets (M11.5.3)
#: How many source calls one question may make in total. Twenty is above any plan anybody
#: should write and below the point at which one question is a crawl: a realistic question
#: touches five to twenty records, and the entity registry is what turns those into a handful
#: of calls rather than one per record.
DEFAULT_TOTAL_CALLS = 20

#: How many calls one question may make to one source, absent a specific figure. Six, because
#: the per-source ceiling this protects is a shared tenant allowance rather than ours, and one
#: question taking more than six of anybody's calls is a question that wanted a projection.
DEFAULT_PER_SOURCE_CALLS = 6


@dataclass(frozen=True)
class CallBudget:
    """What one question may spend, globally and per source.

    Two limits, and neither subsumes the other, for the reason `brain.ops.limits` gives about
    its own pair. The global one stops a question fanning out across every connector at once;
    the per-source one stops a question taking a whole tenant's minute from one of them. The
    difference from `ops.limits` is the subject: that module limits a *caller over time*, and
    this limits *one question*. A caller within their rate can still assemble a single
    question that makes forty calls, and nothing in a sliding window would notice.
    """

    total: int = DEFAULT_TOTAL_CALLS
    per_source: Mapping[str, int] = MappingProxyType({})
    default_per_source: int = DEFAULT_PER_SOURCE_CALLS

    def limit_for(self, connector: str) -> int:
        return self.per_source.get(connector, self.default_per_source)

    def check(self, plan: FanOutPlan) -> tuple[str, ...]:
        """Every way this plan exceeds the budget, reported together.

        All of them rather than the first, for the reason `check_projection` gives: one at a
        time turns assembling a plan into a guessing game where each fix reveals the next
        objection.
        """
        problems: list[str] = []
        if len(plan.calls) > self.total:
            problems.append(
                f"{len(plan.calls)} calls against a budget of {self.total} for one question"
            )
        for connector, count in sorted(plan.calls_by_source().items()):
            allowed = self.limit_for(connector)
            if count > allowed:
                problems.append(
                    f"{count} calls to {connector} against a per-source budget of {allowed}"
                )
        return tuple(problems)

    def assert_affordable(self, plan: FanOutPlan) -> None:
        """Refuse the plan before the first call is made.

        See `THE_BUDGET_IS_CHECKED_BEFORE_THE_FIRST_CALL`.
        """
        problems = self.check(plan)
        if problems:
            listed = "; ".join(problems)
            msg = f"this question cannot be afforded: {listed}"
            raise FederationError(msg)


@dataclass(frozen=True)
class BudgetState:
    """What one question has spent so far. Immutable, like every state machine here.

    Kept alongside the pre-flight check rather than instead of it: a plan is what was
    intended, and this is what happened, and the two differ whenever a source paginates or a
    retry lands. The running check is the one that catches a loop nobody planned.
    """

    spent_total: int = 0
    spent_by_source: Mapping[str, int] = MappingProxyType({})

    def spend(self, connector: str, *, budget: CallBudget) -> BudgetState:
        """Record one call, or refuse it. Refusing raises rather than returning a flag.

        A flag would be checked by the caller that remembered to, and a call made past an
        exhausted budget is a call that has already spent somebody's tenant allowance. The
        exception is `FederationError` rather than `Degraded`: the person's answer being
        partial is a decision for whoever assembles it, and this only says that this call is
        not available.
        """
        source_spent = self.spent_by_source.get(connector, 0)
        if self.spent_total + 1 > budget.total:
            msg = (
                f"this question has already made {self.spent_total} source calls, which is "
                f"its whole budget of {budget.total}"
            )
            raise FederationError(msg)
        if source_spent + 1 > budget.limit_for(connector):
            msg = (
                f"this question has already made {source_spent} calls to {connector}, which "
                f"is its budget of {budget.limit_for(connector)} for that source"
            )
            raise FederationError(msg)
        updated = dict(self.spent_by_source)
        updated[connector] = source_spent + 1
        return BudgetState(
            spent_total=self.spent_total + 1, spent_by_source=MappingProxyType(updated)
        )


# ------------------------------------------------------- thundering herd (M11.5.4)
class FlightRole(enum.StrEnum):
    """Whether this caller fetches, or waits for the caller that is already fetching."""

    LEADER = "leader"
    FOLLOWER = "follower"


#: How long a claim is honoured before another caller may take it. A leader that crashed
#: holds nothing after this: without an expiry, one crash makes every later question for that
#: key a permanent follower waiting on a fetch nobody is performing, and the symptom is a
#: connector that reads as healthy while every question about it hangs.
FLIGHT_TTL = timedelta(seconds=10)


def flight_key(connector: str, entity: str, filters: tuple[tuple[str, str], ...]) -> str:
    """What makes two fetches the same fetch.

    The caller's identity is deliberately absent. Two people asking the same question of the
    same source produce the same rows, because the connector does not know who is asking;
    what differs is what each of them may see, and that is decided afterwards by the
    redactor. Putting an `ent_hash` in this key would make the coalescing useless in exactly
    the case it is for, which is twenty people asking one thing at nine in the morning. See
    `COALESCING_IS_SAFE_BECAUSE_CONNECTORS_DO_NOT_DECIDE`.
    """
    rendered = ",".join(f"{k}={v}" for k, v in sorted(filters))
    return f"{connector}:{entity}:{rendered}"


@dataclass
class SingleFlight:
    """Who is currently fetching what, so twenty callers make one call.

    An instance rather than a module singleton, matching every other registry in this
    codebase. In production the claims live in Valkey with the same TTL; nothing here knows
    that, because a coalescer holding a client could not be tested for the expiry case, which
    is the only part of this that is ever wrong.
    """

    _claims: dict[str, datetime] = field(default_factory=dict)

    def claim(self, key: str, *, now: datetime, ttl: timedelta = FLIGHT_TTL) -> FlightRole:
        """Take the fetch, or find that somebody else already has it.

        An expired claim is taken over rather than merely reported, so the recovery path is
        the ordinary path: the next caller after a crash becomes the leader and the question
        is answered, late, instead of hanging.
        """
        held = self._claims.get(key)
        if held is not None and now - held < ttl:
            return FlightRole.FOLLOWER
        self._claims[key] = now
        return FlightRole.LEADER

    def release(self, key: str) -> None:
        """Give the claim back. Safe to call for a key that is not held.

        Tolerating a release nobody made is deliberate: the release belongs in a `finally`
        beside the fetch, and a `finally` that can raise replaces the real exception with its
        own, which is the argument `brain.ops.secrets._revoke_quietly` makes at length.
        """
        self._claims.pop(key, None)

    def in_flight(self, *, now: datetime, ttl: timedelta = FLIGHT_TTL) -> tuple[str, ...]:
        return tuple(sorted(k for k, held in self._claims.items() if now - held < ttl))


# ------------------------------------------------------ graceful degradation (M11.5.5)
class FailureReason(enum.StrEnum):
    """Why one source contributed nothing. Recorded in the trace, rarely shown to a person.

    Closed, because the reasons differ in who should act. A timeout is the source being slow;
    a quota is us having asked too much; a truncation is the source having answered fully and
    incompletely at once, which is the Freshdesk 300-record case and the one that looks like
    success in every test.
    """

    TIMEOUT = "timeout"
    CIRCUIT_OPEN = "circuit_open"
    QUOTA = "quota"
    TRANSPORT = "transport"
    TRUNCATED = "truncated"
    NOT_SERVING = "not_serving"


@dataclass(frozen=True)
class SourceFailure:
    """One source that could not be fetched, and why."""

    connector: str
    reason: FailureReason
    detail: str = ""


@dataclass(frozen=True)
class PartialAnswer:
    """What came back, what did not, and the two audiences for that.

    The split is the same one `brain.core.redaction.RedactedAnswer` makes: `trace_lines` is
    for an auditor and names everything, while `notice` is for the asker and names only what
    they were already entitled to know exists. Two methods rather than one with a flag,
    because a flag defaults to whatever the first caller needed.
    """

    fetched: tuple[str, ...] = ()
    failed: tuple[SourceFailure, ...] = ()

    @property
    def is_complete(self) -> bool:
        return not self.failed

    def notice(self, *, disclosable: frozenset[str]) -> str:
        """What the asker is told. Names only sources their catalogue already disclosed.

        The fallback is `Degraded.public_message` verbatim rather than a sentence of this
        module's own, so a source the asker may not know about produces exactly the message an
        unreachable source has always produced, and the two are indistinguishable. See
        `NAMING_A_SOURCE_IS_A_DISCLOSURE`.

        A source is named only when it is *both* in `disclosable` and failed, which means the
        notice never announces a connection the asker could not already see. It says nothing
        at all when everything succeeded: a reassurance that all sources answered is a list of
        the sources by another route, offered on every request.
        """
        if not self.failed:
            return ""
        nameable = tuple(sorted({f.connector for f in self.failed} & disclosable))
        if not nameable:
            return Degraded.public_message
        listed = ", ".join(nameable)
        return f"I could not reach {listed}, so this answer is missing whatever it holds."

    def trace_lines(self) -> tuple[str, ...]:
        """The full list, for the trace. Names every source and every reason.

        Safe here for the reason `RedactionTrace` is safe: it is read by an auditor rather
        than by the asker, and nothing in this module can put it into a `ChannelPayload`,
        which has no field that could carry it.
        """
        return tuple(
            f"{f.connector}: {f.reason}" + (f" ({f.detail})" if f.detail else "")
            for f in sorted(self.failed, key=lambda f: (f.connector, f.reason))
        )
