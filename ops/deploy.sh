#!/bin/sh
# Deploy the current published image to the VPS, without going through the Coolify UI.
#
# Coolify writes the compose file and its env to disk and runs the stack from there, so
# this drives exactly what "Restart (pull latest)" in the UI drives: same file, same
# project name, same environment. Coolify reads container state rather than owning it, so
# its dashboard reflects the result correctly.
#
# This exists because every deploy otherwise needed someone to click a button, which made
# progress depend on a person being at a screen.
#
# Usage: ops/deploy.sh
# Task ids: M38.1.3

set -eu

HOST="${DEPLOY_HOST:-verz-vps}"
UUID="${DEPLOY_UUID:-c74hlhygvg7scjttu8ydwnqi}"
DIR="/data/coolify/services/$UUID"
URL="${DEPLOY_URL:-https://brain.194.233.66.89.sslip.io}"

echo "==> deploying to $HOST ($UUID)"

ssh -o BatchMode=yes "$HOST" "
  set -eu
  cd '$DIR'
  echo '--- pulling ---'
  docker compose pull 2>&1 | grep -E 'Pulled|Error' || true
  echo '--- recreating ---'
  docker compose up -d --remove-orphans 2>&1 | grep -E 'Started|Error|Recreated' || true
"

echo "==> waiting for readiness"
for i in $(seq 1 30); do
  code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 10 "$URL/health/ready" || echo 000)
  [ "$code" = "200" ] && break
  sleep 3
done

if [ "$code" != "200" ]; then
  echo "FAILED: /health/ready returned $code after 90s" >&2
  ssh -o BatchMode=yes "$HOST" "docker logs --tail 30 app-$UUID 2>&1 | tail -30" >&2
  exit 1
fi

echo "==> live"
curl -sk --max-time 15 "$URL/api/status.json" \
  | sed -n 's/.*"commit":"\([^"]*\)".*"percent":\([0-9.]*\).*/    commit \1 · \2% complete/p'

# The migrate container is expected to exit; it runs migrations and stops. Exit 0 is
# success, anything else is a failed migration and worth seeing immediately.
echo "==> migrations"
ssh -o BatchMode=yes "$HOST" "
  code=\$(docker inspect migrate-$UUID --format '{{.State.ExitCode}}' 2>/dev/null || echo '?')
  if [ \"\$code\" = '0' ]; then
    echo '    migrations applied cleanly'
  else
    echo \"    MIGRATION FAILED (exit \$code):\"
    docker logs --tail 12 migrate-$UUID 2>&1 | sed 's/^/      /'
  fi
"
