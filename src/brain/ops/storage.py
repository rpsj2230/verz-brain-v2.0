"""Where files go, how long they stay, and which S3 the client is actually paying for.

Object storage is the part of a deployment where a mistake is silent for a long time and
then total. A bucket left readable, a recording of a browser run kept for three years
because nobody set a lifecycle rule, a backup bucket without versioning where the nightly
job overwrites the good copy with the broken one: none of those fail a health check, and
all of them are found afterwards.

So four things are decided here rather than in whichever console the operator happens to
be looking at.

**The bucket set is closed and an object's bucket is derived from what it is.** `bucket_for`
matches on an `ObjectKind` and uses `assert_never`, so adding a kind of object without
deciding where it lives is a type error rather than a runtime default. The default this
replaces is the one everybody writes: everything into `assets`, because that is the bucket
that existed when the feature was built, and now the compliance export shares a lifecycle
rule with the console's logo.

**Every bucket states a retention and why.** `retention_reason` is required prose and
`retention_days` may be None only with that prose attached. A lifecycle policy nobody can
explain is a policy that gets relaxed the first time somebody needs an old file, and the
relaxation is permanent because nobody knows what the number was protecting.

**No bucket is public, and that is checked rather than configured.** A public S3 bucket is
the most reproduced data breach of the last decade, and the reason is that the setting is
one click and the consequence is invisible from inside the application.

**The backend is a choice the client makes and it changes three things, not one.** Pointing
at Cloudflare R2 instead of SeaweedFS is not just a different endpoint: R2 has exactly one
region name, `auto`, and signing a request with a real-looking region produces a 403 that
reads as bad credentials and sends whoever is debugging it to rotate a key that was fine.
SeaweedFS's S3 gateway needs path-style addressing because virtual-host style requires
wildcard DNS the client does not have. `config_for` refuses the combinations that produce
those failures instead of passing them through to be discovered.

Contradiction worth naming: the tracker asks for the storage backend to sit "behind the
extension interface", and `brain/ext/__init__.py` says in as many words that anything this
system ships and depends on does not belong there, because an extension point is precisely
where "must work" stops being true. Files are not optional to this platform. So the
`StorageBackend` protocol lives here, in code that is tested here, and `brain.ext` remains
what its own docstring says it is. The swap the leaf actually asks for - either backend
works without the callers changing - is what the protocol delivers.

**Credentials come from OpenBao, and for two of the three backends they can only be static,
which is worth saying out loud.** `brain.ops.openbao` mints leases from dynamic engines and
refuses anything else, and its static reader is restricted to `providers/`. AWS S3 has a
dynamic engine and gets a real lease. SeaweedFS and R2 have none - SeaweedFS is not an AWS
account and R2 tokens are minted by Cloudflare's own API - so their keys are stored and
read, which means they are long-lived and rotated by hand. `static_credential_backends`
exists so that is a fact somebody can query rather than a gap they discover during a key
rotation. The paths are checked against `brain.ops.openbao`'s own constants rather than
against a second copy of them here, for the reason `brain.ops.sweeps` learned the hard way:
a rule restated is a rule that ends up looser than the thing it guards.

Task ids: M32.3.1.2, M32.3.1.3, M32.3.1.4, M32.3.2.1, M32.3.2.2
"""

from __future__ import annotations

import enum
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final, Protocol, assert_never

from brain.ops.openbao import DYNAMIC_MOUNTS, STATIC_PREFIX


class StorageError(Exception):
    """Raised when a bucket, a lifecycle rule or a backend configuration cannot be deployed."""


class ObjectKind(enum.StrEnum):
    """What is being stored. The question `bucket_for` is asked.

    Deliberately about the object rather than about the bucket. "Which bucket should this
    go in" is answered differently by every caller; "what is this" has one answer.
    """

    CONSOLE_ASSET = "console_asset"
    AGENT_ATTACHMENT = "agent_attachment"
    BROWSER_RUN_RECORDING = "browser_run_recording"
    COMPLIANCE_EXPORT = "compliance_export"
    DATABASE_DUMP = "database_dump"


@dataclass(frozen=True)
class Bucket:
    """One bucket and its lifecycle, which are the same decision.

    Versioning and retention interact and are declared together for that reason. Versioning
    without retention grows without bound; retention without versioning means the nightly
    job that writes a corrupt file has destroyed the good one and the retention window is
    irrelevant.
    """

    name: str
    holds: str
    #: None means kept until somebody deletes it, and requires `retention_reason` to say
    #: why an unbounded lifetime is the right answer for this content.
    retention_days: int | None
    retention_reason: str
    versioned: bool
    #: Present as a field so that the check below has something to check. Nothing sets it
    #: true; the field exists because a bucket made public through a console is a bucket
    #: this constant no longer describes, and a value that is never true is a value a
    #: reconciliation job can compare a live bucket against.
    public_read: bool = False

    def __post_init__(self) -> None:
        if self.retention_days is not None and self.retention_days < 1:
            msg = (
                f"bucket {self.name!r} has retention {self.retention_days}; that is not a lifetime"
            )
            raise StorageError(msg)
        if not self.retention_reason.strip():
            msg = (
                f"bucket {self.name!r} states no reason for its retention; "
                "an unexplained lifecycle rule is one that gets relaxed and never restored"
            )
            raise StorageError(msg)


BUCKETS: Final[tuple[Bucket, ...]] = (
    Bucket(
        name="assets",
        holds="console images, logos, generated report attachments",
        retention_days=None,
        retention_reason=(
            "an asset is referenced by a document that outlives any window we could pick, "
            "and a broken image in a two-year-old report is a support ticket"
        ),
        versioned=False,
    ),
    Bucket(
        name="recordings",
        holds="browser-run video and DOM captures from automated sessions",
        retention_days=30,
        retention_reason=(
            "a recording is a screen capture of whatever the run could see, which is the "
            "widest data any single artefact in this system holds; it is kept long enough "
            "to debug the run that produced it and no longer"
        ),
        versioned=False,
    ),
    Bucket(
        name="exports",
        holds="compliance and audit exports requested by a client",
        retention_days=90,
        retention_reason=(
            "long enough for a regulator's exchange to conclude; an export is a flattened "
            "copy of records whose permissions no longer apply to it"
        ),
        versioned=False,
    ),
    Bucket(
        name="backups",
        holds="database dumps and configuration snapshots",
        retention_days=35,
        retention_reason=(
            "one full monthly cycle plus a few days, so a fault noticed at month end can "
            "still be restored from before it started"
        ),
        versioned=True,
    ),
)

#: Buckets whose content is a copy of something else and must survive being overwritten.
_MUST_BE_VERSIONED: Final[frozenset[str]] = frozenset({"backups"})

#: Buckets that must not be kept indefinitely, whatever reason is offered.
_MUST_EXPIRE: Final[frozenset[str]] = frozenset({"recordings", "exports"})


def bucket(name: str) -> Bucket:
    for b in BUCKETS:
        if b.name == name:
            return b
    msg = f"unknown bucket {name!r}; known: {[b.name for b in BUCKETS]}"
    raise StorageError(msg)


def bucket_for(kind: ObjectKind) -> Bucket:
    """Where this kind of object lives.

    `assert_never` is the whole function, in the same way and for the same reason as
    `brain.gate.context.traffic_class_for`: a new `ObjectKind` cannot reach production
    without somebody deciding what its retention is. A dictionary with a `.get` default
    would file it wherever the default pointed, and the default is always `assets`.
    """
    match kind:
        case ObjectKind.CONSOLE_ASSET | ObjectKind.AGENT_ATTACHMENT:
            return bucket("assets")
        case ObjectKind.BROWSER_RUN_RECORDING:
            return bucket("recordings")
        case ObjectKind.COMPLIANCE_EXPORT:
            return bucket("exports")
        case ObjectKind.DATABASE_DUMP:
            return bucket("backups")
        case _:  # pragma: no cover - unreachable while ObjectKind is exhaustive
            assert_never(kind)


def lifecycle_gaps() -> tuple[str, ...]:
    """Every bucket whose lifecycle does not hold, in words an operator can act on.

    Returns rather than raises, and returns all of them, because this is read by a
    reconciliation job comparing the declaration with a live store: the interesting output
    is the whole list of differences, not the first one.
    """
    findings: list[str] = []
    for b in BUCKETS:
        if b.public_read:
            findings.append(f"{b.name}: readable without credentials")
        if b.name in _MUST_BE_VERSIONED and not b.versioned:
            findings.append(
                f"{b.name}: not versioned; a job that writes a corrupt copy destroys the good one"
            )
        if b.name in _MUST_EXPIRE and b.retention_days is None:
            findings.append(
                f"{b.name}: kept indefinitely; this bucket holds copies whose permissions "
                "stopped applying the moment they were written"
            )
    return tuple(findings)


# ----------------------------------------------------------------- backends
class Backend(enum.StrEnum):
    """The three stores a client can be pointed at."""

    #: Ours, on the client's own host.
    SEAWEEDFS = "seaweedfs"
    AWS_S3 = "aws_s3"
    CLOUDFLARE_R2 = "cloudflare_r2"


class Addressing(enum.StrEnum):
    """How a bucket name reaches the endpoint.

    Not a detail. Virtual-host addressing puts the bucket in the hostname, which needs
    wildcard DNS and a certificate that covers it; a self-hosted gateway has neither, and
    the failure is a name-resolution error that looks like a network problem.
    """

    PATH = "path"
    VIRTUAL_HOST = "virtual_host"


#: R2 has one region and it is this literal. A real-looking region name is accepted by the
#: client library, changes the signature, and comes back as 403 - which reads as a bad
#: key and sends somebody to rotate a credential that was never the problem.
R2_REGION: Final = "auto"


@dataclass(frozen=True)
class BackendConfig:
    """Everything a client needs to talk to one store, with the traps already refused."""

    backend: Backend
    endpoint_url: str
    region: str
    addressing: Addressing
    #: Where the credential comes from in OpenBao. Dynamic for the one backend that has an
    #: engine; a stored key under `providers/` for the two that do not.
    credential_path: str
    dynamic_credentials: bool


def credential_path(backend: Backend) -> str:
    """The OpenBao path this backend's credentials come from.

    The values are checked against `brain.ops.openbao`'s own constants by the test suite
    rather than merely looking right here, so a path that the vault module would refuse
    fails before a deployment discovers it.
    """
    match backend:
        case Backend.AWS_S3:
            return "aws/creds/brain-storage"
        case Backend.SEAWEEDFS:
            return f"{STATIC_PREFIX}seaweedfs"
        case Backend.CLOUDFLARE_R2:
            return f"{STATIC_PREFIX}cloudflare-r2"
        case _:  # pragma: no cover - unreachable while Backend is exhaustive
            assert_never(backend)


def static_credential_backends() -> tuple[Backend, ...]:
    """Backends whose keys are stored and rotated by hand, because no engine mints them.

    A question anybody can ask, rather than a gap found during a rotation. Deriving it from
    `credential_path` rather than listing it separately means the answer cannot drift from
    the paths it describes.
    """
    return tuple(b for b in Backend if credential_path(b).startswith(STATIC_PREFIX))


def config_for(backend: Backend, *, endpoint_url: str, region: str | None = None) -> BackendConfig:
    """A usable configuration, or a refusal that names the trap.

    Refuses rather than corrects. Silently rewriting a caller's region to `auto` would make
    the configuration work and leave the operator believing R2 has regions, which is the
    belief that produces the next 403.
    """
    if not endpoint_url.startswith(("http://", "https://")):
        msg = f"endpoint {endpoint_url!r} is not a URL"
        raise StorageError(msg)

    match backend:
        case Backend.CLOUDFLARE_R2:
            if region is not None and region != R2_REGION:
                msg = (
                    f"R2 has one region, {R2_REGION!r}; {region!r} signs the request "
                    "differently and comes back 403, which reads as a bad credential"
                )
                raise StorageError(msg)
            resolved_region, addressing = R2_REGION, Addressing.PATH
        case Backend.SEAWEEDFS:
            # Its gateway ignores the region entirely, but the signature needs one and both
            # ends have to agree on which. `us-east-1` is what every S3 client defaults to.
            resolved_region, addressing = region or "us-east-1", Addressing.PATH
        case Backend.AWS_S3:
            if not region or region == R2_REGION:
                msg = f"AWS S3 needs a real region; got {region!r}"
                raise StorageError(msg)
            resolved_region, addressing = region, Addressing.VIRTUAL_HOST
        case _:  # pragma: no cover - unreachable while Backend is exhaustive
            assert_never(backend)

    path = credential_path(backend)
    return BackendConfig(
        backend=backend,
        endpoint_url=endpoint_url.rstrip("/"),
        region=resolved_region,
        addressing=addressing,
        credential_path=path,
        dynamic_credentials=path.startswith(DYNAMIC_MOUNTS),
    )


class StorageBackend(Protocol):
    """What the rest of the system may ask of object storage, and nothing more.

    Narrow on purpose. Every method here has to exist on all three backends, so anything
    that only one of them can do - SeaweedFS's filer API, R2's bindings, S3 lifecycle
    transitions to Glacier - is absent, and a caller reaching for it has to add it to the
    protocol and be seen adding it to three implementations.

    No presigned-URL method. A presigned URL is a capability with no principal attached
    that survives being forwarded, which is the one shape this system's whole permission
    model exists to avoid; if it is ever needed it needs its own review, not a method
    slipped into a storage interface.
    """

    def put_object(self, bucket_name: str, key: str, body: bytes, content_type: str) -> None: ...

    def get_object(self, bucket_name: str, key: str) -> bytes: ...

    def delete_object(self, bucket_name: str, key: str) -> None: ...

    def list_objects(self, bucket_name: str, prefix: str) -> Iterator[str]: ...
