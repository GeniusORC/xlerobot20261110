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
follow the person, and during a capture the base can hold a fixed standoff
(`采集时跟随`, requested 1 m, automatically pulled back to the closest distance
that keeps the full body in frame) without losing metric accuracy. The app is
also in the desktop application menu as **XLeRobot 固定健康站**.

## Chat LLM

`scripts/env.sh` loads `~/.config/xlerobot/llm.env` if present (chmod 600).
Set `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` (e.g. DeepSeek `deepseek-chat`)
to use the real LLM; otherwise the local companion fallback is used.

## Companion demo (voice, face, head tracking, follow)

One command brings the whole stack up:

```bash
./scripts/start_demo.sh     # FOLLOW=0 for head-only tracking
./scripts/stop_demo.sh
```

| Service | Port | What it does |
|---|---|---|
| `camera_web.py` | 8080 | web page with four live streams (head, left, right, head depth) |
| `voice_auto_web.py` | 8082 | always-on voice (no push-to-talk), chat, lip-sync |
| `health_station_web.py` | 8084 | gait capture and reports |
| `robot_api.py` | 8090 | base / head / arms |
| `face_display.py` | 8086 | the laptop panel as the robot's face |
| `head_track.py` | — | turns the head to keep your face centred; `--follow` drives the base too |

The interaction loop is not a keyword bot: one DeepSeek function-calling turn
decides chat vs. robot action, the head camera contributes one sentence of
context ("看到一个人，距离约1.1米，在画面中间"), and long-term facts live in
`data/chat/chat.db` (`facts` table).

Safety is enforced in code, not in the prompt:

- emergency/crisis phrases never reach the model; `停下` is a hard keyword stop;
- misfired movement tool calls on sentences without motion words are re-asked
  with tools disabled;
- base following needs an explicit latch (`POST /follow`), `stop`/`estop`
  revoke it, and `/velocity` returns 409 without it — a voice stop cannot be
  overridden by the follow loop 100 ms later;
- follow holds 1 m, never drives forward below 0.85 m, backs off below 0.55 m.

Voice commands: `往前走三十厘米`, `左转九十度`, `过来`, `看我`, `停下`,
`跟着我`, `别跟着我了`.

Barge-in works because PipeWire software echo cancellation removes the robot's
own TTS from the microphone (~34 dB measured), so the VAD can keep listening
while it speaks.

Details: `docs/setup.md` sections 26–29.

## Quick checks

```bash
# D455 (USB3, ~30 Hz color + depth)
.venv/bin/python scripts/test_d455.py

# Depth-assisted gait maths + /snapshot/head_npz transport
.venv/bin/python scripts/test_health_depth.py

# Proof that step length and step count survive a moving head and base
.venv/bin/python scripts/test_metric_camera_motion.py

# Head pan/tilt hardware test (power the robot, start robot_api.py first)
.venv/bin/python scripts/test_head.py --move

# Joystick / gamepad teleop (start robot_api.py first)
.venv-robot/bin/python scripts/teleop_joy.py --list
.venv-robot/bin/python scripts/teleop_joy.py --takeover

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
