#!/bin/bash
# Installs The Spread on a systemd Linux machine (e.g. a mini PC), which then runs every job itself:
# the weekday 04:41 UTC brief and episode, Ingest feeds at 00:23, 08:23, 12:23, 16:23 and 20:23 UTC,
# and Listener stats at 10:37 and 18:37 UTC. Each job pushes to main; GitHub only deploys the site.
# Run from this folder: sudo ./install.sh
#   First run: writes /etc/the-spread/env from env.example; fill in the keys, then run it again.
#   --new-token  replace the GitHub token (fine-grained, the-spread only, Contents: read and write)
set -euo pipefail
cd "$(dirname "$0")"
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
command -v git >/dev/null && command -v flock >/dev/null || { echo "needs git and flock (util-linux)"; exit 1; }

install -d -m 700 /etc/the-spread
if [ ! -s /etc/the-spread/env ]; then
  install -m 600 env.example /etc/the-spread/env
  echo "Wrote /etc/the-spread/env. Fill in the keys (sudo nano /etc/the-spread/env), then run sudo ./install.sh again."
  exit 0
fi
if [ ! -s /etc/the-spread/github-token ] || [ "${1:-}" = --new-token ]; then
  read -rsp "GitHub token (fine-grained, the-spread only, Contents: read and write): " token; echo
  (umask 077; printf '%s' "$token" > /etc/the-spread/github-token)
fi

# The old version only started workflows on GitHub.
rm -f /usr/local/bin/the-spread-dispatch
install -m 755 the-spread-run /usr/local/bin/the-spread-run
install -m 644 the-spread-trigger.service the-spread-trigger.timer \
  the-spread-ingest.service the-spread-ingest.timer \
  the-spread-stats.service the-spread-stats.timer /etc/systemd/system/
systemctl daemon-reload

# Check in the same sandbox the jobs run in: clones the repo, checks Python and that the token may push.
systemd-run --wait --pipe --quiet -p DynamicUser=yes -p User=the-spread -p StateDirectory=the-spread \
  -p Environment=HOME=/var/lib/the-spread -p EnvironmentFile=/etc/the-spread/env \
  -p LoadCredential=github-token:/etc/the-spread/github-token \
  /usr/local/bin/the-spread-run --check \
  || { echo "fix the above (token: sudo ./install.sh --new-token), then run this again"; exit 1; }

systemctl enable --now the-spread-trigger.timer the-spread-ingest.timer the-spread-stats.timer
systemctl list-timers 'the-spread-*'
