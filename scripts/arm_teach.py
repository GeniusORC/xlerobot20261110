#!/usr/bin/env python3
"""Teach-and-replay one XLeRobot arm by hand (no leader arm, no training).

  source scripts/env.sh robot

  # 1) hand-guide the arm, capture waypoints (torque is off)
  python scripts/arm_teach.py record --arm right --out data/grasp_right.json

  # 2) replay the waypoints (torque on, smooth interpolation)
  python scripts/arm_teach.py replay --arm right --in data/grasp_right.json

Both commands talk to the arm buses only by default (--cameras enables the
cameras, which requires camera_web.py to be stopped).
"""
import argparse
import builtins
import json
import sys
import time

import numpy as np

import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from config_loader import CFG

PORT_LEFT  = CFG["motors"]["port_bus1"]
PORT_RIGHT = CFG["motors"]["port_bus2"]


def make_robot(use_cameras: bool):
    from lerobot_robot_xlerobot_2wheels import XLerobot2Wheels, XLerobot2WheelsConfig

    cfg = XLerobot2WheelsConfig(
        port1=PORT_LEFT,
        port2=PORT_RIGHT,
        id="xlerobot",
        wheel_radius=CFG["robot"]["wheel_radius_m"],
        wheelbase=CFG["robot"]["wheelbase_m"],
        cameras={} if not use_cameras else XLerobot2WheelsConfig().cameras,
    )
    robot = XLerobot2Wheels(cfg)
    orig_input = builtins.input
    builtins.input = lambda *a, **k: ""  # restore the existing calibration
    try:
        robot.connect(calibrate=False)
    finally:
        builtins.input = orig_input
    return robot


def arm_handles(robot, arm):
    if arm == "left":
        return robot.bus1, robot.left_arm_motors
    return robot.bus2, robot.right_arm_motors


def read_positions(bus, motors):
    raw = bus.sync_read("Present_Position", motors)
    return {k: float(v) for k, v in raw.items()}


def fmt(pos):
    return "  ".join(f"{k.split('.')[-1].replace('_arm', '')}={v:+.1f}" for k, v in pos.items())


def cmd_record(args):
    robot = make_robot(args.cameras)
    bus, motors = arm_handles(robot, args.arm)
    bus.disable_torque(motors)
    print(f"\n{args.arm} arm torque OFF - move it by hand.")
    print("Pose the arm, then press ENTER to capture a waypoint.")
    print("  u = undo last waypoint   s = save now   q = save and quit\n")

    waypoints = []
    while True:
        try:
            cmd = input(f"[{len(waypoints)} waypoints] ENTER=capture / u / s / q > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            cmd = "s"
        if cmd == "u":
            if waypoints:
                waypoints.pop()
                print("  removed last waypoint")
            continue
        if cmd == "q":
            break
        if cmd == "s":
            if not waypoints:
                print("  nothing captured yet")
                continue
            break
        pos = read_positions(bus, motors)
        waypoints.append(pos)
        print(f"  #{len(waypoints)}  {fmt(pos)}")

    if not waypoints:
        print("no waypoints, nothing saved")
        robot.disconnect()
        return 1

    out = args.out
    with open(out, "w") as f:
        json.dump({"arm": args.arm, "waypoints": waypoints}, f, indent=2)
    print(f"\nsaved {len(waypoints)} waypoints -> {out}")
    robot.disconnect()
    return 0


def cmd_replay(args):
    with open(args.inp) as f:
        data = json.load(f)
    waypoints = data["waypoints"]
    if not waypoints:
        print("no waypoints in file")
        return 1

    robot = make_robot(args.cameras)
    bus, motors = arm_handles(robot, args.arm)
    bus.enable_torque(motors)
    time.sleep(0.3)

    fps = args.fps
    max_step = args.max_step
    print(f"\nreplaying {len(waypoints)} waypoints on the {args.arm} arm "
          f"({args.segment_s:.1f}s per segment, {fps} Hz)")

    def goto(target, segment_s):
        start = read_positions(bus, motors)
        n = max(int(segment_s * fps), 1)
        prev = start
        for i in range(1, n + 1):
            a = i / n
            goal = {k: start[k] + (target[k] - start[k]) * a for k in start}
            # clamp per-tick motion for safety
            goal = {k: float(np.clip(goal[k], prev[k] - max_step, prev[k] + max_step))
                    for k in goal}
            robot.send_action({f"{k}.pos": v for k, v in goal.items()})
            prev = goal
            time.sleep(1.0 / fps)

    try:
        for i, wp in enumerate(waypoints, 1):
            print(f"  -> waypoint {i}/{len(waypoints)}  {fmt(wp)}", flush=True)
            goto(wp, args.segment_s)
            time.sleep(args.dwell_s)
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        robot.disconnect()
    print("done")
    return 0


def cmd_show(args):
    robot = make_robot(args.cameras)
    bus, motors = arm_handles(robot, args.arm)
    bus.disable_torque(motors)
    print(f"{args.arm} arm (torque off):")
    print(" ", fmt(read_positions(bus, motors)))
    robot.disconnect()
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, fn in (("record", cmd_record), ("replay", cmd_replay), ("show", cmd_show)):
        p = sub.add_parser(name)
        p.add_argument("--arm", choices=("left", "right"), default="right")
        p.add_argument("--cameras", action="store_true", help="also connect cameras (stop camera_web.py first)")
        p.set_defaults(fn=fn)
        if name == "record":
            p.add_argument("--out", default="data/grasp_waypoints.json")
        if name == "replay":
            p.add_argument("--in", dest="inp", default="data/grasp_waypoints.json")
            p.add_argument("--segment-s", type=float, default=2.0)
            p.add_argument("--dwell-s", type=float, default=0.6)
            p.add_argument("--fps", type=int, default=30)
            p.add_argument("--max-step", type=float, default=2.0,
                           help="max normalized joint change per tick (safety)")

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
