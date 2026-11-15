#!/usr/bin/env python3
"""Run the official XLeRobot Joy-Con teleop with this robot's calibration.

This is a thin launcher around the unmodified upstream example

    XLeRobot/software/examples/7_xlerobot_2wheels_teleop_joycon.py

The example constructs ``XLerobot2WheelsConfig`` with library defaults
(``wheel_radius=0.05``, ``wheelbase=0.25`` and a full camera set).  This robot
has 63.5 mm wheels on a 414 mm wheelbase and its cameras are owned by
``camera_web.py``, so the launcher injects the measured values and disables the
cameras.  Nothing else is changed: the base ramp, arm IK, head d-pad and
gripper logic all come from the official file.

Official control map (see the upstream docstring):

    base      X = rotate left   B = rotate right
              Y = forward       A = backward        (ramped accel/decel)
    arms      stick  = end-effector X/Z (vertical) and Y (horizontal)
              R / L shoulder = Z up, stick click = Z down
              ZR / ZL = hold to toggle and then ramp the gripper
    head      left Joy-Con d-pad (up/down = tilt, left/right = pan)
    reset     right + (plus) -> both arms and head to the zero pose

Requirements (both need root once):

    sudo apt install -y libhidapi-hidraw0
    sudo tee /etc/udev/rules.d/70-joycon.rules <<'EOF'
    KERNEL=="hidraw*", ATTRS{idVendor}=="057e", MODE="0666"
    EOF
    sudo udevadm control --reload-rules && sudo udevadm trigger

Then ``import hid`` must be the ctypes build (``hid`` on PyPI), not the
``hidapi`` wheel, which only ships the USB backend and cannot see Bluetooth
Joy-Cons.

Stop ``robot_api.py`` first: this script talks to the motor buses directly.

    .venv-robot/bin/python scripts/teleop_joycon_official.py
"""
import argparse
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXAMPLE = os.path.join(
    ROOT, "XLeRobot", "software", "examples",
    "7_xlerobot_2wheels_teleop_joycon.py")

# Measured on this build (see docs/setup.md).
WHEEL_RADIUS = 0.0635
WHEELBASE = 0.414
ROBOT_ID = "xlerobot"
PORT1 = "/dev/xlerobot_serial_b"   # left arm + head
PORT2 = "/dev/xlerobot_serial_a"   # right arm + wheels


def check_hidraw():
    """Fail early with an actionable message instead of a deep traceback."""
    try:
        import hid
    except Exception as e:
        print(f"import hid failed: {e}\nInstall it: uv pip install hid",
              file=sys.stderr)
        return False

    bt = [d for d in hid.enumerate() if d.get("vendor_id") == 0x057E]
    if bt:
        return True

    print("No Bluetooth Joy-Con visible through hidapi.\n", file=sys.stderr)
    print("Two common causes:", file=sys.stderr)
    print("  1. The installed 'hid' is the hidapi wheel (USB backend only). "
          "Bluetooth pads need the ctypes build:\n"
          "       uv pip install --force-reinstall hid\n"
          "     plus the system library: sudo apt install libhidapi-hidraw0",
          file=sys.stderr)
    print("  2. /dev/hidraw* for the pad is root-only. Check with:\n"
          "       ls -l /dev/hidraw*  &&  cat /sys/class/hidraw/hidraw*/"
          "device/uevent | grep Joy-Con\n"
          "     and install the udev rule shown at the top of this file.",
          file=sys.stderr)
    return False


def load_example():
    spec = importlib.util.spec_from_file_location(
        "xlerobot_official_joycon", EXAMPLE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--no-rerun", action="store_true", default=True,
                    help="skip the rerun viewer (default)")
    ap.add_argument("--check-only", action="store_true",
                    help="only verify Joy-Con visibility, do not move")
    args, rest = ap.parse_known_args()

    if not check_hidraw():
        return 1
    if args.check_only:
        print("Joy-Cons visible through hidapi")
        return 0

    mod = load_example()

    # Inject this robot's geometry and skip the cameras (camera_web owns them).
    base_cfg = mod.XLerobot2WheelsConfig

    def config(**kw):
        kw.setdefault("wheel_radius", WHEEL_RADIUS)
        kw.setdefault("wheelbase", WHEELBASE)
        kw.setdefault("cameras", {})
        return base_cfg(**kw)

    mod.XLerobot2WheelsConfig = config
    mod.init_rerun = lambda *a, **k: None
    mod.log_rerun_data = lambda *a, **k: None

    # Default the ports to this robot's udev symlinks unless the caller
    # overrides them.
    argv = list(rest)
    joined = " ".join(argv)
    if "--robot.port1" not in joined:
        argv.append(f"--robot.port1={PORT1}")
    if "--robot.port2" not in joined:
        argv.append(f"--robot.port2={PORT2}")
    if "--robot.id" not in joined:
        argv.append(f"--robot.id={ROBOT_ID}")
    sys.argv = [EXAMPLE] + argv
    print("running:", EXAMPLE)
    print("ports:", PORT1, PORT2, "| id:", ROBOT_ID,
          f"| wheel_radius={WHEEL_RADIUS} wheelbase={WHEELBASE}")
    mod.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
