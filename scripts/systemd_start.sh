#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
source scripts/env.sh voice
source scripts/env.sh robot
exec .venv/bin/python scripts/demo_manager.py run
