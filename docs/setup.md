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
Measure the actual wheelbase; the default 0.25 m is only a starting point.
Example override when running a LeRobot CLI:

```bash
--robot.wheel_radius=0.0635 --robot.wheelbase=<measured>
```

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

## 7. Next steps

1. Run calibration; measure wheelbase; record 10–20 teleop episodes.
2. Compare 0.6.1 vs 0.7 on the calibrated robot; keep 0.6.1 as main until 0.7
   passes calibration + teleop for at least 10 minutes.
3. Add a USB mic array/speaker; test 1–3 m ASR with servo noise.
4. Map the lab with RTAB-Map/Nav2.
