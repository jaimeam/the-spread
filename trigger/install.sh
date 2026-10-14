#!/bin/bash
# Installs the weekday 04:41 UTC trigger on a systemd Linux machine (e.g. a mini PC).
# Run from this folder: sudo ./install.sh   (asks for the GitHub token, stored root-only)
set -euo pipefail
cd "$(dirname "$0")"
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }

install -m 755 the-spread-dispatch /usr/local/bin/the-spread-dispatch
install -m 644 the-spread-trigger.service the-spread-trigger.timer /etc/systemd/system/
install -d -m 700 /etc/the-spread
if [ ! -s /etc/the-spread/github-token ] || [ "${1:-}" = --new-token ]; then
  read -rsp "GitHub token (fine-grained, the-spread only, Actions: read and write): " token; echo
  (umask 077; printf '%s' "$token" > /etc/the-spread/github-token)
fi

CREDENTIALS_DIRECTORY=/etc/the-spread /usr/local/bin/the-spread-dispatch --check \
  || { echo "fix the token, then: sudo ./install.sh --new-token"; exit 1; }
systemctl daemon-reload
systemctl enable --now the-spread-trigger.timer
systemctl list-timers the-spread-trigger.timer
