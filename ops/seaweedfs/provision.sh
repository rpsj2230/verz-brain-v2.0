#!/bin/sh
# Create the object store's buckets with the lifetimes brain.ops.storage declares.
#
# Runs once, from the `seaweedfs-init` service in docker-compose.objectstore.yml, and exits.
# It is idempotent: `weed shell` treats creating an existing bucket as success, so a rerun
# after a partial failure finishes the job rather than refusing.
#
# EVERY VALUE BELOW IS A COPY. The source is `BUCKETS` in `src/brain/ops/storage.py`, and
# `tests/unit/test_object_store.py` compares the two on every run. That test is the only
# reason it is safe to write the numbers twice: without it this file is a set of retention
# rules that agreed with the policy on the day somebody typed them.
#
# Retention is expressed as a bucket TTL. SeaweedFS deletes objects past their TTL during
# volume compaction rather than at the instant they expire, so a bucket with a 30 day TTL
# holds objects for 30 days and a little more. That is the correct direction for a deletion
# policy - it never removes something early - and it is the reason the reconciliation job in
# `brain.ops.storage` compares against a window rather than an exact age.
#
# `assets` has no TTL. That is not an omission; `BUCKETS` records why an unbounded lifetime
# is right for it, and a TTL invented here to make the file look uniform would delete
# referenced content.
#
# Task ids: M32.3.1.1

set -eu

MASTER="${WEED_MASTER:-seaweedfs:9333}"

fail() {
  echo "provision.sh: $1" >&2
  exit 1
}

command -v weed >/dev/null 2>&1 || fail "weed is not on PATH; this runs inside the seaweedfs image"

# One shell invocation per bucket rather than one script piped in. A single stream stops at
# the first failure and leaves the rest uncreated with a zero exit from the pipe, which is a
# provisioning run that reports success and did half the work.
create() {
  name="$1"
  ttl="$2"
  if [ -z "$ttl" ]; then
    echo "creating bucket $name with no expiry"
    echo "s3.bucket.create -name $name" | weed shell -master "$MASTER" >/dev/null \
      || fail "could not create bucket $name"
  else
    echo "creating bucket $name with ttl $ttl"
    echo "s3.bucket.create -name $name" | weed shell -master "$MASTER" >/dev/null \
      || fail "could not create bucket $name"
    echo "fs.configure -locationPrefix=/buckets/$name -ttl=$ttl -apply" \
      | weed shell -master "$MASTER" >/dev/null \
      || fail "could not set ttl $ttl on bucket $name"
  fi
}

# --- generated from brain.ops.storage.BUCKETS; the test holds these equal ---------------
# assets: console images, logos, generated report attachments
create assets ""
# recordings: browser-run video and DOM captures from automated sessions
create recordings "30d"
# exports: compliance and audit exports requested by a client
create exports "90d"
# backups: database dumps and configuration snapshots
create backups "35d"
# ---------------------------------------------------------------------------------------

echo "provision.sh: all buckets created"
