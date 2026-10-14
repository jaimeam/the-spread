#!/bin/bash
# Checks the installed trigger end to end without making an episode: a transient systemd timer
# fires in 5s and runs the installed dispatcher with the same sandbox and token as
# the-spread-trigger.service, but with --test, so it starts trigger-test.yml (prints one line).
# Run from this folder after install.sh: sudo ./test.sh
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }

unit=the-spread-trigger-test-$(date +%s)
systemd-run --quiet --unit="$unit" --on-active=5s \
  -p DynamicUser=yes -p LoadCredential=github-token:/etc/the-spread/github-token \
  -p Wants=network-online.target -p After=network-online.target \
  /usr/local/bin/the-spread-dispatch --test
echo "timer set, fires in 5s..."
sleep 8
while [[ $(systemctl show -p ActiveState --value "$unit.service") = activ* ]]; do sleep 2; done
journalctl -u "$unit.service" --no-pager -o cat
echo "check the run: https://github.com/jaimeam/the-spread/actions/workflows/trigger-test.yml"
