#!/usr/bin/env bash
set -eu
cd "$(dirname "$0")/.."
if systemctl --user is-active --quiet xlerobot.service 2>/dev/null; then
  systemctl --user stop xlerobot.service
fi
.venv/bin/python scripts/demo_manager.py stop || exit 1
echo "XLeRobot stack stopped"
