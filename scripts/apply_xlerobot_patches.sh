#!/usr/bin/env bash
# Apply local patches to the upstream XLeRobot clone.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -d XLeRobot ]; then
  echo "XLeRobot/ not found; clone it first (see docs/setup.md)" >&2
  exit 1
fi
for p in patches/*.patch; do
  echo "Applying $p"
  patch --forward -p1 -d XLeRobot < "$p"
done
