#!/usr/bin/env python3
"""Identify which USB wrist camera is on which arm.

Moves one arm a few units and checks which camera image changes.  Run with
robot_api stopped (it owns the serial bus).
"""
import sys
import time

import cv2
import numpy as np

from lerobot_robot_xlerobot_2wheels import XLerobot2Wheels, XLerobot2WheelsConfig

DEVICES = {"a(1.2)": "/dev/video8", "b(2.2)": "/dev/video10"}


def grab(dev, n=8):
    cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    frame = None
    for _ in range(n):
        ok, f = cap.read()
        if ok:
            frame = f
        time.sleep(0.03)
    cap.release()
    return frame


def shots():
    return {k: grab(d) for k, d in DEVICES.items()}


def main():
    cfg = XLerobot2WheelsConfig(port1="/dev/xlerobot_serial_b",
                                port2="/dev/xlerobot_serial_a",
                                id="xlerobot", wheel_radius=0.0635,
                                wheelbase=0.414, cameras={})
    robot = XLerobot2Wheels(cfg)
    import builtins
    orig = builtins.input
    builtins.input = lambda *a, **k: ""
    try:
        robot.connect(calibrate=False)
    finally:
        builtins.input = orig

    obs = robot.get_observation()
    key = "left_arm_shoulder_pan.pos"
    before_pos = float(obs[key])
    print(f"left_arm_shoulder_pan = {before_pos:.1f}", flush=True)

    before = shots()
    for k, f in before.items():
        if f is not None:
            cv2.imwrite(f"/tmp/opencode/ident_{k.split('(')[0]}_before.jpg", f)

    # Small, reversible sideways swing of the LEFT arm only.
    robot.send_action({key: float(np.clip(before_pos + 6.0, -100, 100))})
    time.sleep(1.2)
    after = shots()
    for k, f in after.items():
        if f is not None:
            cv2.imwrite(f"/tmp/opencode/ident_{k.split('(')[0]}_after.jpg", f)

    robot.send_action({key: before_pos})
    time.sleep(0.6)

    print("\nmean abs difference per camera (0-255):", flush=True)
    for k in before:
        a, b = before[k], after[k]
        if a is None or b is None:
            print(f"  {k}: capture failed", flush=True)
            continue
        d = float(np.mean(np.abs(a.astype(np.float32) - b.astype(np.float32))))
        print(f"  {k}: {d:.2f}", flush=True)

    robot.disconnect()


if __name__ == "__main__":
    sys.exit(main())
