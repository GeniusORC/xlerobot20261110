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

The D455 replaced the old head camera. The three robot cameras are configured in
`config_xlerobot_2wheels.py` (patch `0002-configure-rgb-cameras.patch`):

| Name | Device | Resolution | Notes |
|---|---|---|---|
| `left_wrist` | `/dev/video2` | 640x480@30 | left wrist |
| `right_wrist` | `/dev/video0` | 640x480@30 | right wrist |
| `head` | D455 serial `333422300185` | 640x480@30 | RealSense SDK, color + depth |

Verify all three simultaneously:

```bash
source scripts/env.sh robot
python scripts/test_cameras.py
```

Verified: all three open together, `get_observation()` returns
`left_wrist`, `right_wrist`, `head` as `(480, 640, 3) uint8`, and 20 rounds of
three frames take ~0.75 s (~26.5 rounds/s total).

Device nodes shifted after the D455 replacement: the left wrist is now
`/dev/video2`, the right wrist is `/dev/video0`, and the D455 is accessed through
the RealSense SDK rather than a `/dev/video*` node.

Measured with `scripts/test_cameras.py`:

- left wrist `/dev/video2`: sharpness ~152
- right wrist `/dev/video0`: sharpness ~69
- D455 head: sharpness ~825, color `(480,640,3)`, aligned depth
  `(480,640,1) uint16` (max ~7.4 m)
- three-camera round: ~26.9 rounds/s

The D455 color and aligned depth are streamed by `camera_web.py` at
`/snapshot/head` and `/snapshot/head_depth` (also `/stream/head` and
`/stream/head_depth`). Only one process can own the D455 at a time: stop
`camera_web.py` before using the RealSense head camera inside LeRobot
teleoperation/recording, and stop LeRobot camera connections before starting
`camera_web.py`.

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

## 13. Fixed health station (D455 colour + depth + MediaPipe)

`scripts/health_station_web.py` implements the fixed-station gait/posture demo:

```bash
source scripts/env.sh voice
python scripts/health_station_web.py --port 8084
```

Open `http://127.0.0.1:8084`.

- “准备（停车）” calls the robot API stop.
- “开始采集 10 秒” captures head-camera frames from
  `http://127.0.0.1:8080/snapshot/head_npz` at 10 Hz. That endpoint returns the
  colour JPEG **and** the D455 depth aligned to colour, plus camera intrinsics,
  so one request gives both 2D pose and metric depth.
- MediaPipe PoseLandmarker (Tasks API, `models/pose_landmarker_lite.task`)
  extracts 33 landmarks per frame.
- Outputs:
  - `data/health/pose_<ts>.png` — annotated full-body screenshot
  - `data/health/gait_<ts>.png` — knee/hip/ankle curves, plus foot excursion
    and body distance in metres when depth is available
  - `data/health/metrics_<ts>.json` — step count, cadence, knee/hip angles,
    trunk angle, symmetry, normalized step length, sway, `distance_m`,
    `step_length_m`, `walk_axis`, `depth_frames`
- The page shows live head colour, progress, metrics and the result images.

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
- Defaults to `http://127.0.0.1:8080/snapshot/head` and automatically uses the
  depth-packed endpoint; `--source realsense:333422300185` opens the D455
  directly (stop `camera_web.py` first, single owner).
- Calls `robot_api.py` on port 8090 to stop the base before capture.
- Saves `pose_*.png`, `gait_*.png`, `metrics_*.json` under `data/health/`
  and writes the session to `health.db`.
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
symmetry, risk, full metrics JSON, report text, tips JSON, pose/gait/json paths,
`created_at`, `distance_m`, `step_length_m` and a `depth` flag. Older databases
are migrated in place with `ALTER TABLE` on startup.

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

## 18. D455 head camera replacement

The D455 is configured as the head camera with the LeRobot RealSense backend:

```python
"head": RealSenseCameraConfig(
    serial_number_or_name="333422300185",
    fps=30,
    width=640,
    height=480,
    color_mode=ColorMode.RGB,
    rotation=Cv2Rotation.NO_ROTATION,
    use_depth=True,
)
```

The wrist cameras moved to `/dev/video2` (left) and `/dev/video0` (right) after
the old head camera was removed. The D455 is not opened as a `/dev/video*` node
by the robot configuration; it uses the RealSense SDK.

`camera_web.py` now streams D455 color and aligned depth:

```text
http://127.0.0.1:8080/stream/head
http://127.0.0.1:8080/stream/head_depth
http://127.0.0.1:8080/snapshot/head
http://127.0.0.1:8080/snapshot/head_depth
```

The health station uses `/snapshot/head`. The desktop health app can use the
D455 directly with:

```bash
python scripts/health_station_app.py --source realsense:333422300185
```

In that mode, stop `camera_web.py` first (single D455 owner).

## 19. Depth-assisted gait: metric distance and step length

The health station previously reported a *normalized* step length (pixels
divided by shoulder width), which depends on camera distance. With the D455 the
station now reports metres.

### Transport

`camera_web.py` adds a packed snapshot endpoint:

```text
GET /snapshot/head_npz
```

It returns a compressed NumPy archive with:

| key | type | meaning |
|---|---|---|
| `color_jpeg` | uint8[] | JPEG bytes of the colour frame |
| `depth` | uint16[480,640] | depth aligned to colour, millimetres |
| `fx`, `fy`, `cx`, `cy` | float | colour intrinsics |
| `depth_scale` | float | metres per depth unit (0.001) |

Measured on the D455: ~170 KiB per request, 20 requests in 0.52 s (~38 fps
round-trip on localhost), colour `(480,640,3)`, depth `(480,640)` uint16,
`fx=386.8 fy=386.3 cx=323.6 cy=248.2`.

### Metric conversion

For a landmark at pixel `(u,v)` with median depth `Z` in a small window:

```text
X = (u - cx) * Z / fx        (metres, camera frame)
```

Depth samples outside 0.15–9 m or invalid (0) are discarded; a window median is
used so a single missing pixel cannot create a spike.

### Distance

`distance_m` is the median of the hip-midpoint depth over all valid frames. It
drives the "stand at 2–3 m" check: the report adds a warning when the measured
distance is outside 1.2–4.5 m.

### Step length

Step length is *not* the lateral ankle separation (that is stance width). It is
the peak-to-peak forward excursion of each foot **relative to the body** along
the walking axis:

```text
fwd_foot(t) = body_axis(t) - foot_axis(t)
step_length = mean_over_feet( P95(fwd) - P5(fwd) )
```

The walking axis is chosen per session by comparing the body's depth range with
its lateral range, so both "walk towards the robot" (axis Z) and "walk across
the frame" (axis X) work. Excursions outside 0.02–1.2 m are rejected as tracking
jumps. The gait plot gets a fourth panel with foot excursion and body distance.

### Validation

`scripts/test_health_depth.py` checks the transport and the maths:

```text
PASS npz endpoint reachable   170 KiB
PASS depth shape matches colour
PASS intrinsics plausible     fx=386.8 fy=386.3 cx=323.6 cy=248.2
PASS hip distance is 2.5 m    2.500
PASS ankle X from pinhole model   -0.250 / 0.250
PASS step length recovers synthetic swing   got=0.59 true=0.59
PASS lateral walk selects x axis
PASS lateral step length recovered   0.48
PASS no-depth still produces metrics
PASS report mentions metre step length
```

Limitations: monocular-depth step length is an estimate (D455 depth noise,
loose clothing, foot occlusion). It is suitable for trend monitoring, not for
diagnosis or for replacing motion capture.

## 20. Moving camera: head tracking, auto-framing, metric invariance

The D455 sits on the 2-DoF head, so both the head (pan/tilt) and the base can
move. A moving camera normally corrupts metric gait measurement, so the
measurement was changed to be invariant to camera motion, and then the camera
was allowed to move.

### 20.1 Body-frame step length (camera-motion invariant)

Earlier the step length was measured along a camera axis (`X` or `Z`). That is
only valid for a static camera. Now every pose frame is unprojected to 3D with
its own depth:

```text
P = ((u-cx)*Z/fx, (v-cy)*Z/fy, Z)
```

and the person's own frame is built from their shoulders and hips:

```text
up    = normalize(P_shoulder_mid - P_hip_mid)
right = normalize((P_RS - P_LS) - ((P_RS - P_LS).up) up)
fwd   = normalize(cross(up, right))

step  = P95( (P_ankle - P_hip_mid).fwd ) - P5( ... )
```

Because the foot vector and the axis are expressed in the same camera frame,
rotating or translating the camera does not change the result.

### 20.2 Simulation validation

`scripts/test_metric_camera_motion.py` simulates a walking person, projects the
joints through the real pinhole model while the head pans to follow them
(±18° yaw) and the base sways, rasterises a depth map, and runs the real
`extract -> add_depth -> analyze` pipeline.

```text
=== walking lateral (ground-truth step length 0.59 m) ===
static camera          body=0.58 m (err 0.009)  camera-axis=0.58 m (err 0.009)
panning/swaying camera body=0.59 m (err 0.001)  camera-axis=0.53 m (err 0.059)

=== walking towards (ground-truth 0.59 m) ===
static camera          body=0.57 m (err 0.019)  camera-axis=0.57 m (err 0.019)
panning/swaying camera body=0.59 m (err 0.001)  camera-axis=0.59 m (err 0.001)
```

The body-frame estimate stays within 2 cm under camera motion; the old
camera-axis estimate drifts by 6 cm in the lateral case.

### 20.3 Head and base control

`robot_api.py` gained:

```text
GET  /head                      -> {"pan":..,"tilt":..}
POST /head  {"pan":..,"tilt":..}   absolute, normalized -100..100
POST /head  {"dpan":..,"dtilt":..} relative
POST /head  {"center":true}        back to mid-range
POST /drive {"x":..,"theta":..,"duration":..}   bounded fine positioning
GET  /status                    -> also reports head/busy state
```

Limits: each command moves at most 8 normalized units (a slew limit), and the
only envelope is the calibrated motor range −98..98. An earlier version also
clamped to ±30 tilt around the pose captured at connect time; that could trap
the camera looking up at the ceiling with no way to tilt down, so the whole
full-body capture failed. `center` ramps smoothly back to 0 (opt-in, can be a
large move). Drive |x| ≤ 0.08 m/s, |theta| ≤ 25 °/s, duration ≤ 3 s. All bus
access goes through one lock so the 10 Hz tracker, the voice commands and the
status endpoint cannot interleave.

Important: `POST /command {"intent":"stop"}` (called by the health station
before every capture) sets the internal `STOP_EVENT`. Head commands now clear
it explicitly — without that, the first capture of a session left the camera
frozen for the rest of the session.

`GET /head` reports the raw pose plus `pan_ref`/`tilt_ref` and the effective
`pan_window`/`tilt_window`.

Head mapping: `head_motor_1` = pan (yaw), `head_motor_2` = tilt (pitch), IDs 7
and 8 on bus1.

### 20.4 Health-station behaviour

| Mode | Base | Head | Metric |
|---|---|---|---|
| Idle + 头部跟随 | still | pans/tilts to keep the person centred | live distance shown |
| 自动取景 | drives to 2.6 ± 0.45 m | aims at the person | full-body framing |
| 采集 | still | follows, keeps the body framed | body-frame step length |
| 采集 + 采集时跟随 | holds the standoff (see §21) | follows | body-frame step length |

Without following, the base deliberately stays still during a capture. Run
自动取景 first, then start the capture; §21 adds optional base following.

The tracker closes the loop on pixels, so no motor-angle calibration is
needed. On this build, a positive `dpan` turns the camera **right** and a
negative `dtilt` tilts it **up**; those are the defaults. Each axis watches its
own error and flips its own sign after three worsening updates, so a
differently assembled head self-corrects. Commands are rate-limited to one per
0.20 s so a move based on an already-stale frame cannot stack up and overshoot.
During a capture the torso is held at 42 % of the frame height, which leaves
room for head and feet.

Two bugs made real captures produce nonsense (9 accepted frames and then a
crash) and are now fixed:

* MediaPipe `detect_for_video` needs a monotonically increasing timestamp.
  The capture loop used `int(elapsed*1000)`, which restarted at 0 for every
  capture and raised `ValueError: Input timestamp must be monotonically
  increasing` on the second one. It now uses `time.monotonic()`.
* Accepted frames are not evenly spaced (a frame is dropped whenever a knee or
  ankle leaves the image). Step peaks are now detected after resampling onto
  the nominal fps grid using each row's real timestamp, instead of assuming
  contiguous samples.

The report also refuses to score a capture when fewer than 35 % of the frames
were usable, and says so, rather than printing a plausible-looking but wrong
step length.

### 20.5 Hardware test

```bash
source scripts/env.sh robot
python scripts/robot_api.py --port 8090      # power the robot first
python scripts/test_head.py                  # read-only
python scripts/test_head.py --move           # 5-unit nudge, then re-centre
```

## 21. Following during capture ("采集时跟随")

Requested behaviour: during a capture the base should also follow the person
and hold a fixed standoff (1 m), instead of standing still. Two things had to
be fixed first: step counting, and the fact that a full adult does not fit in
the frame at 1 m.

### 21.1 The 1 m target is physically impossible, so it is clamped

The D455 colour stream is 640x480 with a vertical FOV of about 57-63 deg, so at
1 m it sees roughly 1.1-1.2 m of height. A 1.7 m adult therefore cannot be
fully framed at 1 m, and `extract()` deliberately rejects partial bodies.

The station measures the person's height from the pose + depth and derives the
closest distance that still keeps the whole body in frame:

```text
body_h_m = (v_ankle - v_nose) * depth / fy * 1.08     # nose is below head top
d_min    = body_h_m * fy / (0.85 * frame_h)
```

`0.85` leaves a 7-8 % margin so the ankles are not clipped. Examples:

| Person height | Requested | Effective standoff |
|---|---|---|
| 1.82 m | 1.0 m | 1.72 m |
| 1.42 m | 1.0 m | 1.53 m |

The requested value is stored in `follow_distance`, the clamped value in
`follow_target`, and the UI says when it pulled the robot back:

> 跟随 1.5 m：1.0 m 时全身会出框，已自动拉远以保证准度

Requests are clamped to at least the person's `d_min`, never above it. If the
height cannot be measured the robot does **not** move at all until a valid
full-body frame arrives.

### 21.2 Step counting no longer uses image space

The old detector took peaks of the ankle image height (`ankle_ly`). A base that
moves adds a trend to that signal, which corrupts the cadence. The detector now
prefers the body-frame foot excursion `bf_l` / `bf_r` (metres, §20.1), which is
already invariant to camera motion:

```text
step_count = peaks(bf_l) + peaks(bf_r)   # prominence 0.04 m, 0.35 s gap
step_detector = "depth_body_frame"
```

The image-space detector remains only as a no-depth fallback.

### 21.3 Short-lease velocity endpoint

Continuous following needs a velocity that persists between HTTP calls, but a
dead client must never leave the base driving.

```text
POST /velocity {"x": m/s, "theta": deg/s}    -> lease 0.6 s
POST /velocity {"stop": true}                -> immediate stop
```

`robot_api.py` applies the value in its 20 Hz worker loop (queue timeout
0.05 s) and stops the base as soon as the lease expires, `POST /estop` is
called, or a normal command arrives. Limits: `x` in [-0.20, +0.12] m/s,
`theta` in ±25 deg/s. Approaching a person is deliberately slower than
retreating.

### 21.4 Controller and safety envelope

```text
deadband = 0.15 m
speed    = clip(0.30 * (dist - target), -0.20, +0.12) m/s
```

| Condition | Action |
|---|---|
| target not measured yet | no motion |
| person not detected | stop in the same frame |
| `dist < 0.60 m` | retreat at 0.20 m/s |
| forward request while `dist < 0.85 m` | refused |
| capture ends / `/api/stop` | `{"stop": true}` |

### 21.5 Test results

`scripts/test_metric_camera_motion.py` now simulates three cases: a static
camera, a panning head with sway, and a panning head **plus a base that dollies
to hold the standoff**. Ground truth step length 0.59 m, 24 steps:

```text
lateral                body   cam-axis  steps  detector
  static camera        0.59 m   0.50     24    depth_body_frame
  panning head         0.59 m   0.46     24    depth_body_frame
  head + base follow   0.59 m   0.43     24    depth_body_frame

towards
  static camera        0.59 m   0.59     24    depth_body_frame
  panning head         0.59 m   0.59     24    depth_body_frame
  head + base follow   0.59 m   0.59     24    depth_body_frame
```

Step length error stays at 0.001 m and the step count stays at 24 while the
base drives; the old camera-axis estimate degrades to 0.43 m.

`scripts/test_health_depth.py` also checks the controller: no motion before a
height is measured, approach when too far, idle inside the deadband, retreat
when too close, stop when the person is lost, and the 0.85 m forward floor.

An end-to-end run of the real `run_capture()` with a synthetic camera confirmed
that a requested 1.0 m is raised to 1.53 m, the base is driven, and `stop` is
sent when the capture ends.

### 21.6 How to use

```bash
source scripts/env.sh voice
python scripts/health_station_web.py --port 8084 --follow --follow-distance 1.0
```

or in the UI: set **目标距离** and press **采集时跟随**, then **开始采集 10 秒**.
The button shows the effective standoff (e.g. `1.5 m`), not the requested one.
The Tk desktop app (`health_station_app.py`) has the same **采集时跟随** checkbox
and **目标距离** spinbox, plus the matching `--follow` / `--follow-distance`
flags.

Recommended sequence:

1. Stand 2-3 m away with the full body visible.
2. Press **自动取景** so the base reaches the working distance and the head
   aims at you.
3. Press **采集时跟随**, then **开始采集 10 s** and walk naturally.
4. The base keeps the standoff; if you walk toward it, it backs away at up to
   0.2 m/s. Remove obstacles behind the robot first — there is no rear sensor.

## 22. Joystick / gamepad teleop

`scripts/teleop_joy.py` drives the base from any Linux joystick. It uses the
same short-lease `/velocity` endpoint as the follower, refreshed at 20 Hz, so
releasing the stick, unplugging the controller or killing the script stops the
robot within 0.6 s.

```bash
source scripts/env.sh robot
python scripts/teleop_joy.py --list        # what is connected?
python scripts/teleop_joy.py --probe       # print raw axis/button numbers
python scripts/teleop_joy.py --connect     # connect paired Joy-Cons, then drive
python scripts/teleop_joy.py --takeover    # drive (turns follow off first)
python scripts/teleop_joy.py --single-stick # one Joy-Con: stick X/Y only
python scripts/teleop_joy.py --dry-run     # print velocities, no motion
```

Default mapping: left stick up/down = forward/back, right stick left/right =
turn, A = stop, B = emergency stop, Start = quit. Use `--axis-x`,
`--axis-theta`, `--invert-x`, `--invert-theta` to match a different pad.

With a Joy-Con pair the buttons do more than drive:

| Input | Action |
|---|---|
| left stick | forward/back + turn |
| left d-pad up/down | head tilt up/down |
| left d-pad left/right | head pan left/right |
| right **A** | stop (centre the stick to drive again) |
| right **B** | emergency stop |
| right **X** | cycle speed preset (slow → medium → fast) |
| right **Y** | health station auto-aim (自动取景) |
| right **R** shoulder | centre the head |
| right **ZR** trigger | start a 10 s capture |
| right **＋** | quit |
| right stick click | print robot + health status |

Each binding accepts a symbolic name (`R:A`) or a raw number (`3`):
`--btn-stop`, `--btn-estop`, `--btn-quit`, `--btn-speed`, `--btn-aim`,
`--btn-center`, `--btn-capture`, `--btn-status`. `--no-actions` disables all
of them and leaves only driving. Head steps are tuned with `--head-step`
(default 3 units per tick) and `--head-rate` (8 Hz).

`--takeover` turns off both the health station's 采集时跟随 and its head
tracking, so the joystick is in full control.

Every joystick node is opened at once, so a Joy-Con **pair** works as two
devices: left stick drives, right stick turns. The second device's numbers are
offset by 100 (`theta=100` on the `js` backend, `102` on `evdev`). A single
Joy-Con uses its one stick. Joy-Con sticks are auto-inverted so up = forward and
left = left; pass `--no-auto-invert` to keep the raw polarity. If the chosen
axis is not reported, the script prints the axes it did see and the matching
`--axis-x` / `--axis-theta` to pass.

Two backends:

* **js** (`/dev/input/js*`) — used automatically when present. Usually
  world-readable, no group change needed.
* **evdev** (`/dev/input/event*`) — needed for Bluetooth controllers such as a
  Nintendo Joy-Con. The user must be able to read the event node. Either add
  the user to the `input` group (then log out/in), or add a udev rule:

  ```bash
  echo 'SUBSYSTEM=="input", ENV{ID_INPUT_JOYSTICK}=="1", MODE="0660", GROUP="plugdev"' \
    | sudo tee /etc/udev/rules.d/99-xlerobot-joystick.rules
  sudo udevadm control --reload-rules && sudo udevadm trigger
  ```

Axis centres are learned at startup, so a trigger that rests at −1 does not
drive the base. If the reader thread dies (controller unplugged) the script
sends a stop instead of repeating the last command.

Pairing a Bluetooth Joy-Con:

```bash
bluetoothctl
> scan on
> pair <MAC>
> trust <MAC>
> connect <MAC>
```

After that, `teleop_joy.py --connect` reconnects both pads in one step. A
Joy-Con that has gone to sleep still has to be woken by pressing a button on
it; BlueZ cannot wake it, and reports `br-connection-create-socket` until then.

## 23. Official Joy-Con teleop (arms + head + base)

`XLeRobot/software/examples/7_xlerobot_2wheels_teleop_joycon.py` is the
upstream full-body Joy-Con teleop. Run it through the launcher so this robot's
geometry and ports are used and the upstream bus-overload bugs are worked
around:

```bash
# 1. one-time system setup (needs root)
sudo apt install -y libhidapi-hidraw0
sudo tee /etc/udev/rules.d/70-joycon.rules >/dev/null <<'RULE'
SUBSYSTEM=="hidraw", ATTRS{modalias}=="hid:b0005*v0000057Ep00002006", MODE="0666", TAG+="uaccess"
SUBSYSTEM=="hidraw", ATTRS{modalias}=="hid:b0005*v0000057Ep00002007", MODE="0666", TAG+="uaccess"
SUBSYSTEM=="hidraw", ATTRS{modalias}=="hid:b0005*v0000057Ep00002009", MODE="0666", TAG+="uaccess"
RULE
sudo udevadm control --reload-rules && sudo udevadm trigger
# 2. python side: the ctypes build, not the hidapi wheel (USB backend only)
uv pip install --python .venv-robot/bin/python hid PyGLM scipy
```

Then:

```bash
# robot_api owns the serial ports; stop it first
kill $(cat /tmp/opencode/robot_api.pid)

.venv-robot/bin/python scripts/teleop_joycon_official.py --check-only
.venv-robot/bin/python scripts/teleop_joycon_official.py --preflight
.venv-robot/bin/python scripts/teleop_joycon_official.py --fps 20
```

Control map (official):

| Input | Action |
|---|---|
| right Y / A | forward / backward (ramped) |
| right X / B | turn left / right (ramped) |
| right stick | right arm end-effector X/Z and Y |
| right R / stick click | right arm Z up / down |
| right ZR | toggle + ramp the right gripper |
| left stick | left arm end-effector |
| left L / stick click | left arm Z up / down |
| left ZL | toggle + ramp the left gripper |
| left d-pad | head tilt (up/down) and pan (left/right) |
| right ＋ | both arms and head to the zero pose |

Two upstream issues the launcher patches without editing the example:

1. The loop imports `precise_sleep` and accepts `--fps` but never calls either,
   so it spins at ~50 Hz. The launcher paces it through `log_rerun_data`
   (the last call in the loop) at `--fps`, default 20.
2. Each iteration calls `get_observation()` four times and `send_action()`
   reads three more, i.e. ~15 sync reads on a bus with eight motors. At 20 Hz
   bus1 (left arm + head) stopped answering after 1-2 minutes
   (`ConnectionError: no status packet`). The launcher serves all four calls
   from one snapshot per iteration, bringing it down to ~6 reads like
   `robot_api.py`.

`--preflight` opens both Joy-Cons, runs the gyro calibration and prints pose
and buttons without connecting to the robot.

Note: the official teleop and `robot_api.py` both need exclusive access to
`/dev/xlerobot_serial_a|b`, so the health station and voice robot commands are
unavailable while it runs. Restart `robot_api.py` afterwards:

```bash
nohup .venv-robot/bin/python scripts/robot_api.py --port 8090 \
  > /tmp/opencode/robot_api.log 2>&1 & echo $! > /tmp/opencode/robot_api.pid
```
