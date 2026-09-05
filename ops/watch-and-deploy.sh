#!/bin/sh
# Deploys the VPS when a new image appears in the registry, and puts the old one back if
# the new one does not come up. Runs on the VPS itself.
#
# The direction matters. Every other way to automate this means giving something outside
# the server a way in: a Coolify API token travelling over plain HTTP, or an SSH key held
# by GitHub Actions. This pulls instead, so nothing new can reach the box and no
# credential leaves it. The GHCR read token is already there from `docker login`.
#
# It compares the registry's digest for :latest against the digest the app container is
# actually running. Comparing tags would be useless, since :latest always equals :latest,
# and comparing build times would trust a clock.
#
# **Rollback (M38.1.3.4).** A deploy that fails readiness used to leave the site down until
# somebody noticed, which on a Saturday is the whole weekend. The previous image is now
# pinned by digest before anything changes, and restored if the new one does not answer
# within the window. Pinning by digest rather than by tag matters: `:latest` is exactly the
# tag that has already moved by the time rollback is needed.
#
# Install: ops/install-watcher.sh
# Task ids: M38.1.3.1, M38.1.3.4, M38.1.3.5

set -eu

IMAGE="${WATCH_IMAGE:-ghcr.io/rpsj2230/verz-brain-v2.0}"
UUID="${WATCH_UUID:-c74hlhygvg7scjttu8ydwnqi}"
REPO="${WATCH_REPO:-rpsj2230/verz-brain-v2.0}"
DIR="${WATCH_DIR:-/data/coolify/services/$UUID}"
#: Deployment records, newest last. Read by /build/deployments.
RECORD="${WATCH_RECORD:-/var/lib/brain/deployments.jsonl}"
#: How many times an image may fail readiness before the watcher stops trying it.
#:
#: Not one. A deploy that failed because the database was restarting is a transient
#: failure, and blacklisting a good image on a single bad minute means the next good
#: release never ships either. Not unlimited: without a ceiling the timer redeploys the
#: same broken image every three minutes, flapping the service all weekend, which is worse
#: than staying on the old one.
FAIL_LIMIT=2
FAILDIR="${WATCH_FAILDIR:-/var/lib/brain/failed}"
READY_TRIES="${WATCH_READY_TRIES:-30}"
READY_GAP="${WATCH_READY_GAP:-3}"

log() { echo "$(date -Is) $*"; }

running_image() {
  docker inspect "app-$UUID" --format '{{index .Image}}' 2>/dev/null || echo none
}

local_latest() {
  docker image inspect "$IMAGE:latest" --format '{{index .Id}}' 2>/dev/null || echo none
}

running_commit() {
  docker exec "app-$UUID" printenv BRAIN_COMMIT_SHA 2>/dev/null || echo unknown
}

# Readiness, not liveness: a container that is up but cannot reach its database answers
# questions from whatever it can still reach.
wait_ready() {
  i=0
  while [ "$i" -lt "$READY_TRIES" ]; do
    if docker exec "app-$UUID" python -c \
        "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/ready',timeout=4).status==200 else 1)" \
        >/dev/null 2>&1; then
      return 0
    fi
    i=$((i + 1))
    sleep "$READY_GAP"
  done
  return 1
}

# M38.1.3.5. One line per deploy, appended, never rewritten. It is not the audit ledger:
# the ledger lives in Postgres and nothing writes to it yet, and a deployment record that
# depends on the application being up is a record that is missing for exactly the deploys
# worth reading. This file survives the app being down, which is the point.
record() {
  mkdir -p "$(dirname "$RECORD")"
  printf '{"at":"%s","outcome":"%s","commit":"%s","image":"%s","previous":"%s"}\n' \
    "$(date -Is)" "$1" "$2" "$3" "$4" >> "$RECORD"
}

# Failure bookkeeping, keyed on the image digest so a new build starts with a clean slate.
fail_file() {
  mkdir -p "$FAILDIR"
  # printf, not echo: echo appends a newline and `tr -c` turns that newline into another
  # underscore, so the file was named `sha256_new_` while every message about it said
  # `sha256_new`. Consistent, and therefore harmless, right up until somebody follows the
  # instruction to delete it by hand.
  printf '%s/%s
' "$FAILDIR" "$(printf '%s' "$1" | tr -c 'A-Za-z0-9' '_')"
}

fail_count() {
  f="$(fail_file "$1")"
  [ -f "$f" ] && cat "$f" || echo 0
}

note_failure() {
  f="$(fail_file "$1")"
  echo $(( $(fail_count "$1") + 1 )) > "$f"
}

log "checking $IMAGE"

before="$(local_latest)"
if ! docker pull --quiet "$IMAGE:latest" >/dev/null 2>&1; then
  log "pull failed; leaving the running version alone"
  exit 0
fi
after="$(local_latest)"

if [ "$before" = "$after" ] && [ "$(running_image)" = "$after" ]; then
  log "no change"
  exit 0
fi

# Verify before running, not after. A signature checked after the container is live is a
# signature checked too late.
if command -v cosign >/dev/null 2>&1; then
  if cosign verify "$IMAGE:latest" \
      --certificate-identity-regexp "^https://github.com/$REPO/" \
      --certificate-oidc-issuer https://token.actions.githubusercontent.com \
      >/dev/null 2>&1; then
    log "signature verified"
  else
    log "SIGNATURE DID NOT VERIFY - refusing to deploy"
    record refused unknown "$after" "$(running_image)"
    exit 1
  fi
else
  log "cosign not installed; signature NOT checked"
fi

if [ "$(fail_count "$after")" -ge "$FAIL_LIMIT" ]; then
  log "image $after has failed readiness $FAIL_LIMIT times; not trying it again"
  log "push a new build, or clear $(fail_file "$after") to retry this one"
  exit 0
fi

# Pin the running image by digest before anything changes. `:latest` has already moved, so
# a rollback that re-pulled a tag would fetch the broken image again.
PREVIOUS="$(running_image)"
PREVIOUS_COMMIT="$(running_commit)"
log "current image $PREVIOUS (commit $PREVIOUS_COMMIT)"

log "new image, deploying"
cd "$DIR"
docker compose up -d --remove-orphans 2>&1 | grep -E 'Started|Recreated|Error' || true

if wait_ready; then
  log "ready"
  rm -f "$(fail_file "$after")"
  record deployed "$(running_commit)" "$after" "$PREVIOUS"
  exit 0
fi

log "DID NOT BECOME READY after $((READY_TRIES * READY_GAP))s"
note_failure "$after"
log "that image has now failed $(fail_count "$after") of $FAIL_LIMIT allowed attempts"
docker logs --tail 30 "app-$UUID" 2>&1 || true

if [ "$PREVIOUS" = "none" ]; then
  # Nothing to go back to. A first deploy that fails is a failure, not a rollback, and
  # pretending otherwise would leave a confusing record.
  log "no previous image to roll back to"
  record failed_no_rollback unknown "$after" none
  exit 1
fi

log "ROLLING BACK to $PREVIOUS"
docker tag "$PREVIOUS" "$IMAGE:latest"
docker compose up -d --remove-orphans 2>&1 | grep -E 'Started|Recreated|Error' || true

if wait_ready; then
  log "rolled back and ready; the bad image is still in the registry and will be retried"
  record rolled_back "$PREVIOUS_COMMIT" "$after" "$PREVIOUS"
  # Exit non-zero on purpose. The rollback worked, but a deploy failed and the timer's
  # journal is the only place anybody would see that. A clean exit here would make a
  # broken release look like an ordinary quiet run.
  exit 1
fi

# Worse than where we started: neither version answers. Almost always something outside
# the image, and the log line says so, because the instinct is to blame the deploy.
log "ROLLBACK ALSO NOT READY - the problem is probably not the image (database? disk? pooler?)"
docker logs --tail 30 "app-$UUID" 2>&1 || true
record rollback_failed "$PREVIOUS_COMMIT" "$after" "$PREVIOUS"
exit 1
