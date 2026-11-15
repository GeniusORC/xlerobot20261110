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

  left stick   up/down    -> forward / backward
  left stick   left/right -> turn left / right (single-stick mode)
  left d-pad   up/down    -> head tilt up / down
  left d-pad   left/right -> head pan left / right
  right A                 -> stop (centre the stick to drive again)
  right B                 -> emergency stop
  right X                 -> cycle speed preset
  right Y                 -> health station auto-aim (自动取景)
  right R (shoulder)      -> centre the head
  right ZR (trigger)      -> start a 10 s capture
  right +                 -> quit
  right stick click       -> print robot/health status

Bindings accept a symbolic name (``R:A``) or a raw button number, e.g.
``--btn-aim 3``.  Use ``--probe`` to see the names your pad reports.

Every joystick node is opened, so a Joy-Con pair works as two devices: the
left stick drives and the right stick turns.  A single Joy-Con uses its one
stick (``--single-stick``).  Axis/button numbers of the second device are
offset by 100 to keep them apart.

The base is driven through ``robot_api``'s short-lease ``/velocity`` endpoint
at 20 Hz.  The lease expires after 0.6 s, so releasing the stick, unplugging the
controller or killing this script stops the robot by itself.

Examples
--------
    source scripts/env.sh robot
    python scripts/teleop_joy.py --list          # what is connected?
    python scripts/teleop_joy.py --connect       # wake paired Joy-Cons, then drive
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
        self.buttons = {}       # numeric key (offset + number) -> bool
        self.named = {}         # "L:UP", "R:A", ... -> bool
        self.quit = False
        self.estop = False
        self.last_event = time.time()
        self.lock = threading.Lock()

    def set_axis(self, number, value):
        with self.lock:
            self.axes[number] = float(value)
            self.last_event = time.time()

    def set_button(self, number, name, pressed):
        with self.lock:
            self.buttons[number] = bool(pressed)
            if name:
                self.named[name] = bool(pressed)
            self.last_event = time.time()


# Joy-Con button names per backend, so bindings can be written as "R:A"
# instead of a fragile raw number.  The js API numbers buttons in the order the
# driver declares them, which is ascending key code.
JOYCON_JS_BTN = {
    "l": {0: "ZL", 1: "R", 2: "ZR", 3: "R2", 4: "ZR2", 5: "MINUS",
          6: "STICK", 7: "UP", 8: "DOWN", 9: "LEFT", 10: "RIGHT"},
    "r": {0: "A", 1: "B", 2: "X", 3: "Y", 4: "R", 5: "ZR", 6: "R2",
          7: "ZR2", 8: "PLUS", 9: "HOME", 10: "STICK"},
}

BTN_ALIASES = {
    "SOUTH": "A", "EAST": "B", "NORTH": "X", "WEST": "Y",
    "TL": "R", "TR": "ZR", "TL2": "R2", "TR2": "ZR2",
    "START": "PLUS", "SELECT": "MINUS", "MODE": "HOME",
    "THUMBL": "STICK", "THUMBR": "STICK", "Z": "ZL",
    "DPAD_UP": "UP", "DPAD_DOWN": "DOWN",
    "DPAD_LEFT": "LEFT", "DPAD_RIGHT": "RIGHT",
}


def device_label(name, index):
    """Short stable prefix: L / R for a Joy-Con pair, D<idx> otherwise."""
    low = (name or "").lower()
    if "joy-con" in low:
        if "(l)" in low or low.endswith("l)"):
            return "L"
        if "(r)" in low or low.endswith("r)"):
            return "R"
    return f"D{index}"


def js_btn_map(name):
    low = (name or "").lower()
    if "joy-con" in low and "(l)" in low:
        return JOYCON_JS_BTN["l"]
    if "joy-con" in low and "(r)" in low:
        return JOYCON_JS_BTN["r"]
    return {}


def evdev_btn_name(code, ecodes):
    raw = ecodes.BTN.get(code) or ecodes.KEY.get(code)
    if not isinstance(raw, str):
        return None
    if raw.startswith("BTN_"):
        raw = raw[4:]
    return BTN_ALIASES.get(raw, raw)


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


def read_js(path, state, offset, label, btn_map, verbose=False):
    """Read 8-byte ``js_event`` structs until the file closes.

    ``offset`` namespaces this device's axes/buttons (0, 100, 200, ...) so a
    left and a right Joy-Con can be used together without clashing on axis 0.
    """
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
                    state.set_axis(offset + number, value / AXIS_MAX)
                    if verbose:
                        print(f"AXIS  {offset + number:3d}  {value:+6d}", flush=True)
                elif etype == JS_EVENT_BUTTON:
                    pressed = value != 0
                    sym = btn_map.get(number)
                    full = f"{label}:{sym}" if sym else "?"
                    state.set_button(offset + number,
                                     full if sym else None, pressed)
                    if verbose:
                        print(f"BTN   {offset + number:3d}  {full:8} "
                              f"{'down' if pressed else 'up'}", flush=True)
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


def read_evdev(path, state, offset, label, btn_map, verbose=False):
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
    try:
        for ev in dev.read_loop():
            if state.quit:
                break
            if ev.type == ecodes.EV_ABS:
                n = offset + axis_names.get(ev.code, ev.code)
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
                n = offset + btn_map.get(ev.code, ev.code)
                pressed = ev.value != 0
                sym = evdev_btn_name(ev.code, ecodes)
                full = f"{label}:{sym}" if sym else "?"
                state.set_button(n, full if sym else None, pressed)
                if verbose:
                    print(f"KEY   {ecodes.BTN.get(ev.code, ev.code)} -> "
                          f"{full:8} {'down' if pressed else 'up'}", flush=True)
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
    return bool(health_status().get("follow"))


def health_status():
    try:
        r = requests.get(f"{HEALTH_API}/api/status", timeout=1.0)
        return r.json()
    except Exception:
        return {}


def health_post(path, payload=None, dry_run=False):
    if dry_run:
        print(f"[dry-run] POST {path} {payload or {}}", flush=True)
        return {"dry_run": True}
    try:
        r = requests.post(f"{HEALTH_API}{path}", json=payload or {}, timeout=3.0)
        try:
            return r.json()
        except Exception:
            return {"status": r.status_code}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def post_head(dry_run=False, **kw):
    if dry_run:
        print(f"[dry-run] HEAD {kw}", flush=True)
        return
    try:
        requests.post(f"{ROBOT_API}/head", json=kw, timeout=0.5)
    except Exception:
        pass


def robot_status():
    try:
        r = requests.get(f"{ROBOT_API}/status", timeout=1.0)
        return r.json()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def connect_bluetooth(match="joy-con", wait=6.0):
    """Connect already-paired Bluetooth controllers.

    A sleeping Joy-Con cannot be woken over Bluetooth; press any button on
    the controller first.  This only saves the manual ``bluetoothctl connect``.
    """
    import re
    import subprocess

    try:
        out = subprocess.run(["bluetoothctl", "devices"], capture_output=True,
                             text=True, timeout=8).stdout
    except Exception as e:
        print(f"--connect: bluetoothctl unavailable ({type(e).__name__})",
              flush=True)
        return False
    found = []
    for line in out.splitlines():
        m = re.match(r"Device\s+([0-9A-Fa-f:]{17})\s+(.*)", line.strip())
        if m and match in m.group(2).lower():
            found.append((m.group(1), m.group(2)))
    if not found:
        print(f"--connect: no paired device matching {match!r}", flush=True)
        return False

    ok = False
    for mac, name in found:
        try:
            r = subprocess.run(["bluetoothctl", "connect", mac],
                               capture_output=True, text=True, timeout=25)
            text = (r.stdout + r.stderr).strip().splitlines()
            tail = text[-1] if text else ""
            print(f"--connect: {name} ({mac}) -> {tail}", flush=True)
            ok = ok or r.returncode == 0
        except Exception as e:
            print(f"--connect: {name} failed ({type(e).__name__})", flush=True)

    # udev needs a moment to create the js/event nodes.
    deadline = time.time() + wait
    while time.time() < deadline:
        if any("joy-con" in js_name(p).lower() for p in js_devices()):
            break
        time.sleep(0.25)
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="list input devices")
    ap.add_argument("--probe", action="store_true",
                    help="print raw axis/button events for 30 s")
    ap.add_argument("--probe-seconds", type=float, default=30.0)
    ap.add_argument("--device", nargs="+",
                    help="one or more /dev/input/jsX or eventX paths")
    ap.add_argument("--backend", choices=("auto", "js", "evdev"), default="auto")
    ap.add_argument("--single-stick", action="store_true",
                    help="one stick: Y drives, X turns")
    ap.add_argument("--axis-x", type=int, default=None,
                    help="axis for forward/back (default 1; device[1] uses 101)")
    ap.add_argument("--axis-theta", type=int, default=None,
                    help="axis for turning (default 3, or 100 for a 2nd device)")
    ap.add_argument("--invert-x", action="store_true")
    ap.add_argument("--invert-theta", action="store_true")
    ap.add_argument("--no-auto-invert", action="store_true",
                    help="keep raw Joy-Con stick polarity")
    ap.add_argument("--deadzone", type=float, default=0.12)
    ap.add_argument("--max-x", type=float, default=0.22,
                    help="m/s (robot_api clamps to -0.22..0.30)")
    ap.add_argument("--max-theta", type=float, default=25.0,
                    help="deg/s (robot_api clamps to +-25)")
    ap.add_argument("--btn-stop", default="R:A",
                    help="stop button (name like R:A, or a raw number)")
    ap.add_argument("--btn-estop", default="R:B",
                    help="emergency stop button")
    ap.add_argument("--btn-quit", default="R:PLUS", help="quit button")
    ap.add_argument("--btn-speed", default="R:X",
                    help="cycle the speed preset")
    ap.add_argument("--btn-aim", default="R:Y",
                    help="health station auto-aim (自动取景)")
    ap.add_argument("--btn-center", default="R:R", help="centre the head")
    ap.add_argument("--btn-capture", default="R:ZR",
                    help="start a 10 s capture in the health station")
    ap.add_argument("--btn-status", default="R:STICK",
                    help="print robot/health status")
    ap.add_argument("--no-actions", action="store_true",
                    help="drive only, ignore every button binding")
    ap.add_argument("--head-step", type=float, default=3.0,
                    help="head units per tick while a d-pad button is held")
    ap.add_argument("--head-rate", type=float, default=8.0,
                    help="head command rate in Hz")
    ap.add_argument("--capture-seconds", type=float, default=10.0)
    ap.add_argument("--rate", type=float, default=20.0, help="Hz")
    ap.add_argument("--connect", action="store_true",
                    help="connect already-paired Bluetooth Joy-Cons first")
    ap.add_argument("--takeover", action="store_true",
                    help="turn off the health station's follow mode first")
    ap.add_argument("--verbose", action="store_true",
                    help="print the velocity actually sent")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.single_stick and args.axis_theta is None:
        args.axis_theta = 0

    if args.connect:
        connect_bluetooth()

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

    # pick devices
    def _not_a_keyboard(name):
        low = name.lower()
        return not any(k in low for k in (
            "keyboard", "mouse", "touchpad", "lid switch", "video bus",
            "hda ", "hdmi", "button"))

    devices = []
    if args.device:
        for p in args.device:
            b = args.backend
            if b == "auto":
                b = "js" if p.startswith("/dev/input/js") else "evdev"
            devices.append((p, b))
    else:
        # Sort by name so a Joy-Con pair is always (L) then (R) regardless of
        # which js* number the kernel handed out.
        js = sorted((p for p in js_devices() if _not_a_keyboard(js_name(p))),
                    key=lambda p: js_name(p).lower())
        if js and args.backend in ("auto", "js"):
            devices = [(p, "js") for p in js]
        else:
            devs = sorted(((p, n) for p, n, _a, _k in evdev_devices()
                           if _not_a_keyboard(n)), key=lambda pn: pn[1].lower())
            if devs and args.backend in ("auto", "evdev"):
                devices = [(p, "evdev") for p, _n in devs]
    if not devices:
        print("No joystick found. Plug it in / pair it, then run `--list`.",
              flush=True)
        return

    names = " ".join(js_name(p) if b == "js" else "" for p, b in devices).lower()

    if args.axis_x is None:
        args.axis_x = 1
    if args.axis_theta is None:
        if len(devices) >= 2:
            # The second device's own stick: js always reports axes 0/1,
            # evdev reports the right stick as ABS_RX/ABS_RY (2/3).
            args.axis_theta = 100 + (0 if devices[1][1] == "js" else 2)
        elif "joy-con" in names:
            args.axis_theta = 0        # one Joy-Con: single stick
        else:
            args.axis_theta = 3

    for i, (p, b) in enumerate(devices):
        label = js_name(p) if b == "js" else ""
        if i == 0:
            role = "drives + turns" if args.axis_theta < 100 else "drives"
        elif i == 1:
            role = "turns"
        else:
            role = "unused"
        print(f"device[{i}]: {p} ({b}) {label!r} -> {role}", flush=True)

    # Linux reports a stick pushed up / left as negative.  A Joy-Con follows
    # that convention, so raw Y and X must be negated to get "up = forward"
    # and "left = turn left".  Other pads usually need the same; pass
    # --no-auto-invert if a pad already reports the opposite.
    if "joy-con" in names and not args.no_auto_invert:
        args.invert_x = True
        args.invert_theta = True
        print("Joy-Con detected: inverting sticks (up=forward, left=left)",
              flush=True)

    print(f"mapping: axis x={args.axis_x} theta={args.axis_theta} "
          f"deadzone={args.deadzone} invert=({args.invert_x},"
          f"{args.invert_theta})", flush=True)
    if not args.no_actions:
        print("buttons: stop={} estop={} speed={} aim={} centre={} "
              "capture={} status={} quit={}".format(
                  args.btn_stop, args.btn_estop, args.btn_speed, args.btn_aim,
                  args.btn_center, args.btn_capture, args.btn_status,
                  args.btn_quit), flush=True)
        print("head: left d-pad up/down/left/right", flush=True)
    if args.takeover:
        try:
            requests.post(f"{HEALTH_API}/api/follow", json={"on": False},
                          timeout=0.8)
            requests.post(f"{HEALTH_API}/api/track", json={"on": False},
                          timeout=0.8)
            print("health station follow + head tracking: off", flush=True)
        except Exception as e:
            print("could not take over from the health station:", e, flush=True)
    else:
        hs = health_status()
        if hs.get("follow"):
            print("WARNING: 采集时跟随 is on in the health station; use "
                  "--takeover or it will fight the joystick.", flush=True)
        if hs.get("track"):
            print("WARNING: head tracking is on; it will fight the d-pad head "
                  "control. Use --takeover to switch it off.", flush=True)

    state = JoyState()
    readers = []
    for i, (p, b) in enumerate(devices):
        dev_name = js_name(p) if b == "js" else ""
        label_i = device_label(dev_name, i)
        btn_map = js_btn_map(dev_name) if b == "js" else {}
        t = threading.Thread(
            target=(read_js if b == "js" else read_evdev),
            args=(p, state, i * 100, label_i, btn_map, args.probe),
            daemon=True)
        t.start()
        readers.append(t)

    if args.probe:
        print(f"Probing for {args.probe_seconds:.0f} s - move sticks and press "
              f"buttons.", flush=True)
        time.sleep(max(1.0, args.probe_seconds))
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
    for label, num in (("x", args.axis_x), ("theta", args.axis_theta)):
        if num not in center:
            print(f"WARNING: axis {num} ({label}) not reported; available axes "
                  f"are {sorted(center)}. Pass --axis-{label} N.", flush=True)

    def axis_value(number):
        raw = ax.get(number, center.get(number, 0.0))
        c = center.get(number, 0.0)
        span = max(1e-3, 1.0 - abs(c))
        return max(-1.0, min(1.0, (raw - c) / span))

    # Speed presets: start at the requested maximum, then faster (still inside
    # robot_api's clamps).
    presets = [(args.max_x, args.max_theta)]
    for factor in (1.6, 2.4):
        presets.append((min(args.max_x * factor, 0.30),
                        min(args.max_theta * 1.5, 25.0)))
    speed_i = 0

    specs = [args.btn_stop, args.btn_estop, args.btn_quit, args.btn_speed,
             args.btn_aim, args.btn_center, args.btn_capture, args.btn_status]

    def down(spec):
        """True if a binding is held; accepts 'R:A' or a raw button number."""
        if spec is None:
            return False
        s = str(spec)
        if s.lstrip("-").isdigit():
            return bool(btns.get(int(s), False))
        return bool(named.get(s, False))

    def edge(spec):
        return down(spec) and not prev.get(spec, False)

    prev = {}
    stop_latched = False
    head_next = 0.0
    sent = False
    last_print = 0.0
    try:
        while not state.quit:
            if not all(t.is_alive() for t in readers):
                print("controller lost - stopping", flush=True)
                break
            if state.estop:
                print("EMERGENCY STOP", flush=True)
                post_stop(args.dry_run)
                break
            with state.lock:
                ax = dict(state.axes)
                named = dict(state.named)
                btns = dict(state.buttons)

            if not args.no_actions:
                if edge(args.btn_quit):
                    print("quit button pressed", flush=True)
                    break
                if edge(args.btn_estop):
                    state.estop = True
                if edge(args.btn_stop):
                    post_velocity(0.0, 0.0, args.dry_run)
                    sent = False
                    stop_latched = True
                    print("STOP - centre the stick to drive again", flush=True)
                if edge(args.btn_speed):
                    speed_i = (speed_i + 1) % len(presets)
                    print(f"speed {speed_i + 1}/{len(presets)}: "
                          f"{presets[speed_i][0]:.2f} m/s, "
                          f"{presets[speed_i][1]:.0f} deg/s", flush=True)
                if edge(args.btn_aim):
                    print("auto-aim:", health_post("/api/aim", {},
                                                   args.dry_run), flush=True)
                if edge(args.btn_center):
                    print("head centre", flush=True)
                    post_head(args.dry_run, center=True)
                if edge(args.btn_capture):
                    print("capture:", health_post(
                        "/api/start", {"duration": args.capture_seconds},
                        args.dry_run), flush=True)
                if edge(args.btn_status):
                    st = robot_status()
                    hs = health_status()
                    print(f"status: robot={'up' if 'error' not in st else st} "
                          f"x={st.get('x_vel')} theta={st.get('theta_vel')} | "
                          f"follow={hs.get('follow')} track={hs.get('track')} "
                          f"dist={hs.get('distance_m')}", flush=True)

                # Head: hold a d-pad direction to pan/tilt in small steps.
                now = time.time()
                if now >= head_next:
                    dpan = dtilt = 0.0
                    if down("L:LEFT"):
                        dpan -= args.head_step
                    if down("L:RIGHT"):
                        dpan += args.head_step
                    if down("L:UP"):
                        dtilt -= args.head_step
                    if down("L:DOWN"):
                        dtilt += args.head_step
                    if dpan or dtilt:
                        post_head(args.dry_run, dpan=dpan, dtilt=dtilt)
                        head_next = now + 1.0 / max(args.head_rate, 1.0)

            x = deadzone(axis_value(args.axis_x), args.deadzone)
            th = deadzone(axis_value(args.axis_theta), args.deadzone)
            if args.invert_x:
                x = -x
            if args.invert_theta:
                th = -th
            vx = x * presets[speed_i][0]
            vth = th * presets[speed_i][1]
            if stop_latched:
                if abs(x) < 0.1 and abs(th) < 0.1:
                    stop_latched = False
                    print("stop released", flush=True)
                else:
                    vx = vth = 0.0
            if args.verbose and time.time() - last_print > 0.5:
                if abs(vx) > 0.005 or abs(vth) > 0.2:
                    print(f"x={vx:+.2f} m/s  theta={vth:+6.1f} deg/s",
                          flush=True)
                    last_print = time.time()
            if abs(vx) < 0.005 and abs(vth) < 0.2:
                if sent:
                    post_velocity(0.0, 0.0, args.dry_run)
                    sent = False
            else:
                post_velocity(vx, vth, args.dry_run)
                sent = True
            for spec in specs:
                prev[spec] = down(spec)
            time.sleep(period)
    except KeyboardInterrupt:
        pass
    finally:
        post_stop(args.dry_run)
        print("stopped", flush=True)


if __name__ == "__main__":
    main()
