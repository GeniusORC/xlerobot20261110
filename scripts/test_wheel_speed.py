#!/usr/bin/env python3
"""Low-speed differential-drive test.

SAFETY: put the robot on blocks / lift the wheels before running. The default
command is only 0.03 m/s for 0.8 s. A physical emergency stop must be reachable.

  source scripts/env.sh robot
  python scripts/test_wheel_speed.py --yes
"""
import argparse
import builtins
import time
import traceback

from lerobot_robot_xlerobot_2wheels import XLerobot2Wheels, XLerobot2WheelsConfig


def read_vel(robot):
    return robot.bus2.sync_read("Present_Velocity", robot.base_motors, normalize=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="actually command the wheels")
    ap.add_argument("--speed", type=float, default=0.03, help="linear speed m/s")
    ap.add_argument("--turn", type=float, default=10.0, help="rotation deg/s")
    ap.add_argument("--duration", type=float, default=0.8, help="seconds per command")
    ap.add_argument("--port1", default="/dev/xlerobot_serial_b")
    ap.add_argument("--port2", default="/dev/xlerobot_serial_a")
    ap.add_argument("--wheel-radius", type=float, default=0.0635)
    ap.add_argument("--wheelbase", type=float, default=0.25)
    args = ap.parse_args()

    cfg = XLerobot2WheelsConfig(
        port1=args.port1,
        port2=args.port2,
        id="xlerobot",
        wheel_radius=args.wheel_radius,
        wheelbase=args.wheelbase,
    )
    robot = XLerobot2Wheels(cfg)
    if not args.yes:
        print("dry run: pass --yes to command the wheels")
        return 0

    _orig_input = builtins.input
    builtins.input = lambda *a, **k: ""
    try:
        robot.connect(calibrate=False)
        print("connected; calibration restored")
        print("before:", read_vel(robot))

        tests = [
            ("forward", {"x.vel": args.speed, "theta.vel": 0.0}),
            ("reverse", {"x.vel": -args.speed, "theta.vel": 0.0}),
            ("rotate_left", {"x.vel": 0.0, "theta.vel": args.turn}),
            ("rotate_right", {"x.vel": 0.0, "theta.vel": -args.turn}),
        ]
        for name, action in tests:
            print(f"\n=== {name}: {action} ===")
            robot.send_action(action)
            t0 = time.time()
            samples = []
            while time.time() - t0 < args.duration:
                samples.append(read_vel(robot))
                time.sleep(0.1)
            robot.stop_base()
            time.sleep(0.4)
            print("samples:", samples)
            print("after stop:", read_vel(robot))
            obs = robot.get_observation()
            print("body velocity:", {"x.vel": obs.get("x.vel"), "theta.vel": obs.get("theta.vel")})
        print("\nALL TESTS DONE")
        return 0
    except Exception:
        traceback.print_exc()
        return 1
    finally:
        builtins.input = _orig_input
        try:
            robot.stop_base()
        except Exception:
            pass
        try:
            robot.disconnect()
            print("disconnected")
        except Exception:
            traceback.print_exc()


if __name__ == "__main__":
    raise SystemExit(main())
