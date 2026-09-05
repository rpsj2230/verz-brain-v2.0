#!/bin/sh
# Turns on the vault's audit device. Run once, after the vault is initialised and unsealed.
#
# Without this the vault keeps no record of who read which credential. That is the one
# question anybody asks after a credential turns up somewhere it should not be, and the
# vault is the only component that can answer it: the application sees a lease, not a
# history.
#
# **Two devices, and the second one is not redundancy.** OpenBao requires at least one
# audit device to accept a request. If the only device cannot write, the vault stops
# answering entirely - which is the correct direction, because a vault that hands out
# secrets without recording it is worse than a vault that is down, but it means a full disk
# takes the system out. A second device on a different sink means one can fail without the
# vault sealing itself off. This is the documented failure mode of running exactly one.
#
# **Enabled at run time, not in the config file.** Audit devices are a runtime mount in
# OpenBao, so they cannot go in BAO_LOCAL_CONFIG beside the listener. That is a real
# operational trap: recreating the container from compose restores the listener and the
# storage and silently does *not* restore the audit device, so a vault that was audited
# on Monday can be unaudited on Tuesday with nothing having failed. `bao audit list` is
# the check, and it belongs in whatever runbook covers restarting this.
#
# Usage: ssh verz-vps 'sh -s' < ops/openbao/enable-audit.sh
# Task ids: M31.3.2.6

set -eu

VAULT="${VAULT_CONTAINER:-brain-vault}"

run() {
    docker exec -e BAO_ADDR=http://127.0.0.1:8200 "$VAULT" "$@"
}

echo "==> vault status"
run bao status || true

if run bao audit list 2>/dev/null | grep -q '^file/'; then
    echo "==> file audit device already enabled; leaving it alone"
else
    echo "==> enabling the file audit device"
    # `log_raw=false` is the default and is stated anyway, because the alternative writes
    # every secret value into the audit log in plain text. The log would then be a second
    # copy of the vault with none of its protections, sitting on the same disk.
    #
    # `hmac_accessor=true`, also the default, so a token accessor in the log cannot be
    # replayed. The log is meant to answer "which identity did this", not to be a set of
    # working credentials.
    run bao audit enable -path=file file \
        file_path=/openbao/logs/audit.log \
        log_raw=false \
        hmac_accessor=true
fi

if run bao audit list 2>/dev/null | grep -q '^stderr/'; then
    echo "==> stderr audit device already enabled; leaving it alone"
else
    echo "==> enabling the stderr audit device, so a full disk does not stop the vault"
    # Goes to the container's stderr and therefore to the docker log, which rotates on a
    # different schedule and fills at a different rate from the volume. That difference is
    # the whole point: two sinks that fail together are one sink.
    run bao audit enable -path=stderr file file_path=stderr log_raw=false
fi

echo "==> devices now enabled"
run bao audit list -detailed

cat <<'NOTE'

Two things a person has to know about this, because neither is discoverable:

  1. Recreating the container does NOT restore these. They are a runtime mount, not
     configuration. After any `docker compose up --force-recreate` on the vault, run
     `bao audit list` and re-run this script if it comes back empty.

  2. If BOTH devices stop accepting writes, the vault refuses every request. That is
     deliberate and it is the right direction, but it means "the vault is down" and
     "the disk is full" look identical from the application. Check the volume first.
NOTE
