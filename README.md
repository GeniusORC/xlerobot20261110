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

## Health station

Desktop GUI (no port 8084):

```bash
source scripts/env.sh voice
python scripts/health_station_app.py
```

The robot stops, the head D455 captures a full-body walk (colour + aligned
depth), MediaPipe extracts 2D pose, and the result includes a pose screenshot,
gait curves and metrics JSON under `data/health/`. With depth, the report also
gives the standing distance and the step length in **metres**; the head can
follow the person and the base can drive itself to the 2–3 m working distance
without losing metric accuracy. The app is also in the desktop application menu
as **XLeRobot 固定健康站**.

## Chat LLM

`scripts/env.sh` loads `~/.config/xlerobot/llm.env` if present (chmod 600).
Set `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` (e.g. DeepSeek `deepseek-chat`)
to use the real LLM; otherwise the local companion fallback is used.

## Quick checks

```bash
# D455 (USB3, ~30 Hz color + depth)
.venv/bin/python scripts/test_d455.py

# Depth-assisted gait maths + /snapshot/head_npz transport
.venv/bin/python scripts/test_health_depth.py

# Proof that step length survives a panning/swaying camera
.venv/bin/python scripts/test_metric_camera_motion.py

# Head pan/tilt hardware test (power the robot, start robot_api.py first)
.venv/bin/python scripts/test_head.py --move

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
  **0.0635 m**. The measured turn test gives a **0.414 m** wheelbase for the
  IKEA RÅSKOG build; measure center-to-center to confirm.
- This build drives backwards for positive `x.vel`. The local clone is patched
  with `INVERT_LINEAR_DIRECTION = True` (x only, rotation unchanged); re-apply
  with `./scripts/apply_xlerobot_patches.sh`.
- D455 is now the head camera (serial `333422300185`, color + aligned depth via
  RealSense SDK). Wrist mapping changed after the swap: left `/dev/video2`,
  right `/dev/video0`. Only one process can own the D455 at a time
  (`camera_web.py` vs. LeRobot camera connections).
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
