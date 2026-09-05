"""Buckets, lifecycles, backends and where their credentials come from.

Every test here is about a file that outlives the permission that put it there, or about a
backend configuration whose failure looks like something else.

Task ids: M32.3.1.2, M32.3.1.3, M32.3.1.4, M32.3.2.1, M32.3.2.2
"""

from __future__ import annotations

import pytest

from brain.ops.openbao import DYNAMIC_MOUNTS, STATIC_PREFIX, assert_static_path
from brain.ops.storage import (
    BUCKETS,
    R2_REGION,
    Addressing,
    Backend,
    Bucket,
    ObjectKind,
    StorageBackend,
    StorageError,
    bucket,
    bucket_for,
    config_for,
    credential_path,
    lifecycle_gaps,
    static_credential_backends,
)


def _a_bucket(**overrides: object) -> Bucket:
    base: dict[str, object] = {
        "name": "probe",
        "holds": "nothing",
        "retention_days": 7,
        "retention_reason": "a week is long enough to look at it",
        "versioned": False,
    }
    base.update(overrides)
    return Bucket(**base)  # type: ignore[arg-type]


# --------------------------------------------------- the bucket layout (M32.3.1.2)
def test_the_four_buckets_are_the_ones_the_architecture_names() -> None:
    """The set is closed so that a fifth bucket is a decision rather than a call to
    `create_bucket` somewhere. Delete this and the layout drifts one convenience at a
    time."""
    assert [b.name for b in BUCKETS] == ["assets", "recordings", "exports", "backups"]


@pytest.mark.parametrize("kind", list(ObjectKind))
def test_every_kind_of_object_has_a_bucket_decided_for_it(kind: ObjectKind) -> None:
    """`bucket_for` matches exhaustively, so a new `ObjectKind` without a home is a type
    error. The default it replaces is the one everybody writes - everything into `assets`,
    because that is the bucket that existed - which gives a compliance export the lifecycle
    of a logo. Delete this and the exhaustiveness is never exercised."""
    assert bucket_for(kind) in BUCKETS


def test_a_database_dump_goes_somewhere_versioned() -> None:
    """The pairing that matters. A dump written into an unversioned bucket by a nightly job
    that has started producing corrupt output has destroyed the good copy, and the retention
    window is irrelevant. Delete this and the mapping can be changed with no other test
    failing."""
    assert bucket_for(ObjectKind.DATABASE_DUMP).versioned


def test_an_unknown_bucket_name_is_refused_rather_than_created() -> None:
    """A typo would otherwise become a new bucket the first time something wrote to it, with
    no lifecycle rule and no reason on file. Delete this and the closed set stops being
    closed at the only place it is read."""
    with pytest.raises(StorageError, match="unknown bucket"):
        bucket("assets ")


# --------------------------------------------------- the lifecycle (M32.3.1.3)
def test_every_bucket_states_a_reason_for_its_retention() -> None:
    """A lifecycle rule nobody can explain is one that gets relaxed the first time somebody
    needs an old file, and the relaxation is permanent because nobody knows what the number
    was protecting. Delete this and `retention_reason` becomes an empty string in the next
    bucket added."""
    for b in BUCKETS:
        assert b.retention_reason.strip(), b.name
    with pytest.raises(StorageError, match="states no reason"):
        _a_bucket(retention_reason="")


def test_a_retention_of_zero_days_is_not_a_lifetime() -> None:
    """Zero is what a half-finished configuration holds, and rendered into a lifecycle rule
    it either deletes everything immediately or is ignored entirely, depending on the
    backend. Delete this and which of those happens is decided by the vendor."""
    with pytest.raises(StorageError, match="not a lifetime"):
        _a_bucket(retention_days=0)


def test_the_lifecycle_rules_hold_for_every_declared_bucket() -> None:
    """The three cross-bucket rules at once: nothing public, backups versioned, and the
    buckets holding copies of governed data actually expire. Delete this and each rule
    survives only as a comment beside the bucket it applies to."""
    assert lifecycle_gaps() == ()


def test_a_bucket_holding_copies_of_governed_data_may_not_be_kept_for_ever() -> None:
    """An export and a recording are flattened copies whose permissions stopped applying the
    moment they were written, so time is the only control left on them. Delete this and
    `retention_days=None` on `exports` passes review as caution."""
    findings = []
    for b in BUCKETS:
        if b.name in {"recordings", "exports"}:
            findings.append(b.retention_days)
    assert all(days is not None for days in findings), findings


def test_a_bucket_readable_without_credentials_is_reported() -> None:
    """A public S3 bucket is the most reproduced breach in this category, because the setting
    is one click and invisible from inside the application. The check is run here against a
    bucket that is public, which is the only way to know the check works: run only against
    the declared set, where nothing is public, it would pass with its body deleted. Delete
    this and `public_read` becomes a field nothing reads and somebody removes it."""
    assert all(not b.public_read for b in BUCKETS)
    findings = lifecycle_gaps([_a_bucket(public_read=True)])
    assert any("readable without credentials" in f for f in findings), findings


def test_an_unversioned_backup_bucket_is_reported() -> None:
    """The other rule that is invisible against the declared set. A nightly job that starts
    writing corrupt output destroys the good copy, and the retention window is then
    irrelevant. Delete this and the versioning rule can be deleted with `lifecycle_gaps()`
    still returning empty."""
    findings = lifecycle_gaps([_a_bucket(name="backups", versioned=False)])
    assert any("not versioned" in f for f in findings), findings


def test_a_bucket_of_governed_copies_kept_indefinitely_is_reported() -> None:
    """An export is a flattened copy whose permissions stopped applying the moment it was
    written, so time is the only control left on it. Delete this and `retention_days=None`
    on `exports` passes review as caution rather than as the removal of the last control."""
    forever = _a_bucket(
        name="exports", retention_days=None, retention_reason="kept until somebody deletes it"
    )
    assert any("kept indefinitely" in f for f in lifecycle_gaps([forever]))


# --------------------------------------------------- credentials (M32.3.1.4)
def test_every_credential_path_is_one_the_vault_module_would_accept() -> None:
    """Checked against `brain.ops.openbao`'s own constants rather than against a second copy
    of them, which is the lesson `brain.ops.sweeps` learned: a rule restated ends up looser
    than the thing it guards. Delete this and a path that OpenBao refuses is discovered
    during a deployment."""
    for backend in Backend:
        path = credential_path(backend)
        if path.startswith(DYNAMIC_MOUNTS):
            continue
        assert_static_path(path)


def test_the_backends_whose_keys_cannot_be_leased_are_named() -> None:
    """SeaweedFS is not an AWS account and R2 tokens are minted by Cloudflare's own API, so
    neither has an OpenBao engine and both are long-lived keys rotated by hand. That is a
    real gap and it is better queried than discovered during a rotation. Delete this and the
    gap is invisible."""
    assert set(static_credential_backends()) == {Backend.SEAWEEDFS, Backend.CLOUDFLARE_R2}
    assert credential_path(Backend.AWS_S3).startswith(DYNAMIC_MOUNTS)
    assert credential_path(Backend.SEAWEEDFS).startswith(STATIC_PREFIX)


def test_a_configuration_records_whether_its_credentials_are_leased() -> None:
    """A caller that cannot tell a lease from a stored key cannot know whether to revoke
    anything at the end of a run. Delete this and `brain.ops.secrets` gets handed a static
    key to revoke."""
    leased = config_for(
        Backend.AWS_S3, endpoint_url="https://s3.amazonaws.com", region="ap-southeast-1"
    )
    stored = config_for(Backend.SEAWEEDFS, endpoint_url="http://seaweedfs:8333")
    assert leased.dynamic_credentials
    assert not stored.dynamic_credentials


# --------------------------------------------------- the backends (M32.3.2.1)
def test_r2_refuses_a_region_that_looks_real() -> None:
    """The trap this function exists for. A real-looking region is accepted by the client
    library, changes the signature, and returns 403 - which reads as a bad credential and
    sends somebody to rotate a key that was never the problem. Delete this and that hour is
    spent every time a client moves to R2."""
    with pytest.raises(StorageError, match="one region"):
        config_for(
            Backend.CLOUDFLARE_R2,
            endpoint_url="https://acct.r2.cloudflarestorage.com",
            region="ap-southeast-1",
        )
    assert (
        config_for(
            Backend.CLOUDFLARE_R2, endpoint_url="https://acct.r2.cloudflarestorage.com"
        ).region
        == R2_REGION
    )


def test_aws_refuses_the_r2_region_name() -> None:
    """The same mistake in the other direction, made by whoever copies the R2 configuration
    when the client moves to AWS. Delete this and `auto` is sent to AWS, where it is a
    region that does not exist and a signature that does not verify."""
    with pytest.raises(StorageError, match="real region"):
        config_for(Backend.AWS_S3, endpoint_url="https://s3.amazonaws.com", region=R2_REGION)
    with pytest.raises(StorageError, match="real region"):
        config_for(Backend.AWS_S3, endpoint_url="https://s3.amazonaws.com")


def test_a_self_hosted_gateway_gets_path_style_addressing() -> None:
    """Virtual-host addressing puts the bucket in the hostname, which needs wildcard DNS and
    a certificate covering it. A self-hosted gateway has neither, and the failure is a
    name-resolution error that reads as a network problem. Delete this and every SeaweedFS
    install starts with that hour."""
    assert config_for(Backend.SEAWEEDFS, endpoint_url="http://seaweedfs:8333").addressing is (
        Addressing.PATH
    )
    assert (
        config_for(
            Backend.AWS_S3, endpoint_url="https://s3.amazonaws.com", region="ap-southeast-1"
        ).addressing
        is Addressing.VIRTUAL_HOST
    )


def test_an_endpoint_that_is_not_a_url_is_refused() -> None:
    """A bare hostname is what an operator types, and a client library given one produces an
    error naming a scheme rather than the setting that was wrong. Delete this and the
    refusal happens three layers away from the cause."""
    with pytest.raises(StorageError, match="not a URL"):
        config_for(Backend.SEAWEEDFS, endpoint_url="seaweedfs:8333")


# --------------------------------------------------- the seam (M32.3.2.2)
def test_the_backend_protocol_is_the_same_four_operations_for_every_store() -> None:
    """The swap the leaf asks for: either backend works without callers changing. A protocol
    that grew a SeaweedFS filer call or an S3 Glacier transition would be a protocol only
    one backend can satisfy. Delete this and it grows one method at a time."""
    methods = {name for name in vars(StorageBackend) if not name.startswith("_")}
    assert methods == {"put_object", "get_object", "delete_object", "list_objects"}


def test_the_protocol_offers_no_presigned_url() -> None:
    """A presigned URL is a capability with no principal attached that survives being
    forwarded, which is the one shape this system's permission model exists to avoid. If it
    is ever needed it needs its own review, not a method slipped into a storage interface.
    Delete this and it arrives as an obvious convenience."""
    assert not [name for name in vars(StorageBackend) if "presign" in name]
