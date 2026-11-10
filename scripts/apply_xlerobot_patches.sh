#!/usr/bin/env bash
# Apply local patches to the upstream XLeRobot clone.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -d XLeRobot ]; then
  echo "XLeRobot/ not found; clone it first (see docs/setup.md)" >&2
  exit 1
fi
patch -p1 -d XLeRobot < patches/0001-invert-wheel-direction.patch
