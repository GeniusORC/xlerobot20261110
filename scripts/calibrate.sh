#!/usr/bin/env bash
# Interactive calibration for the XLeRobot 0.4.0 two-wheel base.
# The robot must be powered and free to move.
set -eo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh robot

exec lerobot-calibrate \
  --robot.type=xlerobot_2wheels \
  --robot.port1=/dev/xlerobot_serial_b \
  --robot.port2=/dev/xlerobot_serial_a \
  --robot.id=xlerobot
