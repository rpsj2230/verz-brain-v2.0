#!/bin/sh
# Installs the deploy watcher on the VPS as a systemd timer.
#
# A timer rather than cron for three reasons that matter here: the run is logged to
# journald with the unit name so `journalctl -u brain-deploy` is the whole history,
# a run that overruns cannot overlap the next one, and RandomizedDelaySec spreads the
# registry calls instead of hitting GHCR on the minute alongside everything else.
#
# Usage: sh ops/install-watcher.sh            (from a machine with ssh to the VPS)
# Task ids: M38.1.3.1

set -eu

HOST="${DEPLOY_HOST:-verz-vps}"
INTERVAL="${WATCH_INTERVAL:-3min}"

echo "==> installing the deploy watcher on $HOST, every $INTERVAL"

# Piped through `tr -d` rather than scp'd, because this repo is edited on Windows and git
# gives the working tree CRLF line endings. dash on the server reads the carriage return as
# part of the command and fails with "end of file unexpected", which reads like a truncated
# transfer rather than a line-ending problem and cost half an hour once.
tr -d '\015' < ops/watch-and-deploy.sh | ssh -o BatchMode=yes "$HOST" "cat > /usr/local/bin/brain-deploy"

ssh -o BatchMode=yes "$HOST" "
set -eu
chmod +x /usr/local/bin/brain-deploy
# Checked with the server's own shell. Git Bash accepts scripts dash refuses.
sh -n /usr/local/bin/brain-deploy

cat > /etc/systemd/system/brain-deploy.service <<'UNIT'
[Unit]
Description=Deploy Company Brain when a new image is published
# Do not fight Docker starting up after a reboot.
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/brain-deploy
# A hung registry call must not hold the timer open forever.
TimeoutStartSec=600
UNIT

cat > /etc/systemd/system/brain-deploy.timer <<'UNIT'
[Unit]
Description=Check for a new Company Brain image

[Timer]
OnBootSec=2min
OnUnitActiveSec=$INTERVAL
# Spreads the registry calls rather than hitting GHCR on the minute with everything
# else on this box.
RandomizedDelaySec=45
# A missed run while the box was down should happen once on boot, not be replayed.
Persistent=false

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now brain-deploy.timer
echo '--- timer ---'
systemctl list-timers brain-deploy.timer --no-pager | head -3
"

echo "==> installed. History: ssh $HOST journalctl -u brain-deploy -n 50"
