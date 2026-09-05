#!/bin/sh
# Exercises the deploy watcher against a stubbed docker, without touching a server.
#
# This exists because the rollback path only runs when a deploy has already failed, which
# is the worst possible moment to discover it does not work. Every branch below has been
# wrong at some point in this file's history, and none of them is reachable in normal
# operation.
#
# Usage: sh ops/test-watch-and-deploy.sh
# Task ids: M38.1.3.4

set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

PASS=0
FAIL=0

# The stub reads its behaviour from files, so each scenario is a few echoes rather than a
# separate fake binary.
mkdir -p "$WORK/bin" "$WORK/compose"
cat > "$WORK/bin/docker" <<'STUB'
#!/bin/sh
case "$1 $2" in
  "pull --quiet")   exit "$(cat "$STATE/pull_rc")" ;;
  "image inspect")  cat "$STATE/latest"; exit 0 ;;
  "inspect app-"*)  cat "$STATE/running"; exit 0 ;;
esac
case "$1" in
  inspect)
    # `docker inspect app-<uuid> --format ...`
    cat "$STATE/running"; exit 0 ;;
  exec)
    case "$*" in
      *printenv*) cat "$STATE/commit"; exit 0 ;;
      *health/ready*) exit "$(cat "$STATE/ready_rc")" ;;
    esac
    exit 0 ;;
  tag)
    # Rolling back: local :latest now points at the pinned previous image.
    echo "$3" > "$STATE/latest"
    echo "$3" > "$STATE/rolled_to"
    exit 0 ;;
  compose)
    # Coming up means the running image becomes whatever :latest is.
    cat "$STATE/latest" > "$STATE/running"
    # A rollback is expected to succeed even when the new image did not.
    if [ -f "$STATE/rolled_to" ]; then echo 0 > "$STATE/ready_rc"; fi
    echo "Recreated"; exit 0 ;;
  logs) exit 0 ;;
esac
exit 0
STUB
chmod +x "$WORK/bin/docker"

run_case() {
  name="$1"; pull_rc="$2"; latest="$3"; running="$4"; ready_rc="$5"; expect="$6"
  STATE="$WORK/state"; rm -rf "$STATE"; mkdir -p "$STATE"
  echo "$pull_rc" > "$STATE/pull_rc"
  echo "$latest"  > "$STATE/latest"
  echo "$running" > "$STATE/running"
  echo "$ready_rc" > "$STATE/ready_rc"
  echo "sha-old" > "$STATE/commit"

  rec="$WORK/records.jsonl"; rm -f "$rec"
  set +e
  STATE="$STATE" PATH="$WORK/bin:$PATH" \
    WATCH_RECORD="$rec" WATCH_FAILDIR="$WORK/failed" WATCH_DIR="$WORK/compose" \
    WATCH_READY_TRIES=1 WATCH_READY_GAP=0 \
    sh "$HERE/watch-and-deploy.sh" >"$WORK/out" 2>&1
  set -e

  got="$(grep -o '"outcome":"[a-z_]*"' "$rec" 2>/dev/null | tail -1 | cut -d'"' -f4)"
  [ -z "$got" ] && got="(no record)"
  if [ "$got" = "$expect" ]; then
    echo "  ok   $name -> $got"
    PASS=$((PASS + 1))
  else
    echo "  FAIL $name -> expected $expect, got $got"
    sed 's/^/         /' "$WORK/out" | tail -6
    FAIL=$((FAIL + 1))
  fi
}

echo "deploy watcher:"

# A healthy deploy records itself and leaves no failure marker.
run_case "a new image that comes up" 0 "sha256:new" "sha256:old" 0 "deployed"

# The case the whole rollback path exists for.
run_case "a new image that never becomes ready" 0 "sha256:new" "sha256:old" 1 "rolled_back"

# Nothing to go back to is a failure, not a rollback, or the record lies.
run_case "a first deploy that fails" 0 "sha256:new" "none" 1 "failed_no_rollback"

# A pull that fails must leave the running version alone and record nothing.
run_case "the registry is unreachable" 1 "sha256:old" "sha256:old" 0 "(no record)"

# Nothing to do is not an event.
run_case "no new image" 0 "sha256:old" "sha256:old" 0 "(no record)"

# The flapping guard: a digest that has used up its attempts is not tried again.
echo "  --- failure ceiling"
rm -rf "$WORK/failed"; mkdir -p "$WORK/failed"
echo 2 > "$WORK/failed/sha256_new"
run_case "an image that already failed twice" 0 "sha256:new" "sha256:old" 1 "(no record)"

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
