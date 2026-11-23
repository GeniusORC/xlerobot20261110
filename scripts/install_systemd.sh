#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$HOME/.config/systemd/user"
sed "s|@ROOT@|$ROOT|g" "$ROOT/scripts/systemd/xlerobot.service" \
  > "$HOME/.config/systemd/user/xlerobot.service"
systemctl --user daemon-reload
systemctl --user enable xlerobot.service
echo "Installed xlerobot.service. Start with: systemctl --user start xlerobot.service"
echo "Enable lingering for boot startup: loginctl enable-linger $USER"
