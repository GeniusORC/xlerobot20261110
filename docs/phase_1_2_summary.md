# XLeRobot Optimization — Phase 1 & 2 Implementation Checkpoint

**Date:** 2026-11-16  
**Status:** Software implementation and hardware-free route checks complete; runtime deployment and hardware validation pending.

## Checkpoint review

Python compilation, shell syntax, `git diff --check`, and focused Flask test-client
requests pass. `pytest` is not installed in either project environment. No robot
services were started and no motor commands were issued during these checks.

Remaining gates before operational use:
- Physical gripper direction and arm range must be calibrated; config currently
  reflects upstream RANGE_0_100 but does not establish which end is physically open.
- Test D455 recovery with the actual camera and USB topology.
- Run LeRobot 0.7 wheel-velocity and 10-minute teleop tests before changing the
  production environment from 0.6.1.
- Install/test the user systemd unit in the target graphical login session.

---

## Phase 1 — Stability & Visibility ✅

### Implemented
1. **Central config.yaml** — Runtime hardware and service settings centralized
   - Wheel radius, wheelbase, HEAD_EYE_TILT
   - Camera serial numbers and USB paths
   - Service URLs and log configuration
   - Robot API, camera, teleop, tracking and support scripts read via `CFG`

2. **Persistent logs** — `data/logs/*.log` instead of `/tmp/opencode/`
   - Survives reboots
   - Git-ignored but disk-persistent

3. **Service readiness checks** — `start_demo.sh` polls actual endpoints for up to 60s

4. **D455 auto-recovery** — bounded hardware-reset attempts with backoff
   - Hardware behavior remains unverified on this host

5. **Service health monitoring** — Control panel shows live service status
   - 5 colored dots: ● robot ● camera ● voice ● health ● face
   - Polls `/api/health` every 5 seconds
   - Green = OK, Red = down/unreachable

---

## Phase 2 — Operation Upgrade ✅

### Implemented
1. **Supervisor** — `demo_manager.py` owns child processes, logs, restart backoff,
   single-instance locking, and stop. Head tracking is not restarted after failure.

2. **Arm control API** — `robot_api.py` endpoints
   - `GET /arm/state` — Read all 12 joint positions (6 per arm)
   - `POST /arm/gripper` — `{"arm": "left"|"right", "open": true|false}`
   - `POST /arm/pose` — `{"arm": "left"|"right", "pose": {...}, "duration_s": 2.0}`
   - Smooth interpolation at 30 Hz

3. **Arm control UI** — Control panel "机械臂" card
   - Gripper buttons: 左夹爪 开/关, 右夹爪 开/关
   - Pose snapshots persist server-side under `data/poses`
   - Replay validates the full snapshot before issuing motion

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
- `scripts/demo_manager.py` — Single-instance supervised child processes and logs
- `scripts/service_readiness.py` — Endpoint-based startup readiness
- `scripts/systemd/` — Optional user service template and installer
- `scripts/start_demo.sh` — Supervisor launch and readiness
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

## What's Left / Hardware Gates

From `docs/operation_plan.md`:

1. **LeRobot 0.7 migration** — Wheel-velocity and 10-minute teleop validation
2. **Arm teach from web** — Record mode with countdown (no SSH needed)
3. **Mobile usability** — Validate on target phone/tablet and improve touch layout
4. **Deployment** — Install and verify user systemd service in target session

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

1. Arm motor limits and gripper direction still require physical calibration.
2. Follow/head tracking is opt-in and is not automatically restarted after failure.
3. Runtime service startup, systemd behavior, D455 recovery, and LeRobot 0.7
   migration remain unverified on the physical deployment target.

---

*Implementation time: ~3 hours. Test time: ~30 minutes recommended.*
