"""The deployment chain: what it accepts, what it refuses, and what it detects.

Every test here is about a deployment history that would say something untrue.

Task ids: M38.1.3.5
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from brain.ops.deployments import (
    GENESIS_HASH,
    ChainedDeployment,
    Deployment,
    DeploymentChain,
    DeploymentRecordError,
    chain_hash,
    read_records,
    reconcile,
    unreconciled,
)

NOW = datetime(2026, 9, 5, 14, 0, tzinfo=UTC)


def _deployment(**overrides: object) -> Deployment:
    base: dict[str, object] = {
        "at": NOW,
        "outcome": "deployed",
        "commit": "a" * 40,
        "image": "ghcr.io/rpsj2230/verz-brain-v2.0@sha256:abc",
        "previous": "ghcr.io/rpsj2230/verz-brain-v2.0@sha256:def",
        "task_ids": ("M38.1.3.5",),
    }
    base.update(overrides)
    return Deployment(**base)  # type: ignore[arg-type]


def _line(**overrides: object) -> str:
    base: dict[str, object] = {
        "at": NOW.isoformat(),
        "outcome": "deployed",
        "commit": "a" * 40,
        "image": "img@sha256:abc",
        "previous": "img@sha256:def",
        "task_ids": "M38.1.3.5,M12.1.1",
    }
    base.update(overrides)
    return json.dumps(base)


# ----------------------------------------------------------------- reading a record
def test_a_record_carries_the_task_ids_it_deployed() -> None:
    """The question a deployment record is read to answer is "when did this start
    happening". A line carrying only a digest cannot answer it without the repository the
    server does not have."""
    record = Deployment.from_line(_line())
    assert record.task_ids == ("M12.1.1", "M38.1.3.5")
    assert record.outcome == "deployed"


def test_task_ids_are_sorted_so_two_records_can_be_compared() -> None:
    """The shell joins them in whatever order the manifest listed. A comparison that
    depended on that would report two identical deploys as different."""
    a = Deployment.from_line(_line(task_ids="M2,M1"))
    b = Deployment.from_line(_line(task_ids="M1,M2"))
    assert a.task_ids == b.task_ids


def test_a_deploy_that_recorded_no_ids_is_still_a_deploy() -> None:
    """An image built before the manifest existed, or built locally. Refusing it would make
    the bookkeeping more important than the deployment history."""
    assert Deployment.from_line(_line(task_ids="")).task_ids == ()


def test_an_unknown_outcome_is_refused_rather_than_passed_through() -> None:
    """An outcome nobody has defined reads as a new kind of success in a list of deploys,
    unless somebody stops to look it up. Deleting this lets a typo in the shell script
    become a deployment that appears to have worked."""
    with pytest.raises(DeploymentRecordError, match="unknown deployment outcome"):
        Deployment.from_line(_line(outcome="deployedd"))


def test_a_timestamp_without_a_timezone_is_refused() -> None:
    """Two servers writing local time put deploys in the wrong order, and order is the one
    thing a chain exists to fix."""
    with pytest.raises(DeploymentRecordError, match="timezone"):
        Deployment.from_line(_line(at="2026-09-05T14:00:00"))


def test_a_line_that_is_not_json_is_refused_not_skipped() -> None:
    """The file is appended to by a shell script that can be killed mid-write, so a partial
    line is real. It is also indistinguishable from tampering, and quietly dropping it makes
    the chain silently shorter than the record it derives from."""
    with pytest.raises(DeploymentRecordError):
        Deployment.from_line('{"at": "2026-09')


# ------------------------------------------------------------------- the chain
def test_a_chain_links_each_record_to_the_one_before() -> None:
    chain = DeploymentChain()
    first = chain.append(_deployment())
    second = chain.append(_deployment(at=NOW + timedelta(hours=1)))
    assert first.prev_hash == GENESIS_HASH
    assert second.prev_hash == first.entry_hash
    assert chain.verify() == ()


def test_an_edited_record_is_detected() -> None:
    """The point of the whole construction. A deployment history that can be edited after
    the fact is a history of what somebody wants to have happened."""
    chain = DeploymentChain()
    chain.append(_deployment())
    chain.append(_deployment(at=NOW + timedelta(hours=1), outcome="rolled_back"))
    # Somebody would rather the rollback had been a success.
    tampered = chain.entries[1]
    chain.entries[1] = ChainedDeployment(
        seq=tampered.seq,
        deployment=_deployment(at=NOW + timedelta(hours=1), outcome="deployed"),
        prev_hash=tampered.prev_hash,
        entry_hash=tampered.entry_hash,
    )
    assert chain.verify() == (1,)


def test_a_removed_record_is_detected() -> None:
    """Deleting the embarrassing one is easier than editing it, and leaves the remaining
    records individually valid."""
    chain = DeploymentChain()
    for i in range(3):
        chain.append(_deployment(at=NOW + timedelta(hours=i)))
    del chain.entries[1]
    assert chain.verify() != ()


def test_verify_reports_every_break_and_not_only_the_first() -> None:
    """A chain that refuses to load its own damaged records cannot say which record is
    damaged, which is the entire reason for having kept them."""
    chain = DeploymentChain()
    for i in range(4):
        chain.append(_deployment(at=NOW + timedelta(hours=i)))
    for i in (1, 3):
        e = chain.entries[i]
        chain.entries[i] = ChainedDeployment(
            seq=e.seq, deployment=e.deployment, prev_hash=e.prev_hash, entry_hash="0" * 64
        )
    assert len(chain.verify()) >= 2


def test_the_digest_cannot_be_confused_by_a_colon_in_an_image_name() -> None:
    """Length-prefixed rather than joined. An image reference is full of colons and slashes,
    so a separator-joined digest could be made ambiguous with a chosen tag name, and one
    record swapped for another without the chain noticing."""
    a = chain_hash(0, _deployment(image="img:a", commit="b" * 40), GENESIS_HASH)
    b = chain_hash(0, _deployment(image="img", commit="a:b" + "b" * 37), GENESIS_HASH)
    assert a != b


# ------------------------------------------------------------- reconciliation
def test_reconciling_takes_in_only_what_the_chain_lacks() -> None:
    """Run twice, it must not duplicate. Reconciliation happens on a schedule and a rerun
    after a failure is the normal case, not the exception."""
    records = [_deployment(at=NOW + timedelta(hours=i)) for i in range(3)]
    chain = DeploymentChain()
    assert len(reconcile(records, chain)) == 3
    assert reconcile(records, chain) == ()
    assert len(chain.entries) == 3


def test_the_same_image_deploying_twice_is_two_deployments() -> None:
    """A rollback forward, or a restart after a database fix. Collapsing them on the image
    would hide the second one, and the second one is usually the interesting one."""
    records = [_deployment(at=NOW), _deployment(at=NOW + timedelta(minutes=5))]
    chain = DeploymentChain()
    reconcile(records, chain)
    assert len(chain.entries) == 2


def test_records_are_taken_in_by_their_own_timestamp_not_by_file_order() -> None:
    """The file is almost always in order, and "almost always" is not something to build a
    chain on: two hosts' files concatenated would otherwise interleave by accident of
    concatenation."""
    late = _deployment(at=NOW + timedelta(hours=2))
    early = _deployment(at=NOW)
    chain = DeploymentChain()
    reconcile([late, early], chain)
    assert [link.deployment.at for link in chain.entries] == [early.at, late.at]


def test_the_gap_between_the_file_and_the_chain_is_a_question_anybody_can_ask() -> None:
    """Expected during a deploy, permanent only if reconciliation never runs. Without this,
    "the file says eleven and the chain says nine" needs somebody to read both by hand."""
    records = [_deployment(at=NOW + timedelta(hours=i)) for i in range(3)]
    chain = DeploymentChain()
    reconcile(records[:1], chain)
    assert len(unreconciled(records, chain)) == 2


def test_reading_a_file_the_watcher_wrote(tmp_path: Path) -> None:
    """Against the real format the shell script emits, so a change to either side that
    breaks the other fails here rather than on the server."""
    path = tmp_path / "deployments.jsonl"
    path.write_text(
        _line() + "\n" + _line(at=(NOW + timedelta(hours=1)).isoformat(), outcome="rolled_back"),
        encoding="utf-8",
    )
    records = read_records(path)
    assert [r.outcome for r in records] == ["deployed", "rolled_back"]


def test_no_file_yet_is_not_an_error(tmp_path: Path) -> None:
    """Before the first deploy there is no file. A reconciler that raised would fail every
    scheduled run on a fresh install."""
    assert read_records(tmp_path / "nothing.jsonl") == ()
