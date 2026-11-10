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
| `left_wrist` | `/dev/video0` | 640x480@30 | left wrist |
| `right_wrist` | `/dev/video2` | 640x480@30 | right wrist |
| `head` | `/dev/video6` | 640x480@30 | head, currently aimed downward |

Verify all three simultaneously:

```bash
source scripts/env.sh robot
python scripts/test_cameras.py
```

Verified: all three open together, `get_observation()` returns
`left_wrist`, `right_wrist`, `head` as `(480, 640, 3) uint8`, and 20 rounds of
three frames take ~0.75 s (~26.5 rounds/s total).

The head camera currently sees the arms/cart from above; tilt it up if it should
see a person's body for approach/gait work. Without D455 depth/IMU, navigation
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
