# Setup log — 2026-11-08

## 1. System

- Ubuntu 24.04.2, kernel 7.0.0-42, X11
- ROS 2 Jazzy installed via apt; verified packages:
  `nav2_bringup`, `rtabmap_ros`, `realsense2_camera`, `depthimage_to_laserscan`,
  `robot_localization`, `slam_toolbox`
- RealSense: `librealsense2` + `ros-jazzy-realsense2-camera`
- Serial udev rules:
  - `5B14030560` → `/dev/xlerobot_serial_a`
  - `5B8E114819` → `/dev/xlerobot_serial_b`
  - mode `0666`, so no re-login for `dialout` is required

Source ROS in each shell:

```bash
source /opt/ros/jazzy/setup.bash
```

## 2. D455

Verified with `pyrealsense2`:

- Device: RealSense D455, serial `333422300185`, firmware `5.13.0.55`, USB 3.2
- 640×480 depth + color, 30 fps
- Depth intrinsics: fx=fy≈389.0, cx≈321.9, cy≈244.9
- Center depth in test scene: ~2.72 m

Verified with ROS 2:

```bash
ros2 launch realsense2_camera rs_launch.py \
  depth_module.depth_profile:=640x480x30 \
  rgb_camera.color_profile:=640x480x30
```

Topics are under `/camera/camera/...`; color and depth measured at ~29.8/30.0 Hz.
If the stream drops to 1–3 Hz or the device disappears, kill stale
`realsense2_camera_node` processes and/or move the D455 to a direct USB3 port.

## 3. Voice loop

Environment: `.venv` (Python 3.11, torch CPU).

Pipeline:

```
arecord -> Silero VAD -> SenseVoiceSmall -> edge-tts -> aplay
```

Verified with a TTS-generated WAV and with acoustic loopback through the built-in
speaker/microphone. Example:

```bash
.venv/bin/python scripts/voice_loop.py --record 5
```

The built-in microphone works but is not suitable for 1–3 m far-field use; a USB
array mic with AEC is the next hardware step.

## 4. LeRobot environments

### Main: 0.6.1

```bash
cd /home/adminchen/Documents/xlerobot
uv venv --python 3.12 .venv-robot
uv pip install --python .venv-robot/bin/python \
  torch torchvision --index-url https://download.pytorch.org/whl/cpu
uv pip install --python .venv-robot/bin/python \
  "lerobot[feetech]==0.6.1"
uv pip install --python .venv-robot/bin/python -e \
  XLeRobot/software/plugins/xlerobot_model --no-deps
uv pip install --python .venv-robot/bin/python -e \
  XLeRobot/software/plugins/lerobot_robot_xlerobot_2wheels --no-deps
uv pip install --python .venv-robot/bin/python pyzmq
```

Installed versions: `lerobot 0.6.1`, `torch 2.11.0+cu130`, CUDA works on the
GTX 1660 Ti (sm_75). Note: the CPU torch install was replaced by LeRobot's
CUDA 13 pin; this is fine on driver 595.

### Test: 0.7.0

```bash
uv venv --python 3.12 .venv-robot07
uv pip install --python .venv-robot07/bin/python \
  "lerobot[feetech]==0.7.0"
uv pip install --python .venv-robot07/bin/python -e \
  XLeRobot/software/plugins/xlerobot_model --no-deps
uv pip install --python .venv-robot07/bin/python -e \
  XLeRobot/software/plugins/lerobot_robot_xlerobot_2wheels --no-deps
uv pip install --python .venv-robot07/bin/python pyzmq
```

Verified on 0.7.0:

- `from lerobot.motors.feetech import FeetechMotorsBus, OperatingMode` works
  (deprecated alias, but importable)
- `XLerobot2WheelsConfig` and `XLerobot2Wheels` construct successfully
- Both motor buses are built with the expected SO-101/head/base motor maps

Powered-robot test on 0.7.0:

- `connect()` → `configure()` → `disconnect()` passes, including the register
  writes for operating mode / P-I-D coefficients and torque enable/disable.
- Calibration file restore (`write_calibration`) passes.
- Raw and normalized joint-position reads are numerically identical to 0.6.1
  on the same calibrated robot (all 16 motors, same values).
- Still pending: wheel velocity commands and a 10-minute teleop. Keep 0.6.1
  as the main environment until these pass.

Normalized read is a read-only observation, not a motion command:

```python
robot.bus1.sync_read("Present_Position", robot.left_arm_motors)  # normalize=True
robot.bus1.sync_read("Present_Position", robot.left_arm_motors, normalize=False)  # raw ticks
```

The calibration file is at
`~/.cache/huggingface/lerobot/calibration/robots/xlerobot_2wheels/xlerobot.json`.

## 5. XLeRobot 2-wheel notes

Config fields (`XLerobot2WheelsConfig`):

```python
port1: str = "/dev/ttyACM0"   # left arm + head
port2: str = "/dev/ttyACM1"   # right arm + 2 wheels
wheel_radius: float = 0.05
wheelbase: float = 0.25
```

For the 5-inch wheels, set `wheel_radius=0.0635` (measured value preferred).
The upstream default `wheelbase=0.25` is too small for the IKEA RÅSKOG build.

### Wheelbase (measured by the 80° turn test)

The RÅSKOG cart is 350 mm wide and the CAD base spans 367 mm, so 0.367 m was
the initial estimate. The first on-ground turn test then commanded 90° but the
robot actually turned **80°**, which gives:

```text
L = 0.5779 m (wheel arc difference) / radians(80°) = 0.4139 m
```

Use **0.414 m** as the current value (expect 0.41–0.42 m). Measure the
center-to-center distance with a tape to confirm; update if needed.

Example override when running a LeRobot CLI:

```bash
--robot.wheel_radius=0.0635 --robot.wheelbase=0.414
```

With the wrong wheelbase, straight motion is mostly unaffected but turns are
wrong: using 0.25 m when the real value is 0.414 m makes a commanded turn about
40% smaller than requested.

### Direction inversion patch

This build moves **backwards** when `x.vel` is positive. The initial global
inversion fixed forward but also reversed the turn direction; the final patch
inverts only the linear (x) component:
`INVERT_LINEAR_DIRECTION = True` in
`software/src/robots/xlerobot_2wheels/xlerobot_2wheels.py`. Reproduce with:

```bash
./scripts/apply_xlerobot_patches.sh
```

Patch file: `patches/0001-invert-wheel-direction.patch`.

### Verified motor mapping (1 Mbaud)

`FeetechMotorsBus.scan_port()` found:

| Port | IDs | Bus |
|---|---|---|
| `/dev/xlerobot_serial_b` | 1–8 | bus1: left arm + head |
| `/dev/xlerobot_serial_a` | 1–6, 9, 10 | bus2: right arm + 2 wheels |

So the correct config is:

```python
port1="/dev/xlerobot_serial_b"   # left arm + head
port2="/dev/xlerobot_serial_a"   # right arm + wheels
```

Both environments passed a powered `connect()/configure()/disconnect()` test:

```bash
.venv-robot/bin/python   scripts/test_lerobot_connect.py --yes --skip-calibration
.venv-robot07/bin/python scripts/test_lerobot_connect.py --yes --skip-calibration
```

The only skipped step is the interactive calibration (and therefore position reads).

## 6. Calibration and teleop

Run this in an interactive terminal; the robot must be free to move:

```bash
source scripts/env.sh robot
lerobot-calibrate \
  --robot.type=xlerobot_2wheels \
  --robot.port1=/dev/xlerobot_serial_b \
  --robot.port2=/dev/xlerobot_serial_a \
  --robot.id=xlerobot
```

Follow the prompts: move the left arm + head to the middle, press Enter, then move
each joint through its full range and press Enter; repeat for the right arm + wheels.
Calibration files are compatible between LeRobot 0.6.1 and 0.7.0.

Then:

```bash
# 0.6.1 main
lerobot-teleoperate \
  --robot.type=xlerobot_2wheels \
  --robot.port1=/dev/xlerobot_serial_b \
  --robot.port2=/dev/xlerobot_serial_a \
  --robot.id=xlerobot \
  --robot.wheel_radius=0.0635 \
  --robot.wheelbase=<measured>

# 0.7 comparison
source scripts/env.sh robot07
# same command
```

## 7. Wheel-speed smoke test

Run with the wheels lifted/on blocks and a physical E-stop within reach:

```bash
source scripts/env.sh robot
python scripts/test_wheel_speed.py --yes
```

Verified on both environments (0.6.1 and 0.7.0), 0.03 m/s / 10 deg/s, 0.8 s per command:

| Command | Left raw | Right raw | Result |
|---|---:|---:|---|
| forward | +300 | -300 | both wheels forward (after patch) |
| reverse | -300 | +300 | both wheels backward (after patch) |
| rotate left | -200 | -200 | left backward / right forward |
| rotate right | +200 | +200 | left forward / right backward |

Interactive field check after the final patch:

```text
FORWARD_OK
LEFT_OK
```

After the direction-inversion patch, a positive `x.vel` should move forward.
The raw signs above are from the patched build; confirm the physical direction
on the floor before relying on teleop. After `stop_base()` the wheel velocities
return to 0 within about 0.4 s. 0.6.1 and 0.7.0 produce the same commands and
comparable feedback.

## 8. Next steps

1. Measure wheelbase; run 1 m straight + 90° turn on the ground.
2. Record 10–20 teleop episodes; compare 0.6.1 vs 0.7 for 10 minutes.
3. Add a USB mic array/speaker; test 1–3 m ASR with servo noise.
4. Map the lab with RTAB-Map/Nav2.

## 8. Odometry test (2026-11-10)

Script: `scripts/test_odometry.py`

Straight phase:

- command: 0.15 m/s for 6.7 s (expected 1.005 m)
- encoder wheel angle: 893.85 deg average
- encoder distance with r=0.0635 m: **0.991 m**
- the robot went straight, but the direction was reversed; fixed by the
  direction-inversion patch above
- tape measurement still needed to refine the wheel radius

Turn phase:

- first run: 20 deg/s for 4.5 s, wheel arc difference 0.5779 m;
  measured actual rotation 80 deg → wheelbase **0.4139 m**
- second run after the direction fix: 0.1 m/s for 5 s, encoder distance
  magnitude **0.490 m** (target 0.5 m); 20 deg/s for 4.5 s, wheel arc
  difference **0.6226 m**
- if the second run actually turned 86.2 deg, the inferred wheelbase is 0.414 m
- current value: **0.414 m**; measure the final actual angle on the floor to
  confirm (0.6226 / radians(angle))

Formula:

```text
L_actual = wheel_arc_difference / actual_body_angle_rad
```

### RGB camera configuration

The D455 is not used for now. The three robot RGB cameras are configured in
`config_xlerobot_2wheels.py` (patch `0002-configure-rgb-cameras.patch`):

| Name | Device | Resolution | Notes |
|---|---|---|---|
| `left_wrist` | `/dev/video6` | 640x480@30 | left wrist (sharp) |
| `right_wrist` | `/dev/video2` | 640x480@30 | right wrist (soft) |
| `head` | `/dev/video0` | 640x480@30 | head (blurry, needs focus) |

Verify all three simultaneously:

```bash
source scripts/env.sh robot
python scripts/test_cameras.py
```

Verified: all three open together, `get_observation()` returns
`left_wrist`, `right_wrist`, `head` as `(480, 640, 3) uint8`, and 20 rounds of
three frames take ~0.75 s (~26.5 rounds/s total).

Camera labels were corrected after a physical check: `/dev/video0` is the head,
`/dev/video6` is the left wrist, and `/dev/video2` is the right wrist.

Measured sharpness (Laplacian variance) with the corrected mapping:

- left wrist `/dev/video6`: ~462 (sharp)
- right wrist `/dev/video2`: ~32 (soft)
- head `/dev/video0`: ~15 (blurry)

The head camera is currently the blurry one. These UVC cameras expose no V4L2
focus control, so clean the lens / remove any protective film and turn the
manual focus ring to the working distance. Without D455 depth/IMU, navigation
and gait work must use monocular vision and wheel odometry; the 90° test angle
must come from a floor/phone measurement.

## 9. Direction check and short validation

After the final `INVERT_LINEAR_DIRECTION` patch, the interactive direction check
reported:

```text
FORWARD_OK
LEFT_OK
```

Short validation on the ground:

- straight: 0.1 m/s for 5.0 s (target 0.5 m); encoder distance magnitude 0.490 m
- turn: 20 deg/s for 4.5 s (target 90 deg); wheel arc difference 0.6226 m
- with wheelbase 0.414 m, the implied body angle is
  `0.6226 / 0.414 = 1.504 rad = 86.2 deg`; measure the actual floor angle to
  confirm 0.414 m
- the encoder sign is negative for physical forward on this build; magnitude is
  used for distance/wheelbase calculations

## 10. Keyboard teleop

```bash
source scripts/env.sh robot
python scripts/teleop_base.py
```

Keys:

```text
w / s      forward / backward
a / d      rotate left / right
space      stop
1 / 2 / 3  speed level
q or Esc   quit
```

The script holds the current arm pose and commands only the base. It stops the
base on every idle loop and disconnects in `finally`.

## 11. Voice/chat commands

The web demo (`scripts/voice_web.py`, port 8082) routes these commands to the
robot, and everything else to the companion chat:

| Intent | Example phrases | Action |
|---|---|---|
| `stop` | 停下、停止、别动 | stop base |
| `come` | 过来、靠近 | forward 0.2 m (stub) |
| `forward` | 前进、往前 | forward 0.2 m |
| `backward` | 后退、往后 | backward 0.2 m |
| `turn_left` | 左转、向左转 | rotate ~90° |
| `turn_right` | 右转、向右转 | rotate ~90° |
| `look` | 看我、看着我、面向我 | face detection + base turn to center |
| `report` | 报告、状态 | return robot status |

All motion commands are 0.05 m/s / 20 deg/s, stop on timeout, and can be
preempted by “停下”. The `look` command fetches `/snapshot/head` from the camera
server and rotates the base until the largest detected face is centered; if no
face is found it reports “没有看到您的脸”.

## 12. Always-on voice agent

No push-to-talk button is needed. `scripts/voice_auto_web.py` runs a server-side
microphone loop:

```text
arecord (default) -> Silero VAD -> SenseVoice -> intent/chat -> TTS -> aplay
```

- `default` is used for capture/playback because the ALSA card indices changed
  from `2` to `0`; `plughw:2,0` is no longer valid.
- Speech end is detected after ~0.6 s of silence; replies are spoken through the
  laptop speaker.
- The microphone is muted during TTS playback plus a 2 s tail; an additional
  text-similarity filter ignores the robot's own voice.
- Web UI: `http://127.0.0.1:8082`; messages are polled from `/api/messages`.
- “暂停监听/继续监听” toggles the mic loop without stopping the web server.

Run:

```bash
source scripts/env.sh voice
python scripts/voice_auto_web.py --port 8082
```

`CAPTURE_DEV`/`PLAYBACK_DEV` can override the ALSA device names.

## 13. Fixed health station (RGB + MediaPipe)

`scripts/health_station_web.py` implements the fixed-station gait/posture demo:

```bash
source scripts/env.sh voice
python scripts/health_station_web.py --port 8084
```

Open `http://127.0.0.1:8084`.

- “准备（停车）” calls the robot API stop.
- “开始采集 10 秒” captures head-camera snapshots from
  `http://127.0.0.1:8080/snapshot/head` at 10 Hz.
- MediaPipe PoseLandmarker (Tasks API, `models/pose_landmarker_lite.task`)
  extracts 33 landmarks per frame.
- Outputs:
  - `data/health/pose_<ts>.png` — annotated full-body screenshot
  - `data/health/gait_<ts>.png` — knee/hip/ankle curves
  - `data/health/metrics_<ts>.json` — step count, cadence, knee/hip angles,
    trunk angle, symmetry, normalized step length, sway
- The page shows live head RGB, progress, metrics and the result images.

The person must stand 2–3 m in front of the robot with the full body visible.
If no full body is detected, the page reports “没有检测到完整人体”.

A static-image pipeline test with MediaPipe's sample `pose.jpg` produced
15 valid frames, knee/hip/trunk metrics, a pose overlay and a gait plot.

## 14. Desktop health-station app (no 8084)

`scripts/health_station_app.py` is the GUI version; it does not need port 8084:

```bash
source scripts/env.sh voice
python scripts/health_station_app.py
```

Or launch from the application menu: **XLeRobot 固定健康站**
(desktop entry installed at `~/.local/share/applications/xlerobot-health.desktop`).

- Tk window with live camera, pose overlay, progress and metrics.
- Buttons: 准备（停车） / 开始采集 10 秒 / 停止 / 打开结果文件夹.
- Opens `/dev/video0` directly if free; otherwise falls back to
  `http://127.0.0.1:8080/snapshot/head`.
- Calls `robot_api.py` on port 8090 to stop the base before capture.
- Saves `pose_*.png`, `gait_*.png`, `metrics_*.json` under `data/health/`.
- Uses `models/pose_landmarker_lite.task` and MediaPipe Tasks.

## 15. LLM chat backend

The auto voice agent uses an OpenAI-compatible LLM if these variables are set:

```bash
export LLM_BASE_URL=https://api.deepseek.com/v1
export LLM_API_KEY=...
export LLM_MODEL=deepseek-chat
```

For local use, put them in a git-ignored, user-only file:

```text
~/.config/xlerobot/llm.env   (chmod 600)
```

`scripts/env.sh` sources that file automatically. Never commit the key.

Verified with DeepSeek `deepseek-chat`: `/api/chat` returned `engine: "llm"` and
a natural reply, then TTS spoke it. Without the variables it falls back to the
local rule-based companion.

## 16. Health report, tips and SQLite history

After each capture the health station now:

1. generates a Chinese screening report from the metrics;
2. generates 3–5 health tips;
3. assigns a risk level (`低` / `中` / `高` / `数据不足`);
4. saves everything to `data/health/health.db` (SQLite).

Database table `sessions` stores: timestamp, duration, frames, steps, cadence,
symmetry, risk, full metrics JSON, report text, tips JSON, pose/gait/json paths
and `created_at`.

The web page at 8084 shows the report, tips, risk level and a history table
(`/api/history`, `/api/session/<id>`); the newest DB row is loaded on startup.

Important: a partial-body close-up can make MediaPipe hallucinate leg landmarks.
`extract()` now rejects landmarks outside the image bounds; if the full body is
not visible, the session reports “数据不足” instead of inventing gait numbers.

The report is a 2D RGB screening aid, not a medical diagnosis. Persistent pain,
dizziness, chest tightness or sudden instability should be evaluated by a
clinician.

## 17. Chat database

The always-on voice/chat agent persists every message to SQLite:

```text
data/chat/chat.db
```

Table `messages`:

| column | meaning |
|---|---|
| id | message id |
| role | user / bot / sys |
| text | message text |
| engine | `llm` (DeepSeek), `fallback`, `safety`, `local` |
| intent | command intent, or `chat` |
| created_at | ISO timestamp |

`/api/messages?after=<id>` reads from the DB, so the page restores history after
a restart. The initial greeting is only inserted when the database is empty.
Verified with DeepSeek: the user message and the `engine: "llm"` reply were
written, and both reappeared after restarting the service.
