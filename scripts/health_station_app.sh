#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."
source scripts/env.sh voice
exec python scripts/health_station_app.py "$@"
