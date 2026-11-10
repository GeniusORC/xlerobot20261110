#!/usr/bin/env python3
"""Verify the configured RGB cameras: left wrist, right wrist, head.

  source scripts/env.sh robot
  python scripts/test_cameras.py
"""
import os
import time

import cv2
import numpy as np

from lerobot.cameras.utils import make_cameras_from_configs
from lerobot_robot_xlerobot_2wheels import XLerobot2WheelsConfig


def main() -> int:
    cfg = XLerobot2WheelsConfig(
        id="xlerobot",
        port1="/dev/xlerobot_serial_b",
        port2="/dev/xlerobot_serial_a",
        wheel_radius=0.0635,
        wheelbase=0.414,
    )
    print("configured cameras:")
    for name, cam in cfg.cameras.items():
        print(f"  {name}: {cam.index_or_path} {cam.width}x{cam.height}@{cam.fps}")

    cams = make_cameras_from_configs(cfg.cameras)
    os.makedirs("data", exist_ok=True)
    try:
        for name, cam in cams.items():
            cam.connect()
            print(f"{name}: connected")
        # warm up
        for _ in range(5):
            for cam in cams.values():
                cam.async_read()

        t0 = time.time()
        frames = {}
        for _ in range(20):
            for name, cam in cams.items():
                frames[name] = cam.async_read()
        dt = time.time() - t0
        for name, frame in frames.items():
            cv2.imwrite(f"data/cam_{name}.png", frame)
            print(f"{name}: {frame.shape} dtype={frame.dtype} mean={float(np.mean(frame)):.1f}")
            print(f"  saved data/cam_{name}.png")
        print(f"20 rounds in {dt:.2f}s -> {20 / dt:.1f} rounds/s (3 cameras)")
        return 0
    finally:
        for cam in cams.values():
            try:
                cam.disconnect()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
