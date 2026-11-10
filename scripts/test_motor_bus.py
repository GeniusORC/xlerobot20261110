#!/usr/bin/env python3
"""Read-only scan of the two XLeRobot motor buses (LeRobot 0.6.1).

Power the robot first. This scans every supported baud rate and only pings;
it never enables torque or writes registers.

Expected mapping for the 2-wheel build:
  bus1 (left arm + head): IDs 1-8
  bus2 (right arm + 2 wheels): IDs 1-6, 9, 10
"""
import argparse

from lerobot.motors.feetech import FeetechMotorsBus

PORTS = ["/dev/xlerobot_serial_a", "/dev/xlerobot_serial_b"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ports", nargs="*", default=PORTS)
    args = ap.parse_args()

    for port in args.ports:
        print(f"\n=== {port} ===")
        try:
            result = FeetechMotorsBus.scan_port(port)
        except Exception as e:
            print(f"scan failed: {type(e).__name__}: {e}")
            continue
        print(f"result: {result}")
        if not result:
            print("No motors responded. Is the 12V servo power on?")
        else:
            for baud, ids in result.items():
                kind = "bus1 (left arm + head)" if set(ids) >= {7, 8} else "bus2 (right arm + wheels)"
                print(f"  {baud} baud, IDs {ids} -> {kind}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
