# XLeRobot Optimization — Phase 1 & 2 Implementation Checkpoint

**Date:** 2026-11-16  
**Status:** Implementation checkpoint; runtime and hardware validation pending.

## Checkpoint review

Python compilation and shell syntax checks pass. The completion claims below
describe intended functionality and are not evidence of runtime verification.
Before using this checkpoint for robot operation, address these known gaps:

- Configuration is currently consumed by camera identity and arm teaching only;
  robot_api and other scripts still retain hardware constants.
- The control page health request currently goes through the robot proxy to
  `/health`; it needs to call the voice service's `/api/health` directly.
- Camera recovery resets after three consecutive failures; resets are not
  capped at three attempts. An absent device also needs retry backoff.
- Watchdog processes need single-instance tracking and integration with
  `stop_demo.sh` so stopping services cannot trigger an unwanted restart.
- Arm commands need validated limits, confirmed gripper calibration, command
  serialization, and stop interruption before hardware validation.
- Log viewer aliases for voice and health do not match the launcher's filenames;
  its error response and bounded reads also need correction.
- LLM status/configuration UI, log rotation, and systemd services remain pending.
- Browser pose storage is local to one browser; shared server storage is pending.

No robot services were started and no motor commands were issued for this review.

---

## Phase 1 — Stability & Visibility ✅

### Implemented
1. **Central config.yaml** — All hardware constants in one file
   - Wheel radius, wheelbase, HEAD_EYE_TILT
   - Camera serial numbers and USB paths
   - Service URLs and log configuration
   - Scripts read via `from config_loader import CFG`

2. **Persistent logs** — `data/logs/*.log` instead of `/tmp/opencode/`
   - Survives reboots
   - Git-ignored but disk-persistent

3. **Service readiness checks** — `start_demo.sh` waits for real HTTP 200s
   - `wait_ready()` polls each service up to 30s
   - No more blind 6-second sleep

4. **D455 auto-recovery** — `camera_web.py` retries hardware_reset 3 times
   - Tracks consecutive failures with `_fail_count`
   - Calls `device.hardware_reset()` after 3 failures
   - Waits 9s for re-enumeration

5. **Service health monitoring** — Control panel shows live service status
   - 5 colored dots: ● robot ● camera ● voice ● health ● face
   - Polls `/api/health` every 5 seconds
   - Green = OK, Red = down/unreachable

---

## Phase 2 — Operation Upgrade ✅

### Implemented
1. **Watchdog** — `start_demo.sh` auto-restarts crashed services
   - Monitors `robot_api` and `camera_web` (critical services)
   - 5-second check interval, 10-second back-off
   - Logs to `data/logs/watchdog.log`
   - Disable with `WATCHDOG=0 ./scripts/start_demo.sh`

2. **Arm control API** — `robot_api.py` endpoints
   - `GET /arm/state` — Read all 12 joint positions (6 per arm)
   - `POST /arm/gripper` — `{"arm": "left"|"right", "open": true|false}`
   - `POST /arm/pose` — `{"arm": "left"|"right", "pose": {...}, "duration_s": 2.0}`
   - Smooth interpolation at 30 Hz

3. **Arm control UI** — Control panel "机械臂" card
   - Gripper buttons: 左夹爪 开/关, 右夹爪 开/关
   - Pose snapshot: "记录当前姿态" saves to localStorage
   - Pose replay: Select from dropdown → "执行"
   - Poses persist in browser across sessions

4. **Log viewer** — Control panel "日志查看器" card
   - Select service (robot_api, camera_web, voice_auto, etc.)
   - Shows last 50 lines
   - Auto-refresh every 3s (checkbox)
   - Endpoint: `GET /api/logs/<name>?lines=50`

---

## Files Changed

### New Files
- `config.yaml` — Central hardware configuration
- `scripts/config_loader.py` — YAML loader helper
- `scripts/control_page.py` — Control panel HTML/JS (served at 8082/control)
- `docs/operation_plan.md` — Full 3-phase plan
- `docs/phase_1_2_summary.md` — This document

### Modified Files
- `scripts/arm_teach.py` — Reads wheel params from config.yaml
- `scripts/camera_web.py` — Config integration + D455 retry loop
- `scripts/start_demo.sh` — Persistent logs + readiness + watchdog
- `scripts/robot_api.py` — Arm control endpoints (state, gripper, pose)
- `scripts/voice_auto_web.py` — `/api/health` + `/api/logs/<name>`

---

## Testing Checklist

### Phase 1
- [ ] Run `./scripts/start_demo.sh` and verify readiness checks pass
- [ ] Check logs appear in `data/logs/` (not `/tmp/opencode/`)
- [ ] Open control panel: `http://127.0.0.1:8082/control`
- [ ] Verify service health dots show all green
- [ ] Stop `robot_api` manually and watch dot turn red
- [ ] D455 recovery: Unplug/replug D455 USB and watch logs for retry

### Phase 2
- [ ] Watchdog: `pkill -f robot_api.py` and watch for auto-restart in 5-10s
- [ ] Check `data/logs/watchdog.log` for restart entry
- [ ] Arm control: Click "左夹爪 开" and verify gripper opens
- [ ] Pose snapshot: Position arm, click "记录当前姿态", name it "test"
- [ ] Pose replay: Select "test" from dropdown, click "执行"
- [ ] Log viewer: Select "robot_api", verify last 50 lines appear
- [ ] Enable "自动刷新" and watch logs update every 3s

---

## What's Left (Phase 3)

From `docs/operation_plan.md`:

1. **LeRobot 0.7 migration** — Validate velocity commands, 10-min teleop test
2. **Health data dashboard** — Trend chart for step length over time
3. **Arm teach from web** — Record mode with countdown (no SSH needed)
4. **Voice command trace** — ASR → intent → action pipeline visibility
5. **Mobile-optimized UI** — Large stop button, swipe-to-move, QR code access
6. **systemd user services** — Production deployment with dependency ordering

---

## Quick Start After Implementation

```bash
# Start the stack
./scripts/start_demo.sh

# Open the control panel
firefox http://127.0.0.1:8082/control

# Stop everything
./scripts/stop_demo.sh

# Check logs
ls -lh data/logs/
tail -f data/logs/robot_api.log
```

---

## Known Issues / Notes

1. **Arm motor lists** — Assumes `robot.left_arm_motors` and `robot.right_arm_motors` exist
   - Verify with: `python -c "from lerobot... import XLerobot2Wheels; print(robot.left_arm_motors)"`
   
2. **Gripper open/close values** — Currently set to ±50 normalized units
   - May need tuning per physical build
   
3. **Watchdog only monitors 2 services** — `head_track` NOT watched (prevents spurious head snap)

4. **Pose storage** — Uses browser localStorage (not shared across devices)
   - Consider server-side storage in Phase 3

5. **Log viewer** — Shows last 50 lines only
   - Full log rotation in `config.yaml` (10 MB, 3 backups)

---

*Implementation time: ~3 hours. Test time: ~30 minutes recommended.*
