#!/usr/bin/env python3
"""Connect/read/disconnect test for XLerobot2Wheels.

This enables torque during the test and disables it on disconnect.
Run only with the robot in a safe, unobstructed pose.

  source scripts/env.sh robot
  python scripts/test_lerobot_connect.py --yes
"""
import argparse
import traceback

from lerobot_robot_xlerobot_2wheels import XLerobot2Wheels, XLerobot2WheelsConfig


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="actually connect (enables torque)")
    ap.add_argument("--port1", default="/dev/xlerobot_serial_b")  # left arm + head (IDs 1-8)
    ap.add_argument("--port2", default="/dev/xlerobot_serial_a")  # right arm + wheels (1-6,9,10)
    ap.add_argument("--wheel-radius", type=float, default=0.0635)
    ap.add_argument("--wheelbase", type=float, default=0.367)
    ap.add_argument("--skip-calibration", action="store_true", help="compatibility test only: do not run interactive calibration")
    args = ap.parse_args()

    cfg = XLerobot2WheelsConfig(
        port1=args.port1,
        port2=args.port2,
        id="xlerobot",
        wheel_radius=args.wheel_radius,
        wheelbase=args.wheelbase,
    )
    robot = XLerobot2Wheels(cfg)
    print("constructed:", robot)
    if not args.yes:
        print("dry run only; pass --yes to connect")
        return 0
    if args.skip_calibration:
        print("WARNING: skipping interactive calibration (compatibility test only)")
        robot.calibrate = lambda: None

    # If a calibration file exists, connect() asks whether to restore it.
    # Answer ENTER non-interactively; calibrate=False avoids falling back to
    # the interactive manual calibration if restore fails.
    import builtins

    _orig_input = builtins.input
    builtins.input = lambda *a, **k: ""
    try:
        # calibrate=False means: restore the existing calibration if present;
        # never fall into the interactive manual calibration.
        robot.connect(calibrate=False)
        print("connected")
        print("raw bus1 positions:", robot.bus1.sync_read("Present_Position", robot.left_arm_motors, normalize=False))
        print("raw bus2 positions:", robot.bus2.sync_read("Present_Position", robot.right_arm_motors, normalize=False))
        print("raw head:", robot.bus1.sync_read("Present_Position", robot.head_motors, normalize=False))
        print("raw wheels velocity:", robot.bus2.sync_read("Present_Velocity", robot.base_motors, normalize=False))
        if robot.is_calibrated:
            print("norm bus1 positions:", robot.bus1.sync_read("Present_Position", robot.left_arm_motors))
            print("norm bus2 positions:", robot.bus2.sync_read("Present_Position", robot.right_arm_motors))
            print("norm head:", robot.bus1.sync_read("Present_Position", robot.head_motors))
            print("norm wheels:", robot.bus2.sync_read("Present_Velocity", robot.base_motors))
        else:
            print("not calibrated: skipping normalized reads (run calibration first)")
        return 0
    except Exception:
        traceback.print_exc()
        return 1
    finally:
        builtins.input = _orig_input
        try:
            robot.disconnect()
            print("disconnected")
        except Exception:
            traceback.print_exc()


if __name__ == "__main__":
    raise SystemExit(main())
