#!/usr/bin/env python3
"""Interactive direction check after the wheel-inversion patch.

Shows confirmation dialogs on the laptop, then moves a very short distance:
  forward ~5 cm, left turn ~10 deg.

  source scripts/env.sh robot
  python scripts/test_direction.py
"""
import builtins
import subprocess
import time
import traceback

from lerobot_robot_xlerobot_2wheels import XLerobot2Wheels, XLerobot2WheelsConfig


def ask(text: str, timeout: int = 60) -> bool:
    try:
        r = subprocess.run(
            ["zenity", "--question", "--title", "XLeRobot direction check",
             "--text", text, "--timeout", str(timeout)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return r.returncode == 0
    except Exception:
        return False


def info(text: str) -> None:
    try:
        subprocess.run(
            ["zenity", "--info", "--title", "XLeRobot direction check",
             "--text", text, "--timeout", "5"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def main() -> int:
    if not ask("机器人将前进约 5 cm。\n请确认周围安全后点“是”。"):
        print("ABORTED before forward")
        return 2

    cfg = XLerobot2WheelsConfig(
        port1="/dev/xlerobot_serial_b",
        port2="/dev/xlerobot_serial_a",
        id="xlerobot",
        wheel_radius=0.0635,
        wheelbase=0.414,
    )
    _orig_input = builtins.input
    builtins.input = lambda *a, **k: ""
    robot = None
    try:
        # The CH343/USB link occasionally drops a single status packet during
        # configure(); retry with a fresh robot object.
        for attempt in range(3):
            candidate = XLerobot2Wheels(cfg)
            try:
                candidate.connect(calibrate=False)
                robot = candidate
                print(f"connected (attempt {attempt + 1})")
                break
            except Exception as e:
                print(f"connect attempt {attempt + 1} failed: {type(e).__name__}: {e}")
                try:
                    candidate.disconnect()
                except Exception:
                    pass
                time.sleep(1)
        if robot is None:
            print("connect failed after 3 attempts")
            return 1

        robot.send_action({"x.vel": 0.05, "theta.vel": 0.0})
        time.sleep(1.0)
        robot.stop_base()
        time.sleep(0.5)
        forward_ok = ask("刚才机器人是向“前”移动吗？\n是=方向正确，否=方向仍反。", timeout=90)
        print("FORWARD_OK" if forward_ok else "FORWARD_REVERSED")

        info("接下来原地左转约 10°。")
        if not ask("机器人将原地左转约 10°。\n请确认周围安全后点“是”。"):
            print("ABORTED before left turn")
            return 2
        robot.send_action({"x.vel": 0.0, "theta.vel": 10.0})
        time.sleep(1.0)
        robot.stop_base()
        time.sleep(0.5)
        left_ok = ask("刚才机器人是向“左”转吗？\n是=方向正确，否=转向仍反。", timeout=90)
        print("LEFT_OK" if left_ok else "LEFT_REVERSED")
        return 0
    except Exception:
        traceback.print_exc()
        return 1
    finally:
        builtins.input = _orig_input
        if robot is not None:
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
