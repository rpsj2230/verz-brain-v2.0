"""The object store's deployment, held equal to the policy it is supposed to enforce.

`brain.ops.storage` declares four buckets, what each holds, how long it keeps things and
whether it is versioned. Until the compose file existed that declaration described nothing:
a retention policy no store has ever been told is a policy in the same sense that an unread
runbook is a procedure.

Now there are three copies of the same decision - the Python constant, the provisioning
script, and the compose file - and copies are only safe while something compares them. That
is what this file is. It is the same argument `brain.ops.wiring` makes about memory limits,
and the same one that caught a drifted limit in the trace ledger's compose.

**The lifetimes matter in one direction more than the other.** A TTL that is too short
deletes a client's compliance export before the window they were promised; a TTL that is
missing keeps browser recordings of staff sessions for ever. Neither is visible from the
application, because the deletion happens inside the store on its own schedule.

Task ids: M32.3.1.1
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from brain.ops.storage import BUCKETS
from brain.ops.wiring import COMPONENTS

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "docker-compose.objectstore.yml"
PROVISION = REPO / "ops" / "seaweedfs" / "provision.sh"
S3_CONFIG = REPO / "ops" / "seaweedfs" / "s3.json"

#: `create <name> "<ttl>"` in the provisioning script. The quotes are required by the
#: pattern so an unquoted argument, which the shell would split, cannot match.
CREATE_RE = re.compile(r'^create\s+(\S+)\s+"([^"]*)"\s*$', re.MULTILINE)


def _compose() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    return loaded


def _provisioned() -> dict[str, str]:
    """Bucket name to the TTL string the script sets, empty meaning no expiry."""
    return {
        m.group(1): m.group(2) for m in CREATE_RE.finditer(PROVISION.read_text(encoding="utf-8"))
    }


# --------------------------------------------------------------- the buckets exist
def test_every_declared_bucket_is_actually_created() -> None:
    """A bucket that `brain.ops.storage` describes and nothing creates is a write that fails
    at runtime, in whichever code path first tries to store that kind of object.

    Delete this and a fifth bucket can be added to `BUCKETS` with a retention argued in its
    docstring and no store anywhere that has one."""
    declared = {b.name for b in BUCKETS}

    assert set(_provisioned()) == declared


def test_no_bucket_is_created_that_the_policy_does_not_describe() -> None:
    """The other direction, and the one that matters more. A bucket created here and absent
    from `BUCKETS` has no declared retention, no stated reason, and nothing reconciling it,
    so whatever lands in it is kept for ever by default and nobody knows it is there."""
    extra = sorted(set(_provisioned()) - {b.name for b in BUCKETS})

    assert not extra, f"created but not declared in BUCKETS: {extra}"


# --------------------------------------------------------------- the lifetimes agree
@pytest.mark.parametrize("bucket", BUCKETS, ids=lambda b: str(b.name))
def test_each_bucket_is_created_with_the_lifetime_the_policy_declares(bucket: Any) -> None:
    """**The whole point of the file.** A TTL that is too short deletes a client's
    compliance export inside the window they were promised; one that is missing keeps
    browser recordings of staff sessions for ever. Neither is visible from the application,
    because the store deletes on its own schedule and reports nothing.

    Parametrised per bucket so a failure names which lifetime drifted rather than reporting
    that a dictionary comparison failed.

    Delete this and the numbers can be edited in either place, and the two files keep
    describing different systems while both look reviewed."""
    ttl = _provisioned()[bucket.name]

    if bucket.retention_days is None:
        assert ttl == "", (
            f"{bucket.name} is declared to have no expiry, and the reason is recorded in "
            f"BUCKETS, but the store is told to delete after {ttl!r}"
        )
    else:
        assert ttl == f"{bucket.retention_days}d", (
            f"{bucket.name} keeps {bucket.retention_days} days by policy and {ttl!r} in the store"
        )


def test_a_bucket_with_no_expiry_is_a_stated_decision_and_not_a_blank() -> None:
    """`assets` has no TTL, and the guard is that `BUCKETS` refuses a bucket whose retention
    has no reason. So an unbounded lifetime here is always one somebody argued for.

    Delete this and the provisioning script's empty TTL stops being distinguishable from a
    value somebody forgot to fill in."""
    unbounded = [b for b in BUCKETS if b.retention_days is None]

    assert unbounded, "the case this test exists for has disappeared from BUCKETS"
    for bucket in unbounded:
        assert bucket.retention_reason.strip()


# --------------------------------------------------------------- the deployment
def test_the_object_store_is_sized_to_the_component_it_is_budgeted_as() -> None:
    """`brain.ops.wiring` decides what `seaweedfs` may take and every profile's arithmetic is
    computed from it. A compose file that gives it more is memory no budget accounted for, on
    a host `wiring` records as already overcommitted."""
    declared = {c.name: c.memory_mib for c in COMPONENTS}
    limit = _compose()["services"]["seaweedfs"]["deploy"]["resources"]["limits"]["memory"]

    assert int(str(limit).rstrip("M")) == declared["seaweedfs"]


def test_the_gateway_is_not_published_to_the_host() -> None:
    """The S3 gateway holds every asset, export and backup this system has, and the host has
    neighbours and no firewall in front of the container network. `expose` publishes to the
    compose network; `ports` publishes to the world.

    Delete this and a debugging session adds a port mapping that nobody removes."""
    seaweed = _compose()["services"]["seaweedfs"]

    assert "ports" not in seaweed, "the object store must not be reachable from the host"
    assert seaweed.get("expose") == ["8333"]


def test_the_access_control_file_is_mounted_read_only() -> None:
    """A process that can rewrite its own access control has none. Read-only is one flag and
    it is the difference between a compromised gateway that can serve objects and one that
    can grant itself the right to serve them to anybody."""
    mounts = _compose()["services"]["seaweedfs"]["volumes"]

    s3 = [m for m in mounts if "s3.json" in str(m)]
    assert s3, "the identities file is not mounted"
    assert all(str(m).endswith(":ro") for m in s3), f"s3.json is mounted writable: {s3}"


def test_the_process_is_told_its_own_ceiling_below_the_cgroup_limit() -> None:
    """A cgroup limit is enforced by killing the process, so it protects the neighbour rather
    than this service. `GOMEMLIMIT` is what makes the collector work harder as the heap
    approaches the ceiling instead of the kernel ending it and the index coming back cold.

    Strictly below, because `GOMEMLIMIT` does not count goroutine stacks, the binary or the
    mmap'd index, and the cgroup counts all three."""
    seaweed = _compose()["services"]["seaweedfs"]
    ceiling = int(str(seaweed["environment"]["GOMEMLIMIT"]).removesuffix("MiB"))
    cgroup = int(str(seaweed["deploy"]["resources"]["limits"]["memory"]).rstrip("M"))

    assert ceiling < cgroup


def test_the_provisioning_container_runs_once_and_is_not_restarted() -> None:
    """It creates buckets and exits. `restart: unless-stopped` would run it again on every
    daemon restart, and while the script is idempotent, a one-shot that keeps coming back is
    indistinguishable from one that keeps failing.

    Delete this and the init service acquires a restart policy copied from the service above
    it, which is the most natural edit anybody could make to this file."""
    init = _compose()["services"]["seaweedfs-init"]

    assert init["restart"] == "no"
    assert init["depends_on"]["seaweedfs"]["condition"] == "service_healthy"


# --------------------------------------------------------------- what is not granted
def test_no_bucket_is_declared_publicly_readable() -> None:
    """`public_read` exists as a field so there is something to check. Every bucket here
    holds either a client's data or a recording of a member of staff working."""
    public = [b.name for b in BUCKETS if b.public_read]

    assert not public, f"these buckets are declared public: {public}"


def test_the_s3_identities_file_grants_no_anonymous_access_and_no_admin() -> None:
    """Two absences, each doing separate work.

    SeaweedFS grants public access by naming an identity called `anonymous`. There is none,
    so there is nothing to set to true by accident and nothing a reviewer can misread as
    already disabled.

    And the application's identity holds Read, Write and List but never Admin. It stores and
    fetches objects; it never creates or deletes a bucket, because how many buckets exist is
    a decision in `brain.ops.storage` rather than something a running process makes. A
    compromised application key can corrupt objects, which is what versioning on `backups`
    is for, and cannot delete the bucket they live in."""
    import json

    config = json.loads(S3_CONFIG.read_text(encoding="utf-8"))
    names = {i["name"] for i in config["identities"]}

    assert "anonymous" not in names, "an anonymous identity makes buckets publicly readable"
    for identity in config["identities"]:
        assert "Admin" not in identity["actions"], f"{identity['name']} holds Admin"


def test_the_checked_in_credential_is_obviously_not_a_real_one() -> None:
    """A literal key in a repository is a key that has leaked. M32.3.1.4 issues these from
    OpenBao at deploy time, and the placeholder is written so that nothing could mistake it
    for a working value or ship it by accident."""
    import json

    config = json.loads(S3_CONFIG.read_text(encoding="utf-8"))
    for identity in config["identities"]:
        for credential in identity["credentials"]:
            assert "REPLACE_AT_DEPLOY" in credential["accessKey"]
            assert "REPLACE_AT_DEPLOY" in credential["secretKey"]
