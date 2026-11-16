# XLeRobot Optimized Operation Plan

**Date:** 2026-11-16  
**Scope:** Make the robot easier to operate daily and production-ready.

---

## 1. Current State — Honest Assessment

### What works well
- One-command start/stop (`start_demo.sh` / `stop_demo.sh`)
- Voice pipeline: VAD → SenseVoice → DeepSeek function-calling → TTS, with PipeWire AEC barge-in
- Head tracking: pixel-closed-loop, self-correcting direction signs, tilt guard, restores pose on exit
- Wrist cameras: stable by USB port path, no `/dev/videoN` guessing
- Control page (8082/control): motor scan, jog, ID change, motion reset, live video
- Safety: estop, follow latch, voice "停下" cannot be overridden by follow loop

### Real friction points today

| # | Problem | Impact |
|---|---------|--------|
| 1 | Config is scattered across 6+ files (`wheel_radius=0.0635`, `wheelbase=0.414`, `HEAD_EYE_TILT`, `WRIST_PATHS`, ...) | Hardware change = edit many files |
| 2 | No watchdog: if `robot_api.py` crashes, nothing restarts it | System silently breaks |
| 3 | `start_demo.sh` sleeps 6 s then prints URLs regardless of whether services actually started | Operator sees "ready" when nothing is up |
| 4 | Logs go to `/tmp/opencode/` — lost on reboot, same namespace as OpenCode | No history, confusing location |
| 5 | D455 can wedge (errno 5/16); camera_web.py already calls `hardware_reset()` once, but if it fails again the stream just 503s | Robot goes blind silently |
| 6 | Control page status shows robot_api state only; no visibility into whether camera_web, voice, or health service are alive | Operator cannot tell which of the 6 services is broken |
| 7 | Arm control is entirely absent from the web UI | Cannot verify arm positions from a phone/tablet |
| 8 | LLM credentials live in `~/.config/xlerobot/llm.env` with no UI; if the LLM call fails the robot falls back to keyword matching silently | Hard to notice when DeepSeek is unreachable |
| 9 | Three Python venvs (`.venv`, `.venv-robot`, `.venv-robot07`); which one to activate is non-obvious | Cognitive overhead for anyone new |
| 10 | `LeRobot 0.7` velocity commands not validated; kept as "test only" with no clear migration path | Version debt accumulates |

---

## 2. Improvement Plan — Three Phases

The changes below are ordered by: **how much they reduce daily operational friction** vs. **how much work they require**. Phase 1 is all quick wins; Phase 3 is best-in-class.

---

## Phase 1 — Stability and Visibility (2–3 days)

### 1.1 Central `config.yaml`

**Problem:** `wheel_radius=0.0635` and `wheelbase=0.414` appear in `robot_api.py`, `arm_teach.py`, `teleop_base.py`, `teleop_joycon_official.py`, `identify_wrist_cameras.py`. `HEAD_EYE_TILT` is only in an env var. `WRIST_PATHS` is in `camera_web.py`. There is no single truth.

**Solution:** Create `config.yaml` at the project root. Every Python file reads it at startup with a one-line helper.

```yaml
# config.yaml  — hardware constants for this physical build
robot:
  wheel_radius_m: 0.0635       # 5-inch wheels; default 0.05 is wrong
  wheelbase_m: 0.414           # measured 2026-11-10 by 80° turn test
  follow_standoff_m: 1.0       # target distance in follow mode
  head_eye_tilt: -63.6         # normalized tilt at eye level (measured)

cameras:
  head_serial: "333422300185"  # D455 serial
  wrist_left:  "pci-0000:00:14.0-usb-0:1.2:1.0-video-index0"
  wrist_right: "pci-0000:00:14.0-usb-0:2.2:1.0-video-index0"

services:
  robot_api:   "http://127.0.0.1:8090"
  camera_web:  "http://127.0.0.1:8080"
  voice_web:   "http://127.0.0.1:8082"
  health_web:  "http://127.0.0.1:8084"
  face_web:    "http://127.0.0.1:8086"

logs:
  dir: "data/logs"             # relative to project root; persistent across reboots
```

**Loader** (`scripts/config_loader.py`, ~15 lines):
```python
import yaml, os
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def load():
    with open(os.path.join(_ROOT, "config.yaml")) as f:
        return yaml.safe_load(f)
```

Each script does `from config_loader import load; CFG = load()` and replaces its hardcoded constants. One hardware change then propagates everywhere.

---

### 1.2 Persistent logs with rotation

**Problem:** Logs go to `/tmp/opencode/*.log` and are lost on reboot. When something went wrong at 3 AM, there is no record.

**Solution:**
1. Change `start_demo.sh` to write logs to `data/logs/` (the directory is git-ignored but persists on disk).
2. Use Python `logging.handlers.RotatingFileHandler` (10 MB, 3 backups) in services that have their own loggers.
3. For services launched via `setsid bash -c "..."`, redirect to `data/logs/<name>.log`.

**start_demo.sh change (3 lines):**
```bash
LOG="${LOG:-$(cd "$(dirname "$0")/.." && pwd)/data/logs}"
mkdir -p "$LOG"
```

Control page will add a **log tail** panel (Phase 2) that reads these files.

---

### 1.3 Service readiness check in `start_demo.sh`

**Problem:** `sleep 6` is too short when the robot is cold (motor bus scan takes 3–5 s, D455 init ~8 s), and silently passes even when a service failed to start.

**Solution:** Replace the sleep with a `wait_ready` function:

```bash
wait_ready() {
  local name=$1 url=$2 timeout=${3:-30}
  local t=0
  while ! curl -s -m 1 -o /dev/null "$url"; do
    sleep 1; t=$((t+1))
    [ $t -ge $timeout ] && echo "  WARNING: $name did not come up in ${timeout}s" && return 1
  done
  echo "  $name: ready (${t}s)"
}

wait_ready "robot_api"    "http://127.0.0.1:8090/status"  35
wait_ready "camera_web"   "http://127.0.0.1:8080/"         20
wait_ready "voice"        "http://127.0.0.1:8082/"         25
wait_ready "health"       "http://127.0.0.1:8084/"         20
```

This turns the "URLs are printed" moment into a real readiness signal, so the operator knows the robot is actually usable.

---

### 1.4 D455 auto-recovery loop in `camera_web.py`

**Problem:** `hardware_reset()` is called once when the stream fails to open. If it still fails after the reset, the stream returns 503 forever and the robot goes blind.

**Solution:** Retry the full open sequence (reset → wait 8 s → pipeline start) up to 3 times before giving up:

```python
# In HeadCamera.__init__ / _open():
MAX_RESETS = 3
for attempt in range(MAX_RESETS):
    try:
        self._start_pipeline()
        return  # success
    except Exception as e:
        if attempt < MAX_RESETS - 1 and rs is not None:
            print(f"D455: attempt {attempt+1} failed ({e}), hardware_reset…")
            self.device.hardware_reset()
            time.sleep(9)
        else:
            raise
```

This makes the most common D455 failure (errno 5/16 after a hot unplug) self-healing without operator involvement.

---

### 1.5 Service health in the control page

**Problem:** The control page status bar only shows robot_api state. The operator cannot tell whether camera_web is dead (causing silent 503s on video), or whether voice_auto_web is up.

**Solution:** Add a `/api/health` endpoint to `voice_auto_web.py` that probes all 5 sibling services:

```python
@app.get("/api/health")
def api_health():
    out = {}
    for name, url in [("robot_api", "8090/status"), ("camera_web", "8080/"),
                      ("health_web", "8084/"), ("face_web", "8086/state")]:
        try:
            r = requests.get(f"http://127.0.0.1:{url}", timeout=1.5)
            out[name] = {"ok": r.ok, "code": r.status_code}
        except Exception as e:
            out[name] = {"ok": False, "error": str(e)}
    out["voice_web"] = {"ok": True}  # self
    return jsonify(out)
```

Control page polls this every 5 s and shows a colored dot per service:

```
● robot_api  ● camera  ● voice  ● health  ● face
  (green)      (green)   (green)  (yellow)  (red)
```

Red = not responding; yellow = responding but unhealthy (e.g. camera 503). One glance tells the operator what is up.

---

## Phase 2 — Operation Upgrade (3–5 days)

### 2.1 Arm control in the control page

**Problem:** The control page can jog motors by raw encoder count, but that is a maintenance tool. Daily operation (verify arm is in a safe pose, open/close gripper) requires counting encoder ticks mentally.

**Solution:** Add an **Arms** card to the control page with:

1. **Gripper buttons** — "左夹爪 开 / 关" and "右夹爪 开 / 关". The gripper is the last joint; `arm_teach.py` already shows the full motion range; we map open=0° and closed=100° to the calibrated range.

2. **Pose snapshot** — a "记录当前姿态" button that reads all 12 arm joints (via `GET /motor/1/{1-6}` and `GET /motor/2/{1-6}`) and saves them to `data/poses/pose_<timestamp>.json`.

3. **Pose replay** — a dropdown of saved poses and a "执行" button that sends each joint to its goal position in order. This is `arm_teach.py replay` wired to the web UI.

The `/arm` endpoints needed in `robot_api.py`:
- `GET /arm/state` — returns normalized positions for all 12 arm joints
- `POST /arm/gripper` `{"arm": "left"|"right", "open": true|false}` — open or close
- `POST /arm/pose` `{"joints": {...}}` — replay a snapshot

This makes "verify arm is safe before demo" a one-tap operation.

---

### 2.2 Per-service watchdog

**Problem:** If `robot_api.py` exits (OOM, uncaught exception, serial disconnect), the whole robot is deaf and blind with no indication. The operator only notices when they try to give a voice command and get silence.

**Solution:** A lightweight watchdog loop inside `start_demo.sh` that runs in the background and respawns any service that exits:

```bash
watchdog() {
  local name=$1; shift
  while true; do
    if ! running "$name"; then
      echo "$(date): watchdog: $name exited, restarting" >> "$LOG/watchdog.log"
      launch "$name" "$@"
    fi
    sleep 5
  done
}

watchdog robot_api.py "source scripts/env.sh robot && exec python scripts/robot_api.py --port 8090" &
watchdog camera_web.py "exec .venv-robot/bin/python scripts/camera_web.py --port 8080" &
```

Add `watchdog.log` to the log tail viewer. For the hardware-bound services (robot_api, camera_web) this turns a silent crash into a 5-second blip.

**Note:** Do not auto-restart `head_track.py` — a spurious restart during a session would snap the head. It should stay manual or require explicit re-enabling from the control panel.

---

### 2.3 LLM status and config in the control page

**Problem:** When DeepSeek is unreachable (network, quota), the robot silently falls back to keyword matching. The operator does not know, so conversations that need natural language understanding appear broken.

**Solution:**
1. `GET /api/status` already exists; add `"llm_ok": bool, "llm_model": str, "llm_latency_ms": int` to its response.
2. Control page shows a **LLM** row in the status bar: `● DeepSeek deepseek-chat 340ms` or `● 离线 (关键词模式)`.
3. Add a small **设置** panel (collapsible) to the control page for LLM URL and model — writes to `~/.config/xlerobot/llm.env` on save. No more SSH to edit a file.

---

### 2.4 Log tail viewer in the control page

**Problem:** When something goes wrong, the operator must SSH in and `tail -f /tmp/opencode/*.log`. That is a barrier for non-technical caregivers or during a demo.

**Solution:** A `GET /api/logs/<name>?lines=50` endpoint in `voice_auto_web.py`:

```python
@app.get("/api/logs/<name>")
def api_logs(name):
    allowed = {"robot_api", "camera_web", "voice_auto", "health_station", "head_track", "watchdog"}
    if name not in allowed:
        return jsonify({"error": "unknown log"}), 400
    path = os.path.join(LOG_DIR, f"{name}.log")
    if not os.path.exists(path):
        return jsonify({"lines": []})
    n = min(int(request.args.get("lines", 50)), 200)
    with open(path) as f:
        lines = f.readlines()
    return jsonify({"lines": lines[-n:]})
```

The control page gets a **日志** tab at the bottom: select service, see last 50 lines, auto-refresh every 3 s. Errors are highlighted red. No more SSH for a quick look.

---

### 2.5 systemd user service for production

**Problem:** `setsid bash -c "..."` is fragile. It does not survive a user re-login. It has no dependency ordering (camera_web must start before head_track; robot_api before voice). It offers no `journalctl` integration.

**Solution:** Add `scripts/install_systemd.sh` that installs user-level systemd units:

```
xlerobot-robot-api.service    After=network.target
xlerobot-camera-web.service   After=xlerobot-robot-api.service
xlerobot-voice.service        After=xlerobot-camera-web.service xlerobot-robot-api.service
xlerobot-health.service       After=xlerobot-voice.service
xlerobot-face.service         After=xlerobot-voice.service
xlerobot-head-track.service   After=xlerobot-camera-web.service xlerobot-robot-api.service
xlerobot.target               Wants=all of the above
```

Then:
```bash
systemctl --user start xlerobot.target     # start everything
systemctl --user stop xlerobot.target      # stop everything
systemctl --user status xlerobot-robot-api # check one service
journalctl --user -u xlerobot-robot-api -f # follow logs
```

The `start_demo.sh` / `stop_demo.sh` scripts stay as a compatibility wrapper but call `systemctl` internally when the units are installed.

---

## Phase 3 — Best-in-Class (1 week)

### 3.1 LeRobot 0.7 migration

**Status:** `.venv-robot07` passes `connect/configure/disconnect`, calibration restore, and normalized joint reads. Pending: wheel velocity commands and 10-minute teleop.

**Plan:**
1. Run the velocity test: `python scripts/test_wheel_speed.py --env robot07` — this sends a 0.1 m/s forward command for 2 s and measures actual wheel ticks. If the counts match 0.6.1 within 5%, the migration is safe.
2. Run 10-minute teleop with Joy-Con.
3. If both pass: flip `start_demo.sh` to use `.venv-robot07`, archive `.venv-robot07` → `.venv-robot`, delete the old one.
4. Remove the `# test only` comments in the README.

**Benefit:** LeRobot 0.7 fixes several LeRobot internal APIs; staying on 0.6.1 means every future `pip install` requires a constraint pin.

---

### 3.2 Health data dashboard

**Problem:** Health captures produce JSON files in `data/health/` but the only way to review trends is to open the files manually or use `health_station_web.py`.

**Solution:** A `/health/history` page in `health_station_web.py` that:
- Lists the last 20 captures with date, standing distance, step length, step count
- Shows a simple trend chart (Chart.js, CDN) of step length over time
- Flags sessions where step length dropped >15% vs. the 7-day average (early fall-risk signal)

This turns the health station from a "capture one session" tool into a longitudinal care record — which is the actual use case for elder-care.

---

### 3.3 Arm teach-and-replay from the control page

**Problem:** `arm_teach.py` exists and works from the CLI, but requires SSH. During a demo or care session you cannot teach the robot a pose without a keyboard.

**Solution:**

1. **Record mode:** Control page → Arms → "录制姿态" button. The page enters a 5-second countdown during which the operator manually positions the arm (torque off). At zero, positions are snapped and saved to `data/poses/`.

2. **Replay mode:** Dropdown of saved poses → "执行" button. robot_api receives the pose as a `/arm/pose` command and interpolates to it over 2 s.

3. **Named poses:** "home" (safe park), "greeting" (slight wave), "handoff" (extend arm for cup). These become voice-triggerable: "把手伸出来" maps to `arm_pose{"name": "handoff"}` in `agent_brain.py`.

---

### 3.4 Voice command visualizer

**Problem:** When the robot misunderstands a command, the operator cannot tell whether the problem was ASR (the wrong text), intent routing (right text, wrong action), or the LLM (right intent, wrong arguments).

**Solution:** Add a **command trace** panel to the companion page (8082):
- Last heard: `往前走二十厘米`
- ASR engine: SenseVoice, confidence 0.94
- Decision: `move_robot { forward_m: 0.2 }` via llm-tool
- Executed: 200 OK, 已停稳

Each field is color-coded: red if below threshold or failed. This makes debugging a 10-second task instead of grepping logs.

---

### 3.5 Mobile-optimized control page

**Problem:** The control page works on a laptop but the font sizes and button targets are too small for a phone. Caregivers should be able to estop the robot from their phone in a hallway.

**Solution:**
- Large-tap zone for 停止 (at least 80×80 px, red, always visible at top)
- Swipe-to-move instead of a d-pad on mobile (swipe up = forward, etc.)
- Voice command input for text chat
- QR code on the robot's face display that opens the control page

---

## 3. Execution Order (Recommended)

```
Week 1 (Phase 1):
  Day 1  config.yaml + loader, update all scripts
  Day 2  persistent logs, start_demo.sh readiness check
  Day 3  D455 auto-recovery loop, service health in control page

Week 2 (Phase 2):
  Day 1–2  Arm control (gripper + pose) in control page
  Day 3    Watchdog in start_demo.sh
  Day 4    LLM status + log tail in control page
  Day 5    systemd unit files + install script

Week 3 (Phase 3):
  Day 1–2  LeRobot 0.7 velocity test + migration (if passing)
  Day 3    Health trend dashboard
  Day 4    Arm teach-and-replay in control page
  Day 5    Voice trace panel + mobile layout
```

---

## 4. Files Changed Per Phase

### Phase 1

| File | Change |
|------|--------|
| `config.yaml` (new) | All hardware constants |
| `scripts/config_loader.py` (new) | 15-line YAML loader |
| `scripts/robot_api.py` | Read wheel_radius, wheelbase, HEAD_EYE_TILT from config |
| `scripts/arm_teach.py` | Read wheel params from config |
| `scripts/teleop_base.py` | Same |
| `scripts/teleop_joycon_official.py` | Same |
| `scripts/camera_web.py` | Read wrist paths + D455 serial from config; D455 retry loop |
| `scripts/start_demo.sh` | Persistent log dir; `wait_ready` function |
| `scripts/voice_auto_web.py` | `GET /api/health` endpoint |
| `scripts/control_page.py` | Service health dots in status bar |

### Phase 2

| File | Change |
|------|--------|
| `scripts/robot_api.py` | `GET /arm/state`, `POST /arm/gripper`, `POST /arm/pose` |
| `scripts/voice_auto_web.py` | `GET /api/logs/<name>`, LLM status in `GET /api/status` |
| `scripts/control_page.py` | Arms card, log tail panel, LLM status |
| `scripts/start_demo.sh` | Watchdog background loops |
| `scripts/install_systemd.sh` (new) | systemd user units |
| `scripts/systemd/` (new dir) | 6 `.service` files + 1 `.target` |

### Phase 3

| File | Change |
|------|--------|
| `scripts/agent_brain.py` | Add `arm_pose` tool |
| `scripts/voice_web.py` | Handle `arm_pose` action |
| `scripts/robot_api.py` | `POST /arm/pose` interpolation |
| `scripts/health_station_web.py` | `/health/history` page, trend chart |
| `scripts/control_page.py` | Teach-and-replay UI, mobile layout |
| `scripts/voice_auto_web.py` | Command trace in `/api/status` |
| `scripts/start_demo.sh` | Use systemd when units are installed |

---

## 5. What This Gets You

After Phase 1:
- Hardware change = edit 1 file (not 6)
- Crashes leave a log you can read tomorrow
- Operator knows immediately which service is broken, not "it felt slow"
- D455 recovers itself without SSH

After Phase 2:
- Arm safe-park and gripper control from any phone on the LAN
- Any crashed service restarts automatically in 5 s
- LLM failure is visible, not silent
- Full log access in the browser

After Phase 3:
- Voice commands are debuggable in 10 seconds
- Health data shows trends over weeks, not just today's capture
- Robot can be taught new arm poses without touching a keyboard
- LeRobot is on a supported version
- Control page works on a phone with one hand

---

*This plan is sized for one developer working the robot daily. Each phase is independently mergeable — stopping after Phase 1 already removes most of the daily operational friction.*
