#!/bin/sh
# Loads every policy in ./policies into the vault. Idempotent: run it after any edit.
#
# Task ids: M31.3.2.2
#
# Usage: sh ops/openbao/load-policies.sh
#
# Needs a token that may write policy, which in practice means the root token during first
# setup. That is the whole reason UNSEAL.md destroys the root token only at step 6, after
# this has run.

set -eu

CONTAINER="${VAULT_CONTAINER:-brain-vault}"
HERE="$(cd "$(dirname "$0")" && pwd)"

if ! docker exec "$CONTAINER" bao status >/dev/null 2>&1; then
  echo "cannot reach $CONTAINER; is it running?" >&2
  exit 1
fi

if docker exec "$CONTAINER" bao status 2>&1 | grep -q 'Sealed *true'; then
  echo "the vault is sealed; open it first (see ops/openbao/UNSEAL.md)" >&2
  exit 1
fi

for file in "$HERE"/policies/*.hcl; do
  name="$(basename "$file" .hcl)"
  # Piped through `tr -d` for the same reason the deploy watcher is: this repo is edited on
  # Windows and git gives the working tree CRLF, which the vault's HCL parser rejects with a
  # message about an unexpected character rather than about line endings.
  tr -d '\015' < "$file" | docker exec -i "$CONTAINER" bao policy write "$name" -
  echo "loaded $name"
done

echo
echo "policies now in the vault:"
docker exec "$CONTAINER" bao policy list
