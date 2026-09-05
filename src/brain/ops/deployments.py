"""What went out, when, and which tasks it carried, in a chain that cannot be edited quietly.

**A separate chain from the permission ledger, and that is a decision rather than
convenience.** `brain.audit.ledger` holds who could see what: its `AuditAction` and
`SUBJECT_KINDS` are closed sets, the client-facing view in `brain.audit.view` derives a
capability from every subject kind, and the compliance export is built from all of it. Two
things follow from putting a deployment in there. A new subject kind means a new
`read:audit.*` capability, so anybody holding the wildcard starts seeing the release
history. And `AuditAction` gains a member for an event with no principal, no entitlement
and no scope, in a vocabulary whose whole purpose is to make "everything that happened to
this person" answerable.

`brain.audit.compliance` already met this boundary and refused to widen for the same
reason, in almost the same words. Deciding it differently here would leave the codebase
holding two answers to one question.

So: the same tamper-evident construction, its own domain separator, its own file. What is
given up is a single ordering across permission changes and deploys - an auditor wanting
"what changed between these two grants" has to merge two chains on timestamp. That is a
real cost and it is recorded on the Needs Rupash page rather than decided quietly, because
whether a client's audit view should contain the release history is his call, not a
detail.

**The JSONL written by the deploy watcher is the source, and the chain is derived.** The
watcher runs on the server with no database and appends one line per deploy, including the
ones where the application never came up - which are the lines worth reading. Reconciling
happens afterwards, from a process that can reach a database. A deployment present in the
file and absent from the chain is therefore an ordinary state during a deploy, and a
permanent one only if reconciliation is never run, which is why `unreconciled` exists as a
question anybody can ask rather than as an internal detail.

Task ids: M38.1.3.5
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

#: Domain separation, and a warning attached to it. This string is inside every digest, so
#: changing it invalidates every chain ever written. It is a migration, not an edit.
HASH_SCHEMA = "brain.deployment.v1"

GENESIS_HASH = "0" * 64

#: Where the watcher writes. Overridable because the tests need somewhere else and because
#: a second stack on the same host must not append to the first one's file.
DEFAULT_RECORD_PATH = Path("/var/lib/brain/deployments.jsonl")

#: What a deploy can have concluded. Closed for the same reason `AuditAction` is: an open
#: vocabulary means a new code path invents a string and nothing notices that no deployment
#: was ever counted as failed.
OUTCOMES = frozenset(
    {"deployed", "rolled_back", "failed_no_rollback", "rollback_failed", "refused"}
)


class DeploymentRecordError(Exception):
    """Raised when a line cannot be read as a deployment."""


def _digest(parts: Iterable[str]) -> str:
    """Length-prefixed, exactly as `brain.audit.ledger._digest` is, and for the same reason.

    Joining with a separator makes the digest ambiguous the moment a part can contain that
    separator: two different records then share a digest and one can be swapped for the
    other without the chain noticing. An image reference contains colons and slashes, so
    this is not hypothetical here.
    """
    joined = "".join(f"{len(part)}:{part}" for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Deployment:
    """One deploy, as the watcher recorded it.

    `task_ids` is a tuple rather than a string because it is compared between records, and
    the comparison must not depend on how the shell happened to join them.
    """

    at: datetime
    outcome: str
    commit: str
    image: str
    previous: str = ""
    task_ids: tuple[str, ...] = ()

    def fingerprint(self) -> str:
        """What identifies this deploy for the purpose of "have we recorded it already".

        Deliberately not the timestamp alone: the watcher runs on a cron and a retry writes
        a second line for the same image, seconds later. Deliberately not the image alone
        either, because the same image legitimately deploys twice - a rollback forward, a
        restart after a database fix - and collapsing those would hide the second one.
        """
        return _digest([HASH_SCHEMA, self.at.astimezone(UTC).isoformat(), self.outcome, self.image])

    @classmethod
    def from_line(cls, line: str) -> Deployment:
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            msg = f"deployment record is not JSON: {line[:80]!r}"
            raise DeploymentRecordError(msg) from exc
        if not isinstance(raw, dict):
            msg = f"deployment record is not an object: {line[:80]!r}"
            raise DeploymentRecordError(msg)

        outcome = str(raw.get("outcome", ""))
        if outcome not in OUTCOMES:
            # Refused rather than passed through. An unrecognised outcome in a report of
            # deploys reads as a new kind of success unless somebody looks it up.
            msg = f"unknown deployment outcome {outcome!r}; known: {sorted(OUTCOMES)}"
            raise DeploymentRecordError(msg)
        try:
            at = datetime.fromisoformat(str(raw.get("at", "")))
        except ValueError as exc:
            msg = f"deployment record has no usable timestamp: {raw.get('at')!r}"
            raise DeploymentRecordError(msg) from exc
        if at.tzinfo is None:
            msg = "deployment timestamp has no timezone; two servers would disagree about order"
            raise DeploymentRecordError(msg)

        ids = str(raw.get("task_ids", ""))
        return cls(
            at=at,
            outcome=outcome,
            commit=str(raw.get("commit", "")),
            image=str(raw.get("image", "")),
            previous=str(raw.get("previous", "")),
            task_ids=tuple(sorted(part for part in ids.split(",") if part)),
        )


@dataclass(frozen=True)
class ChainedDeployment:
    """A deployment with its place in the chain fixed.

    `entry_hash` is stored rather than recomputed on read, for the reason the audit ledger
    gives: a computed property follows the data, so an altered record produces an altered
    digest and agrees with itself forever. Storing it is what makes disagreement possible,
    and the disagreement is the detection.
    """

    seq: int
    deployment: Deployment
    prev_hash: str
    entry_hash: str


def chain_hash(seq: int, deployment: Deployment, prev_hash: str) -> str:
    """The digest for one link. The order of these parts is the schema; reordering them
    changes every chain ever written."""
    parts = [
        HASH_SCHEMA,
        prev_hash,
        str(seq),
        deployment.at.astimezone(UTC).isoformat(),
        deployment.outcome,
        deployment.commit,
        deployment.image,
        deployment.previous,
    ]
    # Sorted, so two records carrying the same ids in a different order digest identically.
    parts.extend(sorted(deployment.task_ids))
    return _digest(parts)


@dataclass
class DeploymentChain:
    """Deployments in order, each link naming the one before it."""

    entries: list[ChainedDeployment] = field(default_factory=list)

    def head(self) -> str:
        return self.entries[-1].entry_hash if self.entries else GENESIS_HASH

    def append(self, deployment: Deployment) -> ChainedDeployment:
        seq = self.entries[-1].seq + 1 if self.entries else 0
        prev = self.head()
        link = ChainedDeployment(
            seq=seq,
            deployment=deployment,
            prev_hash=prev,
            entry_hash=chain_hash(seq, deployment, prev),
        )
        self.entries.append(link)
        return link

    def verify(self) -> tuple[int, ...]:
        """Sequence numbers of every link that does not hold, empty if the chain is intact.

        Returns rather than raises, and reports every break rather than the first. A chain
        that refuses to load its own damaged records cannot tell anybody which record is
        damaged or what it says, which is the whole reason to have kept them.
        """
        broken: list[int] = []
        prev = GENESIS_HASH
        for i, link in enumerate(self.entries):
            recomputed = chain_hash(link.seq, link.deployment, link.prev_hash)
            if link.seq != i or link.prev_hash != prev or recomputed != link.entry_hash:
                broken.append(link.seq)
            prev = link.entry_hash
        return tuple(broken)


def read_records(path: Path | None = None) -> tuple[Deployment, ...]:
    """Every deployment the watcher has recorded, oldest first.

    A blank line is skipped; a malformed one raises. The file is appended to by a shell
    script that may be killed mid-write, so a trailing partial line is a real possibility -
    but it is also indistinguishable from tampering, and quietly dropping it would make the
    chain silently shorter than the record it derives from.
    """
    where = path or DEFAULT_RECORD_PATH
    if not where.exists():
        return ()
    out: list[Deployment] = []
    for line in where.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(Deployment.from_line(line))
    return tuple(out)


def unreconciled(records: Sequence[Deployment], chain: DeploymentChain) -> tuple[Deployment, ...]:
    """Deployments the watcher recorded that the chain has not taken in yet.

    The gap is expected during a deploy and permanent only if reconciliation never runs.
    Exposed as a question anybody can ask, so "the file says eleven and the chain says
    nine" is answerable without reading either by hand.
    """
    known = {link.deployment.fingerprint() for link in chain.entries}
    return tuple(r for r in records if r.fingerprint() not in known)


def reconcile(
    records: Sequence[Deployment], chain: DeploymentChain
) -> tuple[ChainedDeployment, ...]:
    """Append every unrecorded deployment, oldest first. Returns what was added.

    Ordered by the record's own timestamp rather than by file order. The file is almost
    always in order, and "almost always" is not a property a chain can be built on: a
    reconciliation run against two hosts' files concatenated would otherwise interleave
    them by accident of concatenation.
    """
    missing = sorted(unreconciled(records, chain), key=lambda d: d.at)
    return tuple(chain.append(d) for d in missing)
