# Source this file, do not execute it:
#   source scripts/env.sh          # ROS only
#   source scripts/env.sh robot    # ROS + .venv-robot (LeRobot 0.6.1)
#   source scripts/env.sh robot07  # ROS + .venv-robot07 (test only)
#   source scripts/env.sh voice    # ROS + .venv (ASR/TTS)

export PATH="$HOME/.local/bin:$PATH"

# Optional local-only LLM credentials (never committed)
if [ -f "$HOME/.config/xlerobot/llm.env" ]; then
  source "$HOME/.config/xlerobot/llm.env"
fi

# ROS 2's setup.bash is not `set -u` clean. Temporarily disable nounset while
# sourcing it, then restore the caller's state.
case $- in
  *u*) _xle_had_u=1; set +u ;;
  *)   _xle_had_u=0 ;;
esac

if [ -f /opt/ros/jazzy/setup.bash ]; then
  source /opt/ros/jazzy/setup.bash
fi

case "${1:-}" in
  robot)   source "$(dirname "${BASH_SOURCE[0]}")/../.venv-robot/bin/activate" ;;
  robot07) source "$(dirname "${BASH_SOURCE[0]}")/../.venv-robot07/bin/activate" ;;
  voice)   source "$(dirname "${BASH_SOURCE[0]}")/../.venv/bin/activate" ;;
esac

if [ "${_xle_had_u:-0}" = "1" ]; then
  set -u
fi
unset _xle_had_u
