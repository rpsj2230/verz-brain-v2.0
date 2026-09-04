#!/bin/sh
# Deploys the VPS when a new image appears in the registry. Runs on the VPS itself.
#
# The direction matters. Every other way to automate this means giving something outside
# the server a way in: a Coolify API token travelling over plain HTTP, or an SSH key held
# by GitHub Actions. This pulls instead, so nothing new can reach the box and no
# credential leaves it. The GHCR read token is already there from `docker login`.
#
# It compares the registry's digest for :latest against the digest the app container is
# actually running. Comparing tags would be useless — :latest always equals :latest — and
# comparing build times would trust a clock.
#
# Install: ops/install-watcher.sh
# Task ids: M38.1.3.1

set -eu

IMAGE="${WATCH_IMAGE:-ghcr.io/rpsj2230/verz-brain-v2.0}"
UUID="${WATCH_UUID:-c74hlhygvg7scjttu8ydwnqi}"
REPO="${WATCH_REPO:-rpsj2230/verz-brain-v2.0}"
DIR="/data/coolify/services/$UUID"

log() { echo "$(date -Is) $*"; }

running_digest() {
  docker inspect "app-$UUID" --format '{{index .Image}}' 2>/dev/null || echo none
}

# The digest the registry currently serves for :latest, without pulling the layers.
published_digest() {
  docker manifest inspect "$IMAGE:latest" 2>/dev/null \
    | sha256sum | cut -c1-64 || echo unknown
}

# What the local :latest resolves to, which is what `docker compose up` would use.
local_latest() {
  docker image inspect "$IMAGE:latest" --format '{{index .Id}}' 2>/dev/null || echo none
}

log "checking $IMAGE"

before="$(local_latest)"
if ! docker pull --quiet "$IMAGE:latest" >/dev/null 2>&1; then
  log "pull failed; leaving the running version alone"
  exit 0
fi
after="$(local_latest)"

if [ "$before" = "$after" ] && [ "$(running_digest)" = "$after" ]; then
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
    exit 1
  fi
else
  log "cosign not installed; signature NOT checked"
fi

log "new image, deploying"
cd "$DIR"
docker compose up -d --remove-orphans 2>&1 | grep -E 'Started|Recreated|Error' || true

# Readiness, not liveness: a container that is up but cannot reach its database answers
# questions from whatever it can still reach.
i=0
while [ "$i" -lt 30 ]; do
  if docker exec "app-$UUID" python -c \
      "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/ready',timeout=4).status==200 else 1)" \
      >/dev/null 2>&1; then
    log "ready"
    exit 0
  fi
  i=$((i + 1))
  sleep 3
done

log "DID NOT BECOME READY after 90s"
docker logs --tail 30 "app-$UUID" 2>&1 || true
exit 1
