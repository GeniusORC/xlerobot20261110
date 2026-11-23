#!/usr/bin/env bash
# Start a single supervised companion stack. FOLLOW=1 explicitly enables follow.
set -eu
cd "$(dirname "$0")/.."
if systemctl --user is-active --quiet xlerobot.service 2>/dev/null; then
  echo "xlerobot.service is already running"
  exit 0
fi
if systemctl --user is-enabled --quiet xlerobot.service 2>/dev/null; then
  exec systemctl --user start xlerobot.service
fi
export FOLLOW="${FOLLOW:-0}"
source scripts/env.sh voice
source scripts/env.sh robot
mkdir -p data/logs
setsid /bin/bash scripts/systemd_start.sh >>data/logs/supervisor.log 2>&1 </dev/null &
exec .venv/bin/python scripts/service_readiness.py
