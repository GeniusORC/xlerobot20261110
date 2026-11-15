#!/usr/bin/env python3
"""Joystick / gamepad teleop for the XLeRobot 2-wheel base.

Two backends:

  * ``js``    - the legacy Linux joystick API (``/dev/input/js*``).  Usually
                world-readable, so it works without joining the ``input``
                group.  This is the default when a ``js*`` node exists.
  * ``evdev`` - ``/dev/input/event*``.  Needed for Bluetooth controllers such
                as a Nintendo Joy-Con, which often have no ``js`` node.  This
                requires read access to the event device (``input`` group or a
                udev rule).

Default mapping (change with the flags below):

  left stick  up/down    -> forward / backward   (axis 1)
  right stick left/right -> turn left / right    (axis 3)
  A / cross              -> stop
  B / circle             -> emergency stop
  Start / options        -> quit

The base is driven through ``robot_api``'s short-lease ``/velocity`` endpoint
at 20 Hz.  The lease expires after 0.6 s, so releasing the stick, unplugging the
controller or killing this script stops the robot by itself.

Examples
--------
    source scripts/env.sh robot
    python scripts/teleop_joy.py --list          # what is connected?
    python scripts/teleop_joy.py --probe         # print raw axis/button ids
    python scripts/teleop_joy.py                 # drive
    python scripts/teleop_joy.py --single-stick  # one Joy-Con (stick = axis 0/1)
    python scripts/teleop_joy.py --dry-run       # print velocities, no motion
"""
import argparse
import glob
import os
import struct
import sys
import threading
import time

import requests

ROBOT_API = os.environ.get("ROBOT_API", "http://127.0.0.1:8090").rstrip("/")
HEALTH_API = os.environ.get("HEALTH_API", "http://127.0.0.1:8084").rstrip("/")

JS_EVENT_BUTTON = 0x01
JS_EVENT_AXIS = 0x02
JS_EVENT_INIT = 0x80

AXIS_MAX = 32767.0


def _fmt_axis(v):
    return f"{v:+.2f}"


class JoyState:
    """Latest axis/button state, filled by a reader thread."""

    def __init__(self):
        self.axes = {}
        self.buttons = {}
        self.quit = False
        self.estop = False
        self.last_event = time.time()
        self.lock = threading.Lock()

    def set_axis(self, number, value):
        with self.lock:
            self.axes[number] = float(value)
            self.last_event = time.time()

    def set_button(self, number, pressed):
        with self.lock:
            self.buttons[number] = bool(pressed)
            self.last_event = time.time()


# --------------------------------------------------------------------------
# legacy /dev/input/js* backend
# --------------------------------------------------------------------------
def js_devices():
    return sorted(glob.glob("/dev/input/js*"))


def js_name(path):
    try:
        with open(f"/sys/class/input/{os.path.basename(path)}/device/name") as f:
            return f.read().strip()
    except Exception:
        return "?"


def read_js(path, state, quit_btn, estop_btn, stop_btn, verbose=False):
    """Read 8-byte ``js_event`` structs until the file closes."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except Exception as e:
        print(f"cannot open {path}: {type(e).__name__}: {e}", flush=True)
        return
    buf = b""
    try:
        while not state.quit:
            try:
                chunk = os.read(fd, 8 * 64)
            except BlockingIOError:
                chunk = b""
            except OSError:
                break
            if not chunk:
                time.sleep(0.004)
                continue
            buf += chunk
            while len(buf) >= 8:
                _t, value, etype, number = struct.unpack("=IhBB", buf[:8])
                buf = buf[8:]
                etype &= ~JS_EVENT_INIT
                if etype == JS_EVENT_AXIS:
                    state.set_axis(number, value / AXIS_MAX)
                    if verbose:
                        print(f"AXIS  {number:2d}  {value:+6d}", flush=True)
                elif etype == JS_EVENT_BUTTON:
                    pressed = value != 0
                    state.set_button(number, pressed)
                    if verbose:
                        print(f"BTN   {number:2d}  {'down' if pressed else 'up'}",
                              flush=True)
                    if pressed:
                        if quit_btn is not None and number == quit_btn:
                            state.quit = True
                        elif estop_btn is not None and number == estop_btn:
                            state.estop = True
                        elif stop_btn is not None and number == stop_btn:
                            with state.lock:
                                state.axes.clear()
    finally:
        try:
            os.close(fd)
        except Exception:
            pass


# --------------------------------------------------------------------------
# evdev backend
# --------------------------------------------------------------------------
def evdev_devices():
    try:
        import evdev
    except Exception as e:
        print("evdev not installed:", e, flush=True)
        return []
    out = []
    for p in sorted(evdev.list_devices()):
        try:
            d = evdev.InputDevice(p)
        except Exception:
            continue
        caps = d.capabilities()
        abs_codes = caps.get(evdev.ecodes.EV_ABS, [])
        key_codes = caps.get(evdev.ecodes.EV_KEY, [])
        if abs_codes or any(k >= evdev.ecodes.BTN_JOYSTICK
                            for k in key_codes):
            out.append((p, d.name, abs_codes, key_codes))
    return out


def read_evdev(path, state, quit_btn, estop_btn, stop_btn, verbose=False):
    import evdev
    from evdev import ecodes
    try:
        dev = evdev.InputDevice(path)
    except Exception as e:
        print(f"cannot open {path}: {type(e).__name__}: {e}", flush=True)
        return
    axis_names = {
        ecodes.ABS_X: 0, ecodes.ABS_Y: 1,
        ecodes.ABS_RX: 2, ecodes.ABS_RY: 3,
        ecodes.ABS_Z: 4, ecodes.ABS_RZ: 5,
        ecodes.ABS_HAT0X: 6, ecodes.ABS_HAT0Y: 7,
    }
    btn_names = {
        ecodes.BTN_SOUTH: 0, ecodes.BTN_A: 0,
        ecodes.BTN_EAST: 1, ecodes.BTN_B: 1,
        ecodes.BTN_START: 8, ecodes.BTN_SELECT: 9,
    }
    try:
        for ev in dev.read_loop():
            if state.quit:
                break
            if ev.type == ecodes.EV_ABS:
                n = axis_names.get(ev.code, ev.code)
                lo, hi = 0, 255
                try:
                    info = dev.absinfo(ev.code)
                    lo, hi = info.min, info.max
                except Exception:
                    pass
                span = max(1, hi - lo)
                value = 2.0 * (ev.value - lo) / span - 1.0
                state.set_axis(n, value)
                if verbose:
                    print(f"ABS   {ecodes.ABS.get(ev.code, ev.code)} -> {n} "
                          f"{value:+.2f}", flush=True)
            elif ev.type == ecodes.EV_KEY:
                n = btn_names.get(ev.code, ev.code)
                pressed = ev.value != 0
                state.set_button(n, pressed)
                if verbose:
                    print(f"KEY   {ecodes.BTN.get(ev.code, ev.code)} -> {n} "
                          f"{'down' if pressed else 'up'}", flush=True)
                if pressed:
                    if quit_btn is not None and n == quit_btn:
                        state.quit = True
                    elif estop_btn is not None and n == estop_btn:
                        state.estop = True
                    elif stop_btn is not None and n == stop_btn:
                        with state.lock:
                            state.axes.clear()
    except Exception as e:
        print(f"evdev read stopped: {type(e).__name__}: {e}", flush=True)
    finally:
        try:
            dev.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# driving
# --------------------------------------------------------------------------
def deadzone(v, dz):
    if abs(v) <= dz:
        return 0.0
    return (1.0 if v > 0 else -1.0) * (abs(v) - dz) / (1.0 - dz)


def post_velocity(x, theta, dry_run=False):
    if dry_run:
        print(f"x={x:+.2f} m/s  theta={theta:+6.1f} deg/s", flush=True)
        return True
    try:
        r = requests.post(f"{ROBOT_API}/velocity", json={"x": x, "theta": theta},
                          timeout=0.4)
        r.raise_for_status()
        return True
    except Exception:
        return False


def post_stop(dry_run=False):
    if dry_run:
        print("STOP", flush=True)
        return
    for payload in ({"stop": True},):
        try:
            requests.post(f"{ROBOT_API}/velocity", json=payload, timeout=0.5)
        except Exception:
            pass


def follow_is_on():
    try:
        r = requests.get(f"{HEALTH_API}/api/status", timeout=0.5)
        return bool(r.json().get("follow"))
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="list input devices")
    ap.add_argument("--probe", action="store_true",
                    help="print raw axis/button events for 30 s")
    ap.add_argument("--device", help="explicit /dev/input/jsX or eventX path")
    ap.add_argument("--backend", choices=("auto", "js", "evdev"), default="auto")
    ap.add_argument("--single-stick", action="store_true",
                    help="one stick: Y drives, X turns")
    ap.add_argument("--axis-x", type=int, default=1)
    ap.add_argument("--axis-theta", type=int, default=3)
    ap.add_argument("--invert-x", action="store_true")
    ap.add_argument("--invert-theta", action="store_true")
    ap.add_argument("--deadzone", type=float, default=0.12)
    ap.add_argument("--max-x", type=float, default=0.22,
                    help="m/s (robot_api clamps to -0.22..0.30)")
    ap.add_argument("--max-theta", type=float, default=25.0,
                    help="deg/s (robot_api clamps to +-25)")
    ap.add_argument("--stop-btn", type=int, default=0)
    ap.add_argument("--estop-btn", type=int, default=1)
    ap.add_argument("--quit-btn", type=int, default=8)
    ap.add_argument("--rate", type=float, default=20.0, help="Hz")
    ap.add_argument("--takeover", action="store_true",
                    help="turn off the health station's follow mode first")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.single_stick:
        args.axis_theta = 0

    if args.list:
        print("legacy joystick nodes:")
        nodes = js_devices()
        for p in nodes:
            print(f"  {p}  {js_name(p)!r}")
        if not nodes:
            print("  (none)")
        print("evdev candidates (needs read access):")
        devs = evdev_devices()
        for p, name, axes, keys in devs:
            print(f"  {p}  {name!r}  axes={axes}  buttons={len(keys)}")
        if not devs:
            print("  (none visible; you may need the 'input' group)")
        return

    # pick a device
    path = args.device
    backend = args.backend
    if path is None:
        if backend in ("auto", "js") and js_devices():
            path = js_devices()[0]
            backend = "js"
        else:
            devs = evdev_devices()
            if devs:
                path, backend = devs[0][0], "evdev"
            else:
                print("No joystick found. Plug it in / pair it, then run "
                      "`--list`.", flush=True)
                return
    elif backend == "auto":
        backend = "js" if path.startswith("/dev/input/js") else "evdev"

    print(f"device: {path} ({backend})", flush=True)
    if args.takeover:
        try:
            requests.post(f"{HEALTH_API}/api/follow", json={"on": False},
                          timeout=0.8)
            print("health station follow: off", flush=True)
        except Exception as e:
            print("could not turn follow off:", e, flush=True)
    elif follow_is_on():
        print("WARNING: 采集时跟随 is on in the health station; turn it off "
              "or it will fight the joystick.", flush=True)

    state = JoyState()
    reader = threading.Thread(
        target=(read_js if backend == "js" else read_evdev),
        args=(path, state, args.quit_btn, args.estop_btn, args.stop_btn,
              args.probe),
        daemon=True)
    reader.start()

    if args.probe:
        print("Probing for 30 s - move sticks and press buttons.", flush=True)
        time.sleep(30)
        state.quit = True
        print("probe done", flush=True)
        return

    period = 1.0 / max(args.rate, 1.0)
    # Learn each axis's resting value.  Sticks rest at 0, but triggers often
    # rest at -1, and some pads report an offset; without this the base would
    # drive at full speed before the user touches anything.
    time.sleep(0.5)
    with state.lock:
        center = dict(state.axes)
    print("axis centres:", {k: round(v, 2) for k, v in sorted(center.items())},
          flush=True)

    def axis_value(number):
        raw = ax.get(number, center.get(number, 0.0))
        c = center.get(number, 0.0)
        span = max(1e-3, 1.0 - abs(c))
        return max(-1.0, min(1.0, (raw - c) / span))

    sent = False
    try:
        while not state.quit:
            if not reader.is_alive():
                print("controller lost - stopping", flush=True)
                break
            if state.estop:
                print("EMERGENCY STOP", flush=True)
                post_stop(args.dry_run)
                break
            with state.lock:
                ax = dict(state.axes)
            x = deadzone(axis_value(args.axis_x), args.deadzone)
            th = deadzone(axis_value(args.axis_theta), args.deadzone)
            if args.invert_x:
                x = -x
            if args.invert_theta:
                th = -th
            vx = x * args.max_x
            vth = th * args.max_theta
            if abs(vx) < 0.005 and abs(vth) < 0.2:
                if sent:
                    post_velocity(0.0, 0.0, args.dry_run)
                    sent = False
            else:
                post_velocity(vx, vth, args.dry_run)
                sent = True
            time.sleep(period)
    except KeyboardInterrupt:
        pass
    finally:
        post_stop(args.dry_run)
        print("stopped", flush=True)


if __name__ == "__main__":
    main()
