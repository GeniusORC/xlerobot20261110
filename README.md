# XLeRobot Elder-Care Project

Working repository for the 智能养老服务领域轮式机器人关键技术攻关 project.

## Hardware

- XLeRobot 0.4.0, two-wheel differential base (5-inch wheels)
- 2 × SO-101 arms + 360° head
- Intel RealSense D455 (serial `333422300185`, firmware `5.13.0.55`)
- 2 × CH343 USB-serial boards (`/dev/xlerobot_serial_a`, `/dev/xlerobot_serial_b`)
- Host: Gigabyte AERO 15-SA, i7-9750H, 16 GB RAM, GTX 1660 Ti 6 GB, Ubuntu 24.04

## Software

| Environment | Python | Key packages | Purpose |
|---|---|---|---|
| `.venv` | 3.11 | FunASR/SenseVoice, Silero VAD, edge-tts, pyrealsense2 | Voice loop / ASR / TTS |
| `.venv-robot` | 3.12 | LeRobot 0.6.1, torch 2.11.0+cu130, XLeRobot 2-wheel plugin | Main robot control |
| `.venv-robot07` | 3.12 | LeRobot 0.7.0, XLeRobot 2-wheel plugin | Compatibility test only |
| system | 3.12 | ROS 2 Jazzy, Nav2, RTAB-Map, realsense2_camera | Navigation / perception |

## Quick checks

```bash
# D455 (USB3, ~30 Hz color + depth)
.venv/bin/python scripts/test_d455.py

# Voice loop (built-in mic; use headset/USB array mic for far field)
.venv/bin/python scripts/voice_loop.py --record 5

# Motor bus scan (power the robot first)
.venv-robot/bin/python scripts/test_motor_bus.py

# ROS 2 RealSense
source /opt/ros/jazzy/setup.bash
ros2 launch realsense2_camera rs_launch.py \
  depth_module.depth_profile:=640x480x30 \
  rgb_camera.color_profile:=640x480x30
```

## Known issues

- The XLeRobot config defaults to `wheel_radius=0.05`; the 5-inch wheel radius is
  **0.0635 m**. The IKEA RÅSKOG build has an estimated **0.367 m** wheelbase
  (cart width 0.35 m + mounts); measure center-to-center to confirm.
- Motor-bus mapping (1 Mbaud):
  - `/dev/xlerobot_serial_b` → IDs 1–8 → **bus1** (left arm + head), use as `port1`
  - `/dev/xlerobot_serial_a` → IDs 1–6, 9, 10 → **bus2** (right arm + 2 wheels), use as `port2`
- D455 must be on a direct USB3 port where possible. A hub plus stale ROS nodes caused
  stream drops/1–3 Hz; after killing stale nodes it runs at ~30 Hz.
- LeRobot 0.7 now passes `connect()/configure()/disconnect()`, calibration restore,
  normalized joint reads, and the lifted-wheel speed test. Values/commands are
  equivalent to 0.6.1. On-ground odometry and a 10-minute teleop are still pending;
  keep 0.6.1 as the main environment until then.

See `docs/setup.md` for details.
