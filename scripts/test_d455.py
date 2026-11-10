#!/usr/bin/env python3
"""D455 smoke test: enumerate, stream 640x480x30, measure FPS, save one frame."""
import argparse
import time

import cv2
import numpy as np
import pyrealsense2 as rs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=60)
    ap.add_argument("--out", default="data/d455")
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()

    ctx = rs.context()
    devices = list(ctx.query_devices())
    print(f"devices: {len(devices)}")
    for d in devices:
        print(
            "  ",
            d.get_info(rs.camera_info.name),
            d.get_info(rs.camera_info.serial_number),
            d.get_info(rs.camera_info.firmware_version),
            d.get_info(rs.camera_info.usb_type_descriptor),
        )
    if not devices:
        print("No RealSense device. Check USB3 connection / replug.")
        return 1

    pipe = rs.pipeline()
    cfg = rs.config()
    cfg.enable_stream(rs.stream.depth, 640, 480, rs.format.z16, 30)
    cfg.enable_stream(rs.stream.color, 640, 480, rs.format.bgr8, 30)
    profile = pipe.start(cfg)
    try:
        # Warm up
        for _ in range(10):
            pipe.wait_for_frames()

        t0 = time.time()
        color_frame = depth_frame = None
        for _ in range(args.frames):
            frames = pipe.wait_for_frames()
            color_frame = frames.get_color_frame()
            depth_frame = frames.get_depth_frame()
        dt = time.time() - t0
        fps = args.frames / dt
        print(f"stream: {args.frames} frames in {dt:.2f}s -> {fps:.1f} fps")

        color = np.asanyarray(color_frame.get_data())
        depth = np.asanyarray(depth_frame.get_data())
        intr = profile.get_stream(rs.stream.depth).as_video_stream_profile().get_intrinsics()
        print(f"color {color.shape}, depth {depth.shape}")
        print(
            f"depth intrinsics: fx={intr.fx:.1f} fy={intr.fy:.1f} "
            f"cx={intr.ppx:.1f} cy={intr.ppy:.1f}"
        )
        h, w = depth.shape
        print(f"center depth: {depth[h // 2, w // 2] / 1000.0:.3f} m")

        if not args.no_save:
            import os

            os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
            cv2.imwrite(args.out + "_color.png", color)
            cv2.imwrite(
                args.out + "_depth.png", (depth / max(1, depth.max()) * 255).astype("uint8")
            )
            print(f"saved {args.out}_color.png / {args.out}_depth.png")
    finally:
        pipe.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
