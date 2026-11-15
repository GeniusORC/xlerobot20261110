#!/usr/bin/env bash
# Stop everything scripts/start_demo.sh launched.
#
# The bracket in the pattern keeps pgrep from matching the shell that runs this
# script (a plain `pkill -f scripts/x.py` kills the caller too).
set -u
for name in head_track.py face_display.py voice_auto_web.py \
            health_station_web.py robot_api.py camera_web.py; do
  pids=$(pgrep -f "[s]cripts/${name}" || true)
  if [ -n "${pids}" ]; then
    kill ${pids} 2>/dev/null && echo "  stopped ${name}"
  else
    echo "  ${name}: not running"
  fi
done
sleep 2
exit 0
