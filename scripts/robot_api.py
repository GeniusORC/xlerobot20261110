#!/usr/bin/env python3
"""Small HTTP robot-control service for the voice web demo.

Run in the robot environment:
  source scripts/env.sh robot
  python scripts/robot_api.py --port 8090

Endpoints:
  GET  /status
  POST /command   {"intent": "stop"|"come"|"report"|...}
  GET  /head                        head pan/tilt (normalized -100..100)
  POST /head      {"pan":..,"tilt":..} or {"dpan":..,"dtilt":..} or {"center":true}
  POST /drive     {"x":m/s,"theta":deg/s,"duration":s}  bounded fine positioning
  POST /follow    {"on":true|false}  enable/revoke follow mode (required by /velocity)
  POST /velocity  {"x":..,"theta":..} short-lease continuous following
  POST /estop
"""
import argparse
import builtins
import queue
import threading
import time
import traceback

import cv2
import numpy as np
import requests
from flask import Flask, jsonify, request

from lerobot_robot_xlerobot_2wheels import XLerobot2Wheels, XLerobot2WheelsConfig

app = Flask(__name__)
STATE = {
    "connected": False,
    "busy": False,
    "last_intent": None,
    "message": "starting",
    "x_vel": 0.0,
    "theta_vel": 0.0,
    "follow": False,
    "updated": time.time(),
}
STATE_LOCK = threading.Lock()
COMMANDS = queue.Queue()
STOP_EVENT = threading.Event()
ROBOT = None
SHUTDOWN = False
BUS_LOCK = threading.Lock()

# Head pan/tilt are ordinary STS3215 position servos on bus1 (IDs 7 and 8).
# Values are normalized to [-100, 100]; 0 is the calibration midpoint.
#
# Every command moves at most MAX_STEP normalized units, so the head can never
# make a violent jump even when it is far from the requested pose.  The only
# real envelope is the calibrated motor range HARD_LIMIT; an earlier version
# also clamped to +-30 around the pose captured at connect time, which trapped
# the camera looking at the ceiling (tilt could only move one way) and made
# full-body framing impossible.
PAN_MOTOR, TILT_MOTOR = "head_motor_1", "head_motor_2"
PAN_SPAN = 98.0               # full calibrated travel for both axes
TILT_SPAN = 98.0
HARD_LIMIT = (-98.0, 98.0)    # normalized motor range, 0 = calibration mid
MAX_STEP = 8.0                # max normalized units per command
HEAD_REF = {"pan": None, "tilt": None}

# Short-lease velocity command used by continuous following. The base keeps
# moving only while the health station keeps refreshing this lease. Approaching
# a person is slower than retreating from one.
VEL = {"x": 0.0, "theta": 0.0, "lease": 0.0}
VEL_LIMIT = (-0.22, 0.30)   # forward raised so a normal walk can be followed

# Following is an explicit mode, not just a stream of velocity commands.  The
# 0.6 s lease already stops the base if the client dies; this latch stops it
# when a *live* client is overridden by a human: /command stop, /estop and the
# voice "停下" all switch it off, so the follow loop cannot immediately resume
# driving after the user asked it to stop.
FOLLOW = {"on": False, "seen": 0.0}


def set_state(**kwargs):
    with STATE_LOCK:
        STATE.update(kwargs)
        STATE["updated"] = time.time()


def get_state():
    with STATE_LOCK:
        return dict(STATE)


def connect_robot(cfg):
    for attempt in range(5):
        robot = XLerobot2Wheels(cfg)
        _orig = builtins.input
        builtins.input = lambda *a, **k: ""
        try:
            robot.connect(calibrate=False)
            return robot
        except Exception as e:
            print(f"connect attempt {attempt + 1} failed: {type(e).__name__}: {e}")
            try:
                robot.disconnect()
            except Exception:
                pass
            time.sleep(1)
        finally:
            builtins.input = _orig
    return None


def _rmw(fn, *a, **kw):
    """Run a serial-bus operation under the shared bus lock."""
    with BUS_LOCK:
        return fn(*a, **kw)


def _read_head():
    vals = _rmw(ROBOT.bus1.sync_read, "Present_Position", [PAN_MOTOR, TILT_MOTOR])
    return float(vals.get(PAN_MOTOR, 0.0)), float(vals.get(TILT_MOTOR, 0.0))


def _ensure_head_ref():
    """Remember where the head was when the service first touched it."""
    if HEAD_REF["pan"] is None or HEAD_REF["tilt"] is None:
        pan, tilt = _read_head()
        HEAD_REF["pan"], HEAD_REF["tilt"] = pan, tilt
    return HEAD_REF["pan"], HEAD_REF["tilt"]


def _head_window(axis):
    ref = HEAD_REF["pan"] if axis == "pan" else HEAD_REF["tilt"]
    span = PAN_SPAN if axis == "pan" else TILT_SPAN
    if ref is None:
        return HARD_LIMIT
    return (max(HARD_LIMIT[0], ref - span),
            min(HARD_LIMIT[1], ref + span))


def _set_head(pan=None, tilt=None, dpan=None, dtilt=None, center=False):
    """Move the head without ever making a large jump.

    * Absolute ``pan``/``tilt`` are in the calibration frame (0 = midpoint)
      and clamped to a window around the reference pose.
    * ``dpan``/``dtilt`` are relative to the current position.
    * ``center`` ramps smoothly back to 0 and re-references the head there.
    * Each step moves at most ``MAX_STEP`` normalized units.
    """
    pan0, tilt0 = _read_head()
    _ensure_head_ref()
    if center:
        target_pan, target_tilt = 0.0, 0.0
        steps, pause = 80, 0.05
    else:
        if pan is None and dpan is not None:
            pan = pan0 + float(dpan)
        if tilt is None and dtilt is not None:
            tilt = tilt0 + float(dtilt)
        lo, hi = _head_window("pan")
        target_pan = None if pan is None else float(np.clip(pan, lo, hi))
        lo, hi = _head_window("tilt")
        target_tilt = None if tilt is None else float(np.clip(tilt, lo, hi))
        steps, pause = 1, 0.0

    moved = False
    for _ in range(steps):
        if STOP_EVENT.is_set():
            break
        pan_now, tilt_now = _read_head()
        dp = (0.0 if target_pan is None
              else float(np.clip(target_pan - pan_now, -MAX_STEP, MAX_STEP)))
        dt = (0.0 if target_tilt is None
              else float(np.clip(target_tilt - tilt_now, -MAX_STEP, MAX_STEP)))
        action = {}
        if abs(dp) > 0.1:
            action[f"{PAN_MOTOR}.pos"] = float(np.clip(pan_now + dp, *HARD_LIMIT))
        if abs(dt) > 0.1:
            action[f"{TILT_MOTOR}.pos"] = float(np.clip(tilt_now + dt, *HARD_LIMIT))
        if not action:
            break
        _rmw(ROBOT.send_action, action)
        moved = True
        if pause:
            time.sleep(pause)
    if center:
        HEAD_REF["pan"], HEAD_REF["tilt"] = 0.0, 0.0
    return {"pan": target_pan, "tilt": target_tilt, "moved": moved}


def _stop():
    STOP_EVENT.set()
    try:
        ROBOT.stop_base()
    except Exception:
        pass
    set_state(busy=False, message="已停止", x_vel=0.0, theta_vel=0.0)


def _move(x: float, theta: float, duration: float, message: str):
    STOP_EVENT.clear()
    set_state(busy=True, message=message)
    deadline = time.time() + duration
    try:
        while time.time() < deadline and not STOP_EVENT.is_set():
            _rmw(ROBOT.send_action, {"x.vel": x, "theta.vel": theta})
            time.sleep(0.05)
        ROBOT.stop_base()
        set_state(busy=False, message="已停止" if STOP_EVENT.is_set() else message + "，已停稳",
                  x_vel=0.0, theta_vel=0.0)
    except Exception as e:
        set_state(busy=False, message=f"动作失败: {e}")


def _apply_velocity():
    """Apply the leased velocity, or stop when it expires. True if applied."""
    if not VEL["lease"]:
        return False
    if not FOLLOW["on"] or STOP_EVENT.is_set() or time.time() > VEL["lease"]:
        VEL["lease"] = 0.0
        try:
            ROBOT.stop_base()
        except Exception:
            pass
        return False
    try:
        _rmw(ROBOT.send_action, {"x.vel": VEL["x"], "theta.vel": VEL["theta"]})
        return True
    except Exception:
        VEL["lease"] = 0.0
        return False


def _drive(x: float, theta: float, duration: float):
    """Bounded fine positioning used by the health station auto-framing."""
    x = float(np.clip(x, -0.08, 0.08))
    theta = float(np.clip(theta, -25.0, 25.0))
    duration = float(np.clip(duration, 0.05, 3.0))
    _move(x, theta, duration, "正在微调位置")


def _move_relative(forward_m: float = 0.0, turn_deg: float = 0.0):
    """Measured move for spoken commands ("往前走三十厘米", "左转九十度").

    There are no wheel encoders we trust for closed-loop odometry, so the
    distance is turned into a duration at a fixed speed.  Turning happens
    first so the drive is in the direction the user asked for.
    """
    turn_deg = float(np.clip(turn_deg, -180.0, 180.0))
    forward_m = float(np.clip(forward_m, -2.0, 2.0))

    if abs(turn_deg) >= 5.0:
        rate = 20.0  # deg/s
        _move(0.0, rate if turn_deg > 0 else -rate,
              min(abs(turn_deg) / rate, 9.0),
              "正在左转" if turn_deg > 0 else "正在右转")

    if abs(forward_m) >= 0.03:
        speed = 0.20 if forward_m > 0 else 0.16
        dur = min(abs(forward_m) / speed, 12.0)
        _move(speed if forward_m > 0 else -speed, 0.0, dur,
              "正在前进" if forward_m > 0 else "正在后退")


def _look_at_person():
    """Rotate the base to center the largest detected face in the head camera."""
    STOP_EVENT.clear()
    set_state(busy=True, message="正在寻找您")
    cascade = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    )
    deadline = time.time() + 6.0
    found = False
    try:
        while time.time() < deadline and not STOP_EVENT.is_set():
            try:
                r = requests.get("http://127.0.0.1:8080/snapshot/head", timeout=1)
                r.raise_for_status()
                img = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
            except Exception:
                img = None
            if img is not None:
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                faces = cascade.detectMultiScale(gray, 1.1, 5, minSize=(40, 40))
                if len(faces):
                    found = True
                    x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
                    cx = x + w / 2.0
                    err = (cx - img.shape[1] / 2.0) / img.shape[1]
                    if h > img.shape[0] * 0.55:
                        ROBOT.stop_base()
                        set_state(busy=False, message="看到您了，离得比较近", x_vel=0.0, theta_vel=0.0)
                        return
                    if abs(err) < 0.12:
                        ROBOT.stop_base()
                        set_state(busy=False, message="已经面向您了", x_vel=0.0, theta_vel=0.0)
                        return
                    theta = max(-12.0, min(12.0, -err * 60.0))
                    _rmw(ROBOT.send_action, {"x.vel": 0.0, "theta.vel": theta})
            time.sleep(0.12)
        ROBOT.stop_base()
        set_state(busy=False, message="已经面向您了" if found else "没有看到您的脸",
                  x_vel=0.0, theta_vel=0.0)
    except Exception as e:
        ROBOT.stop_base()
        set_state(busy=False, message=f"看人失败: {e}")


def worker():
    global ROBOT
    last = time.time()
    while not SHUTDOWN:
        intent = None
        try:
            intent = COMMANDS.get(timeout=0.05)
        except queue.Empty:
            pass

        if intent is None:
            if _apply_velocity():
                continue
            # Idle: make sure the base is stopped.
            if time.time() - last > 0.5:
                try:
                    ROBOT.stop_base()
                except Exception:
                    pass
                last = time.time()
            continue

        if intent == "stop":
            _stop()
            continue
        if isinstance(intent, dict):
            kind = intent.get("type")
            if kind == "head":
                # ``stop_robot()`` (called before every capture) sets
                # STOP_EVENT, and nothing else clears it.  Without this the
                # first head command after a capture aborts immediately and
                # the camera never moves again.
                STOP_EVENT.clear()
                try:
                    res = _set_head(pan=intent.get("pan"),
                                    tilt=intent.get("tilt"),
                                    dpan=intent.get("dpan"),
                                    dtilt=intent.get("dtilt"),
                                    center=bool(intent.get("center")))
                    if intent.get("center"):
                        msg = "头部已回中"
                    elif res["moved"]:
                        msg = "头部已调整"
                    else:
                        msg = "头部已到位"
                    set_state(busy=False, message=msg)
                except Exception as e:
                    set_state(message=f"头部调整失败: {e}")
                continue
            if kind == "drive":
                _drive(float(intent.get("x", 0.0)),
                       float(intent.get("theta", 0.0)),
                       float(intent.get("duration", 0.5)))
                continue
            if kind == "move":
                _move_relative(float(intent.get("forward_m", 0.0)),
                               float(intent.get("turn_deg", 0.0)))
                continue
            continue
        if intent == "report":
            set_state(busy=True, last_intent="report", message="正在读取状态")
            try:
                vel = _rmw(ROBOT.bus2.sync_read, "Present_Velocity",
                           ROBOT.base_motors, normalize=False)
                set_state(busy=False, message="报告完成",
                          x_vel=float(vel.get("base_left_wheel", 0)),
                          theta_vel=float(vel.get("base_right_wheel", 0)))
            except Exception as e:
                set_state(busy=False, message=f"读取失败: {e}")
            continue
        if intent == "come":
            _move(0.05, 0.0, 4.0, "正在靠近")
            continue
        if intent == "forward":
            _move(0.05, 0.0, 4.0, "正在前进")
            continue
        if intent == "backward":
            _move(-0.05, 0.0, 4.0, "正在后退")
            continue
        if intent == "turn_left":
            _move(0.0, 20.0, 4.5, "正在左转")
            continue
        if intent == "turn_right":
            _move(0.0, -20.0, 4.5, "正在右转")
            continue
        if intent == "look":
            _look_at_person()
            continue


@app.get("/status")
def status():
    out = get_state()
    # Whether a follow client (head_track --follow) is actually talking to us.
    # The latch alone is not enough: the voice reply must not promise to
    # follow when nothing is driving.
    out["follow_client"] = (time.time() - FOLLOW["seen"]) < 3.0
    return jsonify(out)


@app.post("/command")
def command():
    data = request.get_json(force=True, silent=True) or {}
    intent = str(data.get("intent", "")).strip().lower()
    if intent not in ("stop", "come", "report", "forward", "backward", "turn_left", "turn_right", "look"):
        return jsonify({"error": "unknown intent"}), 400
    if ROBOT is None or not get_state().get("connected"):
        return jsonify({"error": "robot not connected", **get_state()}), 503
    if intent == "stop":
        STOP_EVENT.set()
        # A human stop also ends follow mode; otherwise the follow loop would
        # refresh the velocity lease 100 ms later and drive on.
        FOLLOW["on"] = False
        set_state(follow=False)
    COMMANDS.put(intent)
    return jsonify({"queued": intent, **get_state()})


@app.get("/head")
def head_status():
    if ROBOT is None or not get_state().get("connected"):
        return jsonify({"error": "robot not connected", **get_state()}), 503
    try:
        pan, tilt = _read_head()
        _ensure_head_ref()
        pan_window = _head_window("pan")
        tilt_window = _head_window("tilt")
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
    return jsonify({"pan": pan, "tilt": tilt,
                    "pan_ref": HEAD_REF["pan"], "tilt_ref": HEAD_REF["tilt"],
                    "pan_window": list(pan_window),
                    "tilt_window": list(tilt_window),
                    "hard_limit": list(HARD_LIMIT),
                    "max_step": MAX_STEP})


@app.post("/head")
def head_set():
    data = request.get_json(force=True, silent=True) or {}
    if ROBOT is None or not get_state().get("connected"):
        return jsonify({"error": "robot not connected", **get_state()}), 503
    if not any(k in data for k in ("pan", "tilt", "dpan", "dtilt", "center")):
        return jsonify({"error": "need pan/tilt/dpan/dtilt/center"}), 400
    COMMANDS.put({"type": "head", **data})
    return jsonify({"queued": "head", **data})


@app.post("/drive")
def drive():
    data = request.get_json(force=True, silent=True) or {}
    if ROBOT is None or not get_state().get("connected"):
        return jsonify({"error": "robot not connected", **get_state()}), 503
    cmd = {"type": "drive",
           "x": float(data.get("x", 0.0)),
           "theta": float(data.get("theta", 0.0)),
           "duration": float(data.get("duration", 0.5))}
    COMMANDS.put(cmd)
    return jsonify({"queued": "drive", **cmd})


@app.post("/move")
def move():
    """Measured move: {"forward_m": 0.3, "turn_deg": 90}.

    forward_m is positive forward, turn_deg is positive left.  This is what
    spoken commands like "往前走三十厘米" or "左转九十度" map onto; it is
    deliberately separate from /drive, which is limited to small corrections
    for the health-station auto-framing.
    """
    data = request.get_json(force=True, silent=True) or {}
    if ROBOT is None or not get_state().get("connected"):
        return jsonify({"error": "robot not connected", **get_state()}), 503
    try:
        fwd = float(data.get("forward_m", 0.0) or 0.0)
        turn = float(data.get("turn_deg", 0.0) or 0.0)
    except (TypeError, ValueError):
        return jsonify({"error": "forward_m and turn_deg must be numbers"}), 400
    if abs(fwd) < 0.01 and abs(turn) < 1.0:
        return jsonify({"error": "need forward_m or turn_deg"}), 400
    fwd = float(np.clip(fwd, -2.0, 2.0))
    turn = float(np.clip(turn, -180.0, 180.0))
    COMMANDS.put({"type": "move", "forward_m": fwd, "turn_deg": turn})
    return jsonify({"queued": "move", "forward_m": fwd, "turn_deg": turn})


@app.post("/velocity")
def velocity():
    """Short-lease base velocity for continuous following.

    The base stops by itself if this is not refreshed within 0.6 s, so a
    crashed client cannot leave the robot driving.
    """
    data = request.get_json(force=True, silent=True) or {}
    if ROBOT is None or not get_state().get("connected"):
        return jsonify({"error": "robot not connected", **get_state()}), 503
    if data.get("stop"):
        VEL["lease"] = 0.0
        VEL["x"] = VEL["theta"] = 0.0
        COMMANDS.put("stop")
        return jsonify({"ok": True, "stopped": True})
    if not FOLLOW["on"]:
        # 409, not 403: the client is allowed to ask, it just has to enable
        # follow mode first (which a stop command revokes).
        return jsonify({"error": "follow is off", "follow": False}), 409
    FOLLOW["seen"] = time.time()
    x = float(np.clip(float(data.get("x", 0.0)), *VEL_LIMIT))
    theta = float(np.clip(float(data.get("theta", 0.0)), -25.0, 25.0))
    STOP_EVENT.clear()
    VEL.update(x=x, theta=theta, lease=time.time() + 0.6)
    return jsonify({"ok": True, "x": x, "theta": theta,
                    "lease_ms": 600})


@app.post("/follow")
def follow():
    """Enable or revoke follow mode.

    Follow mode is what /velocity requires.  It is revoked by /estop and by the
    "stop" command, so the user always has the last word even while a follow
    loop is running.
    """
    data = request.get_json(force=True, silent=True) or {}
    on = bool(data.get("on", True))
    if on and (ROBOT is None or not get_state().get("connected")):
        return jsonify({"error": "robot not connected", **get_state()}), 503
    FOLLOW["on"] = on
    if on:
        STOP_EVENT.clear()
    else:
        VEL["lease"] = 0.0
        VEL["x"] = VEL["theta"] = 0.0
        if ROBOT is not None:
            try:
                ROBOT.stop_base()
            except Exception:
                pass
    set_state(follow=on, message="跟人模式已开启" if on else "跟人模式已关闭")
    return jsonify({"ok": True, "follow": on})


@app.post("/follow/heartbeat")
def follow_heartbeat():
    """Called once a second by the follow client (head_track.py).

    This is what makes "follow is on" different from "something is actually
    following": /status reports ``follow_client`` from this timestamp, so the
    voice side can refuse to promise to follow when no client is running.
    """
    FOLLOW["seen"] = time.time()
    return jsonify({"ok": True, "follow": FOLLOW["on"]})


@app.post("/estop")
def estop():
    STOP_EVENT.set()
    FOLLOW["on"] = False
    set_state(follow=False)
    VEL["lease"] = 0.0
    VEL["x"] = VEL["theta"] = 0.0
    if ROBOT is not None:
        try:
            ROBOT.stop_base()
        except Exception:
            pass
    COMMANDS.put("stop")
    return jsonify({"ok": True, **get_state()})


def main():
    global ROBOT, SHUTDOWN
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port1", default="/dev/xlerobot_serial_b")
    ap.add_argument("--port2", default="/dev/xlerobot_serial_a")
    ap.add_argument("--wheelbase", type=float, default=0.414)
    args = ap.parse_args()

    cfg = XLerobot2WheelsConfig(
        port1=args.port1,
        port2=args.port2,
        id="xlerobot",
        wheel_radius=0.0635,
        wheelbase=args.wheelbase,
        cameras={},  # camera_web owns the RGB devices
    )
    ROBOT = connect_robot(cfg)
    if ROBOT is None:
        print("Could not connect robot")
        set_state(connected=False, message="机器人未连接")
    else:
        set_state(connected=True, message="已连接")
        try:
            ROBOT.stop_base()
        except Exception:
            pass
        threading.Thread(target=worker, daemon=True).start()

    print(f"robot API on http://{args.host}:{args.port}")
    try:
        app.run(host=args.host, port=args.port, threaded=True)
    finally:
        SHUTDOWN = True
        STOP_EVENT.set()
        if ROBOT is not None:
            try:
                ROBOT.stop_base()
            except Exception:
                pass
            try:
                ROBOT.disconnect()
            except Exception:
                traceback.print_exc()


if __name__ == "__main__":
    main()
