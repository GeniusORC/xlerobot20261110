#!/usr/bin/env python3
"""1 m straight + in-place 90 deg test, with D455 gyro/encoder analysis.

SAFETY: clear floor, robot on the ground, physical E-stop within reach.
The script commands ~0.15 m/s for 6.7 s and then ~20 deg/s for 4.5 s.

  source scripts/env.sh robot
  python scripts/test_odometry.py --yes
  python scripts/test_odometry.py --yes --distance 0.98  # measured 1 m result
"""
import argparse
import builtins
import math
import time
import traceback

import numpy as np

from lerobot_robot_xlerobot_2wheels import XLerobot2Wheels, XLerobot2WheelsConfig

TICKS_PER_REV = 4096.0


class EncoderTracker:
    def __init__(self, robot):
        self.robot = robot
        self.prev = robot.bus2.sync_read("Present_Position", robot.base_motors, normalize=False)
        self.acc = {k: 0 for k in self.robot.base_motors}

    def sample(self):
        cur = self.robot.bus2.sync_read("Present_Position", self.robot.base_motors, normalize=False)
        for k in self.robot.base_motors:
            p, c = int(self.prev[k]), int(cur[k])
            d = ((c - p + TICKS_PER_REV / 2) % TICKS_PER_REV) - TICKS_PER_REV / 2
            self.acc[k] += d
            self.prev[k] = cur[k]


class Imu:
    def __init__(self):
        self.pipe = None
        self.samples = []
        try:
            import pyrealsense2 as rs
        except Exception as e:
            print(f"IMU unavailable (pyrealsense2): {e}")
            return
        try:
            ctx = rs.context()
            if len(ctx.query_devices()) == 0:
                print("IMU unavailable: no RealSense device")
                return
            cfg = rs.config()
            cfg.enable_stream(rs.stream.gyro)
            cfg.enable_stream(rs.stream.accel)
            self.pipe = rs.pipeline()
            self.pipe.start(cfg, self._cb)
            print("D455 IMU started")
        except Exception as e:
            print(f"IMU unavailable: {type(e).__name__}: {e}")
            self.pipe = None

    def _cb(self, f):
        try:
            if f.is_gyro_frame():
                g = f.as_motion_frame().get_motion_data()
                self.samples.append((f.get_timestamp(), g.x, g.y, g.z))
        except Exception:
            pass

    def bias(self, seconds=1.0):
        if self.pipe is None:
            return (0.0, 0.0, 0.0)
        self.samples.clear()
        time.sleep(seconds)
        if not self.samples:
            return (0.0, 0.0, 0.0)
        a = np.array([(x[1], x[2], x[3]) for x in self.samples])
        return tuple(a.mean(axis=0))

    def integrals(self):
        if not self.samples:
            return (0.0, 0.0, 0.0)
        a = np.array(self.samples)
        ts = a[:, 0] / 1000.0
        out = []
        for i in (1, 2, 3):
            out.append(float(np.trapz(a[:, i], ts)) if len(ts) > 1 else 0.0)
        return tuple(out)

    def stop(self):
        if self.pipe is not None:
            try:
                self.pipe.stop()
            except Exception:
                pass


def run_phase(robot, name, action, duration):
    enc = EncoderTracker(robot)
    robot.send_action(action)
    t0 = time.time()
    while time.time() - t0 < duration:
        enc.sample()
        time.sleep(0.05)
    robot.stop_base()
    # capture coast-down
    t1 = time.time()
    while time.time() - t1 < 0.5:
        enc.sample()
        time.sleep(0.05)
    return enc.acc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--speed", type=float, default=0.15)
    ap.add_argument("--straight-time", type=float, default=6.7)
    ap.add_argument("--turn-rate", type=float, default=20.0)
    ap.add_argument("--turn-time", type=float, default=4.5)
    ap.add_argument("--wheel-radius", type=float, default=0.0635)
    ap.add_argument("--wheelbase", type=float, default=0.414)
    ap.add_argument("--distance", type=float, default=None, help="measured straight distance in m")
    ap.add_argument("--angle", type=float, default=None, help="measured turn angle in deg (if no IMU)")
    ap.add_argument("--skip-straight", action="store_true")
    ap.add_argument("--skip-turn", action="store_true")
    ap.add_argument("--port1", default="/dev/xlerobot_serial_b")
    ap.add_argument("--port2", default="/dev/xlerobot_serial_a")
    args = ap.parse_args()

    cfg = XLerobot2WheelsConfig(
        port1=args.port1, port2=args.port2, id="xlerobot",
        wheel_radius=args.wheel_radius, wheelbase=args.wheelbase,
    )
    robot = XLerobot2Wheels(cfg)
    if not args.yes:
        print("dry run: pass --yes")
        return 0

    imu = Imu()
    _orig_input = builtins.input
    builtins.input = lambda *a, **k: ""
    try:
        robot.connect(calibrate=False)
        print("connected")
        bias = imu.bias(1.0)
        print("gyro bias (rad/s):", tuple(round(x, 6) for x in bias))
        imu.samples.clear()

        if args.skip_straight:
            acc = None
            r_actual = args.wheel_radius
            print("straight test skipped")
        else:
            print(f"\n=== straight: {args.speed} m/s for {args.straight_time}s ===")
            acc = run_phase(robot, "straight", {"x.vel": args.speed, "theta.vel": 0.0}, args.straight_time)
            # Physical wheel angles: left is mirrored in raw feedback.
            left_rad = math.radians(-acc["base_left_wheel"] * 360.0 / TICKS_PER_REV)
            right_rad = math.radians(acc["base_right_wheel"] * 360.0 / TICKS_PER_REV)
            wheel_avg = (left_rad + right_rad) / 2.0
            expected_d = args.speed * args.straight_time
            enc_d = wheel_avg * args.wheel_radius
            print(f"encoder wheel angle avg: {math.degrees(wheel_avg):.2f} deg")
            print(f"expected distance: {expected_d:.3f} m")
            print(f"encoder distance (r={args.wheel_radius}): {enc_d:.3f} m")
            if args.distance is not None:
                r_actual = args.distance / wheel_avg if abs(wheel_avg) > 1e-6 else float("nan")
                print(f"measured distance: {args.distance:.3f} m")
                print(f"inferred wheel radius: {r_actual:.4f} m (config {args.wheel_radius})")
            else:
                r_actual = args.wheel_radius
                print("no --distance given; using config wheel radius")

        if args.skip_turn:
            print("turn test skipped")
            return 0
        # Turn phase: clear gyro after a short settle.
        time.sleep(0.5)
        imu.samples.clear()
        print(f"\n=== turn: {args.turn_rate} deg/s for {args.turn_time}s ===")
        t0 = time.time()
        acc2 = run_phase(robot, "turn", {"x.vel": 0.0, "theta.vel": args.turn_rate}, args.turn_time)
        time.sleep(0.2)
        integrals = imu.integrals()
        print("gyro integrals (rad) x,y,z:", tuple(round(x, 4) for x in integrals))
        axis = int(np.argmax(np.abs(integrals)))
        theta_imu = integrals[axis]
        print(f"actual IMU angle: {math.degrees(theta_imu):.2f} deg (axis {'xyz'[axis]})")
        if args.angle is not None:
            theta_actual = math.radians(args.angle)
            print(f"using measured angle: {args.angle:.2f} deg")
        else:
            theta_actual = theta_imu
        left_rad2 = math.radians(-acc2["base_left_wheel"] * 360.0 / TICKS_PER_REV)
        right_rad2 = math.radians(acc2["base_right_wheel"] * 360.0 / TICKS_PER_REV)
        wheel_diff = r_actual * (right_rad2 - left_rad2)
        if abs(theta_actual) > 1e-4 and abs(wheel_diff) > 1e-6:
            L_actual = abs(wheel_diff / theta_actual)
            print(f"wheel arc difference: {wheel_diff:+.4f} m")
            print(f"inferred wheelbase: {L_actual:.4f} m (config {args.wheelbase})")
            print(f"recommended --robot.wheelbase={L_actual:.4f}")
        else:
            print(f"wheel arc difference: {wheel_diff:+.4f} m")
            print("cannot infer wheelbase: IMU missing and no --angle given")
        return 0
    except Exception:
        traceback.print_exc()
        return 1
    finally:
        builtins.input = _orig_input
        imu.stop()
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
