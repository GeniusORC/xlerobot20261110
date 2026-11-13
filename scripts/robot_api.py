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
    "updated": time.time(),
}
STATE_LOCK = threading.Lock()
COMMANDS = queue.Queue()
STOP_EVENT = threading.Event()
ROBOT = None
SHUTDOWN = False
BUS_LOCK = threading.Lock()

# Head pan/tilt are ordinary STS3215 position servos on bus1 (IDs 7 and 8).
# Values are normalized to [-100, 100]; 0 is mid-range. Limits below are
# deliberately narrower than the mechanical range.
PAN_MOTOR, TILT_MOTOR = "head_motor_1", "head_motor_2"
PAN_LIMIT = (-70.0, 70.0)
TILT_LIMIT = (-30.0, 30.0)


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


def _set_head(pan=None, tilt=None):
    action = {}
    if pan is not None:
        action[f"{PAN_MOTOR}.pos"] = float(np.clip(pan, *PAN_LIMIT))
    if tilt is not None:
        action[f"{TILT_MOTOR}.pos"] = float(np.clip(tilt, *TILT_LIMIT))
    if action:
        _rmw(ROBOT.send_action, action)
    return action


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


def _drive(x: float, theta: float, duration: float):
    """Bounded fine positioning used by the health station auto-framing."""
    x = float(np.clip(x, -0.08, 0.08))
    theta = float(np.clip(theta, -25.0, 25.0))
    duration = float(np.clip(duration, 0.05, 3.0))
    _move(x, theta, duration, "正在微调位置")


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
            intent = COMMANDS.get(timeout=0.2)
        except queue.Empty:
            pass

        if intent == "stop":
            _stop()
            continue
        if isinstance(intent, dict):
            kind = intent.get("type")
            if kind == "head":
                try:
                    pan0, tilt0 = _read_head()
                    if intent.get("center"):
                        _set_head(0.0, 0.0)
                    else:
                        pan = intent.get("pan")
                        tilt = intent.get("tilt")
                        if pan is None and intent.get("dpan") is not None:
                            pan = pan0 + float(intent["dpan"])
                        if tilt is None and intent.get("dtilt") is not None:
                            tilt = tilt0 + float(intent["dtilt"])
                        _set_head(pan, tilt)
                    set_state(busy=False, message="头部已调整")
                except Exception as e:
                    set_state(message=f"头部调整失败: {e}")
                continue
            if kind == "drive":
                _drive(float(intent.get("x", 0.0)),
                       float(intent.get("theta", 0.0)),
                       float(intent.get("duration", 0.5)))
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

        # Idle: make sure the base is stopped.
        if time.time() - last > 0.5:
            try:
                ROBOT.stop_base()
            except Exception:
                pass
            last = time.time()


@app.get("/status")
def status():
    return jsonify(get_state())


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
    COMMANDS.put(intent)
    return jsonify({"queued": intent, **get_state()})


@app.get("/head")
def head_status():
    if ROBOT is None or not get_state().get("connected"):
        return jsonify({"error": "robot not connected", **get_state()}), 503
    try:
        pan, tilt = _read_head()
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
    return jsonify({"pan": pan, "tilt": tilt,
                    "pan_limit": PAN_LIMIT, "tilt_limit": TILT_LIMIT})


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


@app.post("/estop")
def estop():
    STOP_EVENT.set()
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
