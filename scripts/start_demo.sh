#!/usr/bin/env bash
# Start a single supervised companion stack. FOLLOW=1 explicitly enables follow.
# `--face` relaunches only the face display in an already running stack.
set -eu
cd "$(dirname "$0")/.."

case "${1:-}" in
  "") ;;
  --face)
    pid=$(cat data/run/demo.pid 2>/dev/null || true)
    if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
      kill -HUP "$pid"
      echo "asked supervisor $pid to relaunch face_display"
      exit 0
    fi
    echo "no supervisor running; starting the whole stack"
    ;;
  *) echo "usage: $0 [--face]"; exit 2 ;;
esac

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
