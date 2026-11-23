#!/usr/bin/env python3
"""Minimal keyboard teleop for the XLeRobot 2-wheel base.

Keys:
  w / s : forward / backward
  a / d : rotate left / right
  space : stop
  1 / 2 / 3 : speed level
  q or Esc : quit

Run this in a real terminal window (it needs keyboard focus):
  source scripts/env.sh robot
  python scripts/teleop_base.py
"""
import builtins
import time
import traceback

from pynput import keyboard
from config_loader import CFG

from lerobot_robot_xlerobot_2wheels import XLerobot2Wheels, XLerobot2WheelsConfig

SPEED_LEVELS = [
    (0.05, 8.0),   # slow
    (0.10, 15.0),  # medium
    (0.15, 20.0),  # fast
]


class Keys:
    def __init__(self):
        self.pressed = set()
        self.quit = False
        self.listener = keyboard.Listener(on_press=self.on_press, on_release=self.on_release)

    def on_press(self, key):
        try:
            name = key.char.lower() if key.char else key.name
        except Exception:
            name = getattr(key, "name", str(key))
        if name in ("q", "esc"):
            self.quit = True
            return False
        self.pressed.add(name)

    def on_release(self, key):
        try:
            name = key.char.lower() if key.char else key.name
        except Exception:
            name = getattr(key, "name", str(key))
        self.pressed.discard(name)

    def start(self):
        self.listener.start()

    def stop(self):
        try:
            self.listener.stop()
        except Exception:
            pass


def connect_robot(cfg):
    for attempt in range(3):
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


def main() -> int:
    cfg = XLerobot2WheelsConfig(
        port1=CFG["motors"]["port_bus1"],
        port2=CFG["motors"]["port_bus2"],
        id="xlerobot",
        wheel_radius=CFG["robot"]["wheel_radius_m"],
        wheelbase=CFG["robot"]["wheelbase_m"],
    )
    robot = connect_robot(cfg)
    if robot is None:
        print("Could not connect after 3 attempts.")
        return 1

    keys = Keys()
    keys.start()
    level = 0
    print("Teleop ready.")
    print("w/s = forward/back, a/d = left/right, space = stop, 1/2/3 = speed, q = quit")
    try:
        while not keys.quit:
            if "1" in keys.pressed:
                level = 0
            elif "2" in keys.pressed:
                level = 1
            elif "3" in keys.pressed:
                level = 2
            vmax, wmax = SPEED_LEVELS[level]

            x = 0.0
            th = 0.0
            if "w" in keys.pressed:
                x += vmax
            if "s" in keys.pressed:
                x -= vmax
            if "a" in keys.pressed:
                th += wmax
            if "d" in keys.pressed:
                th -= wmax

            if "space" in keys.pressed or (x == 0.0 and th == 0.0):
                robot.stop_base()
            else:
                robot.send_action({"x.vel": x, "theta.vel": th})
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    except Exception:
        traceback.print_exc()
    finally:
        keys.stop()
        try:
            robot.stop_base()
        except Exception:
            pass
        try:
            robot.disconnect()
        except Exception:
            traceback.print_exc()
        print("Teleop stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
