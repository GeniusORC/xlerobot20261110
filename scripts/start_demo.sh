#!/usr/bin/env bash
# Start the whole companion stack in the background.
#
#   ./scripts/start_demo.sh          # everything
#   ./scripts/stop_demo.sh           # stop everything
#
# Services and ports:
#   camera_web.py        8080  four live streams (head, left, right, head depth)
#   voice_auto_web.py    8082  always-on voice + chat + face lip-sync
#   health_station_web.py 8084  gait capture and reports
#   robot_api.py         8090  base / head / arms
#   face_display.py      8086  the laptop screen as a face
#   head_track.py         -     turns the head to keep your face centred
set -u
cd "$(dirname "$0")/.."
LOG=${LOG:-/tmp/opencode}
mkdir -p "$LOG"

# FOLLOW=0 disables base following (head tracking still runs).
FOLLOW=${FOLLOW:-1}
HT_ARGS=""
[ "$FOLLOW" = "1" ] && HT_ARGS="--follow"

export DISPLAY="${DISPLAY:-:1}"
export XAUTHORITY="${XAUTHORITY:-/run/user/1000/gdm/Xauthority}"

running() { pgrep -f "[s]cripts/$1" >/dev/null; }

launch() {
  local name=$1; shift
  if running "$name"; then
    echo "  $name: already running"
    return 0
  fi
  setsid bash -c "$*" > "$LOG/${name%.py}.log" 2>&1 < /dev/null &
  echo "  $name: started"
}

echo "starting the companion stack (logs in $LOG)"
launch camera_web.py "exec .venv-robot/bin/python scripts/camera_web.py --port 8080"
launch robot_api.py "source scripts/env.sh robot && exec python scripts/robot_api.py --port 8090"
launch health_station_web.py "exec .venv/bin/python scripts/health_station_web.py --port 8084"
launch voice_auto_web.py "source scripts/env.sh voice && exec python scripts/voice_auto_web.py --port 8082"
launch face_display.py "exec .venv/bin/python scripts/face_display.py --monitor 1"
launch head_track.py "exec .venv/bin/python scripts/head_track.py $HT_ARGS"

sleep 6
echo
echo "  video   http://127.0.0.1:8080/      (open on the second monitor)"
echo "  chat    http://127.0.0.1:8082/"
echo "  health  http://127.0.0.1:8084/"
echo "  face    http://127.0.0.1:8086/state"
echo
echo "  robot_api:  curl -s localhost:8090/status"
if [ "$FOLLOW" = "1" ]; then
  echo "  following:  on (1.0 m standoff; say 跟着我 / 别跟着我了 to switch)"
else
  echo "  following:  off (start again with FOLLOW=1 to enable)"
fi
echo "  stop all:   ./scripts/stop_demo.sh"
