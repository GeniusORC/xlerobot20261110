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
  POST /estop                        stop the base and revoke follow
  POST /reset                        motion reset: stop, drop follow, head to eye level
  GET  /motors                       broadcast-ping both buses (ID -> model)
  GET  /motor/<bus>/<id>             position, load, voltage, temperature, torque
  POST /motor     {"bus":1,"id":7,"goal":2048|"jog":20|"torque":false|"set_id":9}
"""
import argparse
import builtins
import math
import json
from pathlib import Path
import os
import queue
import threading
import time
import traceback

import cv2
import numpy as np
import requests
from flask import Flask, jsonify, request

from lerobot_robot_xlerobot_2wheels import XLerobot2Wheels, XLerobot2WheelsConfig
from config_loader import CFG, project_path, service_url

app = Flask(__name__)
STATE = {
    "connected": False,
    "api_ready": True,
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
ARM_LOCK = threading.Lock()
ARM_STOP = threading.Event()
POSE_DIR = project_path("data/poses")

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

# Where "reset" puts the head.  The calibration midpoint (0) points at the
# table on this build, so resetting to 0 leaves the robot staring at the desk;
# -63.6 is the eye-level pose measured on 2026-11-16.
HEAD_EYE_TILT = float(os.environ.get("HEAD_EYE_TILT", CFG["robot"]["head_eye_tilt"]))

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
    ARM_STOP.set()
    try:
        ROBOT.stop_base()
    except Exception:
        pass
    set_state(busy=False, message="已停止", x_vel=0.0, theta_vel=0.0)


def _reset_motion():
    """Put the robot back in a known, safe state.

    Stop the base, drop follow, clear the velocity lease, and ramp the head to
    eye level.  Deliberately does *not* cycle torque: that is a separate
    maintenance action, and dropping torque on a loaded arm makes it fall.
    """
    STOP_EVENT.set()
    ARM_STOP.set()
    FOLLOW["on"] = False
    VEL.update(x=0.0, theta=0.0, lease=0.0)
    set_state(follow=False, x_vel=0.0, theta_vel=0.0,
              busy=True, message="正在重置")
    try:
        ROBOT.stop_base()
    except Exception as e:
        print(f"reset: stop_base failed: {e}", flush=True)
    try:
        # /head moves at most MAX_STEP per request; ramp instead of jumping.
        for _ in range(40):
            pan, tilt = _read_head()
            dp = float(np.clip(0.0 - pan, -MAX_STEP, MAX_STEP))
            dt = float(np.clip(HEAD_EYE_TILT - tilt, -MAX_STEP, MAX_STEP))
            if abs(dp) < 0.6 and abs(dt) < 0.6:
                break
            action = {}
            if abs(dp) >= 0.6:
                action[f"{PAN_MOTOR}.pos"] = float(np.clip(pan + dp, *HARD_LIMIT))
            if abs(dt) >= 0.6:
                action[f"{TILT_MOTOR}.pos"] = float(np.clip(tilt + dt, *HARD_LIMIT))
            _rmw(ROBOT.send_action, action)
            time.sleep(0.05)
    except Exception as e:
        print(f"reset: head ramp failed: {e}", flush=True)
    STOP_EVENT.clear()
    set_state(busy=False, message="已重置")


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
                r = requests.get(service_url("camera_web") + "/snapshot/head", timeout=1)
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
        if intent == "reset":
            _reset_motion()
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
    out["ok"] = out.get("api_ready", False)
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
        ARM_STOP.set()
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
    ARM_STOP.set()
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
    while True:
        try:
            COMMANDS.get_nowait()
        except queue.Empty:
            break
    COMMANDS.put("stop")
    return jsonify({"ok": True, **get_state()})


@app.post("/reset")
def reset():
    """Motion reset: stop, drop follow, head back to eye level."""
    if ROBOT is None or not get_state().get("connected"):
        return jsonify({"error": "robot not connected", **get_state()}), 503
    COMMANDS.put("reset")
    return jsonify({"queued": "reset", **get_state()})


# --- motor-level access (control panel) ------------------------------------
# The panel addresses motors by bus + ID, because that is what you need when a
# motor's ID does not match the config.  Register access goes through the
# packet handler rather than the LeRobot bus API so it also works for IDs that
# are not in the robot config at all.
BUS_NAMES = {1: "bus1", 2: "bus2"}
REG = {
    "ID": (5, 1),
    "Operating_Mode": (33, 1),
    "Torque_Enable": (40, 1),
    "Goal_Position": (42, 2),
    "Lock": (55, 1),
    "Present_Position": (56, 2),
    "Present_Load": (60, 2),
    "Present_Voltage": (62, 1),
    "Present_Temperature": (63, 1),
    "Moving": (66, 1),
}


def _bus(n):
    n = int(n)
    if n not in BUS_NAMES:
        raise ValueError("bus must be 1 or 2")
    return ROBOT.bus1 if n == 1 else ROBOT.bus2


def _motor_names(bus):
    return {m.id: name for name, m in bus.motors.items()}


def _raw_read(bus, mid, reg):
    addr, length = REG[reg]
    ph, pk = bus.port_handler, bus.packet_handler
    if length == 1:
        val, comm, err = pk.read1ByteTxRx(ph, int(mid), addr)
    else:
        val, comm, err = pk.read2ByteTxRx(ph, int(mid), addr)
    if not bus._is_comm_success(comm):
        raise IOError(pk.getTxRxResult(comm))
    if bus._is_error(err):
        raise IOError(pk.getRxPacketError(err))
    return int(val)


def _raw_write(bus, mid, reg, value):
    addr, length = REG[reg]
    ph, pk = bus.port_handler, bus.packet_handler
    if length == 1:
        comm, err = pk.write1ByteTxRx(ph, int(mid), addr, int(value))
    else:
        comm, err = pk.write2ByteTxRx(ph, int(mid), addr, int(value))
    if not bus._is_comm_success(comm):
        raise IOError(pk.getTxRxResult(comm))
    if bus._is_error(err):
        raise IOError(pk.getRxPacketError(err))


@app.get("/motors")
def motors_scan():
    """Broadcast-ping both buses: which IDs answer, and which are unexpected."""
    if ROBOT is None or not get_state().get("connected"):
        return jsonify({"error": "robot not connected"}), 503
    out = {}
    for n, label in BUS_NAMES.items():
        bus = _bus(n)
        names = _motor_names(bus)
        try:
            found = _rmw(bus.broadcast_ping, 1) or {}
        except Exception as e:
            out[label] = {"error": f"{type(e).__name__}: {e}", "motors": []}
            continue
        out[label] = {
            "motors": [{"id": mid, "model": int(model),
                        "name": names.get(mid)}
                       for mid, model in sorted(found.items())],
            "unknown": sorted(mid for mid in found if mid not in names),
            "missing": sorted(mid for mid in names if mid not in found),
        }
    return jsonify({"buses": out})


@app.get("/motor/<int:bus_n>/<int:mid>")
def motor_read(bus_n, mid):
    if ROBOT is None or not get_state().get("connected"):
        return jsonify({"error": "robot not connected"}), 503
    try:
        bus = _bus(bus_n)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    data = {"bus": bus_n, "id": mid, "name": _motor_names(bus).get(mid)}
    for reg in ("Present_Position", "Goal_Position", "Present_Load",
                "Present_Voltage", "Present_Temperature", "Moving",
                "Torque_Enable", "Operating_Mode"):
        try:
            data[reg] = _rmw(_raw_read, bus, mid, reg)
        except Exception as e:
            data[reg] = None
            data.setdefault("errors", {})[reg] = str(e)
    # STS load: bit 10 is the direction, the low 10 bits the magnitude
    # (0-1000 = 0-100 % of the torque limit).
    load = data.get("Present_Load")
    if load is not None:
        pct = (load & 0x3FF) * 0.1
        data["load_pct"] = round(-pct if load & 0x400 else pct, 1)
    if data.get("Present_Voltage") is not None:
        data["voltage_v"] = round(data["Present_Voltage"] * 0.1, 1)
    return jsonify(data)


@app.post("/motor")
def motor_write():
    """Jog / torque / change-ID for one motor.

    ``goal`` and ``jog`` are raw encoder counts (0-4095).  Changing an ID only
    updates the running config: config_xlerobot_2wheels.py still has the old
    one, so a restart would look for the motor at the old address.
    """
    data = request.get_json(force=True, silent=True) or {}
    if ROBOT is None or not get_state().get("connected"):
        return jsonify({"error": "robot not connected"}), 503
    try:
        bus_n = int(data.get("bus", 1))
        mid = int(data.get("id"))
    except (TypeError, ValueError):
        return jsonify({"error": "need numeric bus and id"}), 400
    try:
        bus = _bus(bus_n)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    did, warning = [], None
    try:
        if "torque" in data:
            _rmw(_raw_write, bus, mid, "Torque_Enable",
                 1 if data["torque"] else 0)
            did.append("torque=%d" % (1 if data["torque"] else 0))
        if data.get("goal") is not None:
            goal = int(np.clip(int(data["goal"]), 0, 4095))
            _rmw(_raw_write, bus, mid, "Goal_Position", goal)
            did.append(f"goal={goal}")
        if data.get("jog") is not None:
            now = _rmw(_raw_read, bus, mid, "Present_Position")
            goal = int(np.clip(now + int(data["jog"]), 0, 4095))
            _rmw(_raw_write, bus, mid, "Goal_Position", goal)
            did.append(f"jog {now}->{goal}")
        if data.get("set_id") is not None:
            new_id = int(data["set_id"])
            if not 1 <= new_id <= 253:
                return jsonify({"error": "new id must be 1..253"}), 400
            if new_id != mid:
                names = _motor_names(bus)
                if new_id in names:
                    return jsonify({"error": f"ID {new_id} already used by "
                                             f"{names[new_id]}"}), 409
                _rmw(_raw_write, bus, mid, "Lock", 0)
                _rmw(_raw_write, bus, mid, "ID", new_id)
                _rmw(_raw_write, bus, new_id, "Lock", 1)
                name = names.get(mid)
                if name in bus.motors:
                    bus.motors[name].id = new_id
                did.append(f"id {mid}->{new_id}")
                warning = ("ID changed in RAM only: update Motor(..., id=...) in "
                           "config_xlerobot_2wheels.py and restart robot_api, "
                           "or the next start will look for the old ID")
    except Exception as e:
        return jsonify({"ok": False, "did": did,
                        "error": f"{type(e).__name__}: {e}"}), 500
    if not did:
        return jsonify({"error": "nothing to do: give torque, goal, jog or set_id"}), 400
    return jsonify({"ok": True, "did": did, "warning": warning})


# ----------------------------------------  arms  ----------------------------------------

@app.get("/arm/state")
def arm_state():
    """Read normalized positions for all 12 arm joints (6 per arm)."""
    if ROBOT is None or not get_state().get("connected"):
        return jsonify({"error": "robot not connected"}), 503
    out = {}
    for arm_name, bus, motors in [("left", ROBOT.bus1, ROBOT.left_arm_motors),
                                   ("right", ROBOT.bus2, ROBOT.right_arm_motors)]:
        try:
            positions = _rmw(bus.sync_read, "Present_Position", motors)
            out[arm_name] = {name: float(val) for name, val in positions.items()}
        except Exception as e:
            out[arm_name] = {"error": f"{type(e).__name__}: {e}"}
    return jsonify(out)


@app.post("/arm/gripper")
def arm_gripper():
    """Open or close a gripper.

    Body: {"arm": "left"|"right", "open": true|false}

    The gripper is the last joint (index 5, ID 6 on each arm).
    Gripper range is 0..100 in the upstream driver.
    """
    if ROBOT is None or not get_state().get("connected"):
        return jsonify({"error": "robot not connected"}), 503
    data = request.get_json(force=True, silent=True) or {}
    arm = data.get("arm")
    if arm not in ("left", "right"):
        return jsonify({"error": "arm must be 'left' or 'right'"}), 400
    open_it = data.get("open")
    if not isinstance(open_it, bool):
        return jsonify({"error": "open must be true or false"}), 400

    bus = ROBOT.bus1 if arm == "left" else ROBOT.bus2
    motors = ROBOT.left_arm_motors if arm == "left" else ROBOT.right_arm_motors
    gripper_name = next(name for name in motors if name.endswith("_gripper"))

    goal = CFG["arms"]["gripper_open" if open_it else "gripper_closed"]
    return execute_arm_pose(arm, {gripper_name: goal}, 2.0)


@app.post("/arm/pose")
def arm_pose():
    """Replay a saved arm pose (interpolated motion).

    Body: {"arm": "left"|"right", "pose": {motor_name: position, ...}, "duration_s": 2.0}

    Moves all joints to the target pose over duration_s seconds with smooth interpolation.
    """
    if ROBOT is None or not get_state().get("connected"):
        return jsonify({"error": "robot not connected"}), 503
    data = request.get_json(force=True, silent=True) or {}
    arm = data.get("arm")
    if arm not in ("left", "right"):
        return jsonify({"error": "arm must be 'left' or 'right'"}), 400
    pose = data.get("pose")
    if not isinstance(pose, dict):
        return jsonify({"error": "pose must be a dict of {motor_name: position}"}), 400
    return execute_arm_pose(arm, pose, data.get("duration_s", 2.0))


def execute_arm_pose(arm, pose, duration):
    """Serialize arm trajectories; validate every target before any motor write."""
    try:
        duration = float(duration)
        if not math.isfinite(duration) or not 0.5 <= duration <= 10:
            raise ValueError("duration_s must be 0.5..10")
        motors = ROBOT.left_arm_motors if arm == "left" else ROBOT.right_arm_motors
        if not pose or set(pose) - set(motors):
            raise ValueError("pose must contain only joints belonging to the selected arm")
        pose = {name: float(value) for name, value in pose.items()}
        for name, value in pose.items():
            lo, hi = (0, 100) if name.endswith("_gripper") else (-98, 98)
            if not math.isfinite(value) or not lo <= value <= hi:
                raise ValueError(f"{name} must be {lo}..{hi}")
    except (TypeError, ValueError) as e:
        return jsonify({"error": str(e)}), 400
    if not ARM_LOCK.acquire(blocking=False):
        return jsonify({"error": "another arm operation is active"}), 409
    ARM_STOP.clear()

    bus = ROBOT.bus1 if arm == "left" else ROBOT.bus2
    motors = ROBOT.left_arm_motors if arm == "left" else ROBOT.right_arm_motors

    try:
        # Read current positions
        current = _rmw(bus.sync_read, "Present_Position", motors)
        current = {k: float(v) for k, v in current.items()}

        # Interpolate over duration_s at 30 Hz
        fps = 30
        max_step = float(CFG["arms"]["max_step"])
        if not all(name in current and math.isfinite(current[name]) for name in pose):
            raise ValueError("incomplete joint observation")
        steps = max(int(duration * fps), math.ceil(max(abs(pose[k] - current[k]) for k in pose) / max_step), 1)
        for i in range(1, steps + 1):
            if ARM_STOP.is_set():
                # Hold measured positions; keep torque enabled to support the arm.
                held = _rmw(bus.sync_read, "Present_Position", motors)
                _rmw(ROBOT.send_action, {f"{k}.pos": float(v) for k, v in held.items()})
                return jsonify({"ok": False, "stopped": True}), 409
            alpha = i / steps
            action = {}
            for name in motors:
                if name in pose:
                    start = current.get(name, 0.0)
                    target = float(pose[name])
                    interp = start + (target - start) * alpha
                    action[f"{name}.pos"] = interp
            if action:
                _rmw(ROBOT.send_action, action)
            ARM_STOP.wait(1.0 / fps)

        return jsonify({"ok": True, "arm": arm, "moved": list(pose.keys())})
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500
    finally:
        ARM_LOCK.release()


@app.get("/arm/poses")
def poses_list():
    POSE_DIR.mkdir(parents=True, exist_ok=True)
    return jsonify({"poses": sorted(p.stem for p in POSE_DIR.glob("*.json"))})


@app.post("/arm/poses/<name>")
def poses_save(name):
    if not name or len(name) > 64 or not all(c.isalnum() or c in "_-" for c in name):
        return jsonify({"error": "pose name must use letters, numbers, _ or -"}), 400
    if ROBOT is None or not get_state().get("connected"):
        return jsonify({"error": "robot not connected"}), 503
    state = {}
    try:
        for arm_name, bus, motors in (("left", ROBOT.bus1, ROBOT.left_arm_motors),
                                      ("right", ROBOT.bus2, ROBOT.right_arm_motors)):
            state[arm_name] = {joint: float(value) for joint, value in
                               _rmw(bus.sync_read, "Present_Position", motors).items()}
            if set(state[arm_name]) != set(motors):
                raise ValueError("incomplete joint observation")
    except Exception as e:
        return jsonify({"error": f"joint read failed: {type(e).__name__}"}), 503
    POSE_DIR.mkdir(parents=True, exist_ok=True)
    target = POSE_DIR / f"{name}.json"
    tmp = target.with_suffix(".tmp")
    with ARM_LOCK:
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(target)
    return jsonify({"ok": True, "name": name})


@app.post("/arm/poses/<name>/replay")
def poses_replay(name):
    if not name or len(name) > 64 or not all(c.isalnum() or c in "_-" for c in name):
        return jsonify({"error": "invalid name"}), 400
    if ROBOT is None or not get_state().get("connected"):
        return jsonify({"error": "robot not connected"}), 503
    try:
        pose = json.loads((POSE_DIR / f"{name}.json").read_text())
    except FileNotFoundError:
        return jsonify({"error": "pose not found"}), 404
    except (OSError, json.JSONDecodeError):
        return jsonify({"error": "pose file is unreadable"}), 400
    if not isinstance(pose, dict) or set(pose) != {"left", "right"}:
        return jsonify({"error": "pose file must contain left and right joints"}), 400
    # Validate both arms and acquire the trajectory lock before moving either.
    try:
        validated = {}
        for arm in ("left", "right"):
            names = ROBOT.left_arm_motors if arm == "left" else ROBOT.right_arm_motors
            values = pose[arm]
            if not isinstance(values, dict) or set(values) != set(names):
                raise ValueError(f"pose has invalid {arm} joints")
            validated[arm] = {k: float(v) for k, v in values.items()}
            for name, value in validated[arm].items():
                lo, hi = (0, 100) if name.endswith("_gripper") else (-98, 98)
                if not math.isfinite(value) or not lo <= value <= hi:
                    raise ValueError(f"{name} outside {lo}..{hi}")
    except (TypeError, ValueError) as e:
        return jsonify({"error": str(e)}), 400
    if not ARM_LOCK.acquire(blocking=False):
        return jsonify({"error": "another arm operation is active"}), 409
    ARM_LOCK.release()
    for arm in ("left", "right"):
        result = execute_arm_pose(arm, validated[arm], 2.0)
        if isinstance(result, tuple):
            return result
    return jsonify({"ok": True, "name": name})


def main():
    global ROBOT, SHUTDOWN
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port1", default=CFG["motors"]["port_bus1"])
    ap.add_argument("--port2", default=CFG["motors"]["port_bus2"])
    ap.add_argument("--wheelbase", type=float, default=CFG["robot"]["wheelbase_m"])
    args = ap.parse_args()

    cfg = XLerobot2WheelsConfig(
        port1=args.port1,
        port2=args.port2,
        id="xlerobot",
        wheel_radius=CFG["robot"]["wheel_radius_m"],
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
        ARM_STOP.set()
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
