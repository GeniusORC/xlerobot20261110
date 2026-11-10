#!/usr/bin/env python3
"""Small HTTP robot-control service for the voice web demo.

Run in the robot environment:
  source scripts/env.sh robot
  python scripts/robot_api.py --port 8090

Endpoints:
  GET  /status
  POST /command   {"intent": "stop"|"come"|"report"}
  POST /estop
"""
import argparse
import builtins
import queue
import threading
import time
import traceback

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
            STOP_EVENT.set()
            try:
                ROBOT.stop_base()
            except Exception:
                pass
            set_state(busy=False, last_intent="stop", message="已停止", x_vel=0.0, theta_vel=0.0)
            continue

        if intent == "come":
            STOP_EVENT.clear()
            set_state(busy=True, last_intent="come", message="正在靠近")
            try:
                # Safe approach stub: 5 cm/s for up to 4 s (0.2 m).
                deadline = time.time() + 4.0
                while time.time() < deadline and not STOP_EVENT.is_set():
                    ROBOT.send_action({"x.vel": 0.05, "theta.vel": 0.0})
                    time.sleep(0.05)
                ROBOT.stop_base()
                set_state(
                    busy=False,
                    message="已停止" if STOP_EVENT.is_set() else "已靠近并停稳",
                    x_vel=0.0,
                    theta_vel=0.0,
                )
            except Exception as e:
                set_state(busy=False, message=f"接近失败: {e}")
            continue

        if intent == "report":
            set_state(busy=True, last_intent="report", message="正在读取状态")
            try:
                vel = ROBOT.bus2.sync_read("Present_Velocity", ROBOT.base_motors, normalize=False)
                set_state(
                    busy=False,
                    message="报告完成",
                    x_vel=float(vel.get("base_left_wheel", 0)),
                    theta_vel=float(vel.get("base_right_wheel", 0)),
                )
            except Exception as e:
                set_state(busy=False, message=f"读取失败: {e}")
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
    if intent not in ("stop", "come", "report"):
        return jsonify({"error": "intent must be stop|come|report"}), 400
    if ROBOT is None or not get_state().get("connected"):
        return jsonify({"error": "robot not connected", **get_state()}), 503
    if intent == "stop":
        STOP_EVENT.set()
    COMMANDS.put(intent)
    return jsonify({"queued": intent, **get_state()})


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
