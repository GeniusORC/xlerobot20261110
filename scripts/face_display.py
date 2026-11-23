#!/usr/bin/env python3
"""Full-screen parametric face for the companion robot.

The laptop is bolted to the cart and faces the user, so it doubles as the
robot's face: no extra screen, no HDMI cable.  This draws a soft, low-glare
face with eyes, brows and a mouth, animates breathing and blinking, and
lip-syncs the mouth to the TTS wav the voice agent just played.

Control (HTTP, default port 8086)
  GET  /state                      current emotion and look direction
  POST /emotion {"emotion":"happy","intensity":0.8,"hold":3}
  POST /say     {"audio":"reply_xxx.wav"}   lip-sync that file
  POST /say     {"duration":2.5}            open the mouth for N seconds
  POST /look    {"dx":0.2,"dy":-0.1}        move the pupils, -1..1
  POST /blink
  GET  /window                     "full" | "windowed" | "hidden"
  POST /window  {"state":"windowed"}        same actions as the buttons

The window has no title bar, so it draws its own three controls in the
top-right corner, like a title bar: minimize (-), maximize/restore (square) and
close (x).  Minimize leaves a small face in the corner of the monitor; tap it
to bring the face back.  Esc also closes.  A deliberate close exits with code
0 and the supervisor leaves the face closed instead of relaunching it.

Run:
  DISPLAY=:1 XAUTHORITY=/run/user/1000/gdm/Xauthority \
    .venv/bin/python scripts/face_display.py --monitor 1

Emotions: neutral happy surprised sad concerned listening thinking sleepy
"""
import argparse
import math
import os
import random
import re
import subprocess
import threading
import time
import wave

from flask import Flask, jsonify, request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDIO_DIR = os.path.join(ROOT, "data", "audio")

BG = "#07090d"
FACE = "#9fe4ff"
FACE_EDGE = "#5fb6d8"
FACE_DIM = "#356d84"
MOUTH = "#ff9fb2"
PUPIL = "#08131a"
BLUSH = "#7a2d3d"

EMOTIONS = {
    #            eye   brow   curve  open  pupil
    "neutral":   (1.00,  0.00,  0.15,  0.00, 1.00),
    "happy":     (0.74,  0.03,  0.95,  0.18, 1.00),
    "surprised": (1.28,  0.05,  0.00,  0.60, 0.72),
    "sad":       (0.80, -0.04, -0.85,  0.00, 1.00),
    "concerned": (0.96, -0.02, -0.45,  0.00, 1.00),
    "listening": (1.10,  0.02,  0.10,  0.05, 0.90),
    "thinking":  (1.00,  0.01,  0.00,  0.00, 0.82),
    "sleepy":    (0.32, -0.01, -0.10,  0.00, 1.00),
    "speaking":  (1.00,  0.01,  0.30,  0.30, 1.00),
}


def list_monitors():
    try:
        out = subprocess.run(["xrandr"], capture_output=True, text=True,
                             timeout=5).stdout
    except Exception:
        return []
    found = []
    for m in re.finditer(
            r"^(\S+) connected (?:primary )?(\d+)x(\d+)\+(\d+)\+(\d+)",
            out, re.M):
        name = m.group(1)
        w, h, x, y = map(int, m.groups()[1:])
        found.append({"name": name, "w": w, "h": h, "x": x, "y": y})
    return found


class FaceState:
    """Thread-safe state shared by the HTTP thread and the Tk loop."""

    def __init__(self):
        self.lock = threading.Lock()
        self.emotion = "neutral"
        self.intensity = 1.0
        self.until = 0.0
        self.look = [0.0, 0.0]
        self.speaking_until = 0.0
        self.speaking_emotion = None
        self.envelope = []
        self.env_t0 = 0.0
        self.blink_at = 0.0

    def set_emotion(self, name, intensity=1.0, hold=0.0):
        if name not in EMOTIONS:
            return False
        with self.lock:
            self.emotion = name
            self.intensity = max(0.0, min(1.0, float(intensity)))
            self.until = time.time() + float(hold) if hold else 0.0
        return True

    def snapshot(self):
        with self.lock:
            now = time.time()
            emotion = self.emotion
            if self.until and now > self.until:
                emotion = "neutral"
                self.emotion = "neutral"
                self.until = 0.0
            level = 0.0
            if now < self.speaking_until:
                if self.envelope:
                    idx = int((now - self.env_t0) * 50.0)
                    if 0 <= idx < len(self.envelope):
                        level = self.envelope[idx]
                else:
                    level = 0.35 + 0.3 * math.sin(now * 12.0)
                emotion = "speaking"
            elif self.speaking_emotion and now < self.speaking_until + 4.0:
                emotion = self.speaking_emotion
            return {
                "emotion": emotion,
                "intensity": self.intensity,
                "look": list(self.look),
                "mouth": level,
                "speaking": now < self.speaking_until,
                "blink": self.blink_at,
            }


STATE = FaceState()
WINDOW = {"view": None}
app = Flask(__name__)


@app.get("/window")
def api_window():
    view = WINDOW["view"]
    if view is None:
        return jsonify({"state": "starting"}), 503
    return jsonify({"state": view.window_state,
                    "monitor": view.monitor,
                    "controls": view.controls_enabled})


@app.post("/window")
def api_set_window():
    data = request.get_json(force=True, silent=True) or {}
    state = str(data.get("state", "")).lower()
    if state not in ("full", "windowed", "hidden", "toggle"):
        return jsonify({"error": "state must be full, windowed, hidden or toggle"}), 400
    view = WINDOW["view"]
    if view is None:
        return jsonify({"error": "window not ready"}), 503
    view.request(state)
    return jsonify({"ok": True, "requested": state})


@app.get("/state")
def api_state():
    return jsonify(STATE.snapshot())


@app.post("/emotion")
def api_emotion():
    data = request.get_json(force=True, silent=True) or {}
    name = str(data.get("emotion", "neutral")).lower()
    ok = STATE.set_emotion(name, data.get("intensity", 1.0), data.get("hold", 0))
    if not ok:
        return jsonify({"error": "unknown emotion",
                        "known": sorted(EMOTIONS)}), 400
    return jsonify(STATE.snapshot())


@app.post("/look")
def api_look():
    data = request.get_json(force=True, silent=True) or {}
    with STATE.lock:
        STATE.look = [max(-1.0, min(1.0, float(data.get("dx", 0.0)))),
                      max(-1.0, min(1.0, float(data.get("dy", 0.0))))]
    return jsonify(STATE.snapshot())


@app.post("/blink")
def api_blink():
    with STATE.lock:
        STATE.blink_at = time.time()
    return jsonify({"ok": True})


@app.post("/say")
def api_say():
    data = request.get_json(force=True, silent=True) or {}
    audio = data.get("audio")
    duration = float(data.get("duration", 0.0) or 0.0)
    after = data.get("emotion")
    if after not in EMOTIONS:
        after = None
    env = []
    if audio:
        path = audio if os.path.isabs(audio) else os.path.join(AUDIO_DIR, audio)
        env = load_envelope(path)
        if env:
            duration = len(env) / 50.0
    if duration <= 0:
        duration = 2.0
    with STATE.lock:
        STATE.envelope = env
        STATE.env_t0 = time.time()
        STATE.speaking_until = time.time() + duration
        STATE.speaking_emotion = after
        STATE.emotion = "speaking"
        STATE.intensity = 1.0
        STATE.until = time.time() + duration
    return jsonify({"ok": True, "duration": duration, "frames": len(env)})


def load_envelope(path, rate=50.0):
    """RMS envelope of a wav file at ``rate`` frames per second, 0..1."""
    try:
        with wave.open(path, "rb") as w:
            nch, width, sr = w.getnchannels(), w.getsampwidth(), w.getframerate()
            raw = w.readframes(w.getnframes())
    except Exception:
        return []
    if width != 2:
        return []
    step = max(2, int(sr / rate))
    frames = []
    for i in range(0, max(0, len(raw) - 4 * step), 2 * step):
        chunk = raw[i:i + 2 * step]
        s = 0
        n = 0
        for j in range(0, len(chunk) - 1, 2 * nch):
            v = int.from_bytes(chunk[j:j + 2], "little", signed=True)
            s += v * v
            n += 1
        frames.append(math.sqrt(s / n) if n else 0.0)
    peak = max(frames) if frames else 0.0
    if peak <= 0:
        return []
    return [min(1.0, (f / peak) ** 0.6) for f in frames]


class FaceWindow:
    # Touch-friendly controls: 44 px circles, plus slop so they are easy to hit
    # on the laptop touchscreen.  Laid out like a window title bar: minimize,
    # maximize/restore, close, anchored to the top-right corner.
    R = 22
    SLOP = 8
    GAP = 56
    ORDER = ("minimize", "maximize", "close")

    def __init__(self, root, w, h, fps=33, debug=False, controls=True,
                 monitor=None, state="full"):
        import tkinter as tk
        self.tk = tk
        self.root = root
        self.debug = debug
        self.cv = tk.Canvas(root, width=w, height=h, bg=BG,
                            highlightthickness=0, bd=0)
        self.cv.pack(fill="both", expand=True)
        self.w, self.h = w, h
        self.monitor = monitor or {"name": "screen", "w": w, "h": h,
                                   "x": 0, "y": 0}
        self.window_state = state
        self._hidden_from = "full"
        self._pending = None
        self._pending_lock = threading.Lock()
        self.chip = None
        self.next_blink = time.time() + random.uniform(2.0, 5.0)
        self.blink_start = 0.0

        self.eyes = [self.cv.create_oval(0, 0, 0, 0, fill=FACE,
                                         outline=FACE_EDGE, width=1)
                     for _ in range(2)]
        self.pupils = [self.cv.create_oval(0, 0, 0, 0, fill=PUPIL, outline="")
                       for _ in range(2)]
        self.high = [self.cv.create_oval(0, 0, 0, 0, fill="#ffffff", outline="")
                     for _ in range(2)]
        self.brows = [self.cv.create_line(0, 0, 0, 0, fill=FACE_DIM,
                                          width=3, capstyle="round")
                      for _ in range(2)]
        self.mouth = self.cv.create_polygon(
            0, 0, 0, 0, 0, 0, 0, 0, fill=MOUTH, outline="", smooth=True)
        self.blush = [self.cv.create_oval(0, 0, 0, 0, fill=BLUSH, outline="",
                                          state="hidden") for _ in range(2)]
        self.hint = self.cv.create_text(
            w / 2, h * 0.94, text="", fill=FACE_DIM,
            font=("DejaVu Sans", 12), state="normal" if debug else "hidden")

        # The window is overrideredirect (no title bar), so it draws its own
        # window controls.  They are created last so they sit above the face.
        self.controls_enabled = controls
        self._build_controls()

        self.cv.bind("<Button-1>", self._on_click)
        self.cv.bind("<Motion>", self._on_motion)
        self.cv.bind("<Configure>", self._resize)
        self.root.bind("<Escape>", lambda e: self.close("Escape key"))
        self.root.after(200, self.tick)

    def _build_controls(self):
        tk = self.tk
        shown = "normal" if self.controls_enabled else "hidden"
        self.buttons = {}
        for name in self.ORDER:
            ring = self.cv.create_oval(0, 0, 0, 0, outline=FACE_DIM, width=2,
                                       state=shown, tags=("control",))
            self.buttons[name] = {"ring": ring, "cx": 0, "cy": 0, "glyphs": [],
                                  "alt": []}
        min_glyph = self.cv.create_line(0, 0, 0, 0, fill=FACE_DIM, width=2,
                                        capstyle="round", state=shown,
                                        tags=("control",))
        self.buttons["minimize"]["glyphs"] = [min_glyph]
        close_glyph = self.cv.create_text(0, 0, text="✕", fill=FACE_DIM,
                                          font=("DejaVu Sans", 16),
                                          state=shown, tags=("control",))
        self.buttons["close"]["glyphs"] = [close_glyph]
        # Full screen: one square.  Restored: two overlapping squares.
        square = self.cv.create_rectangle(0, 0, 0, 0, outline=FACE_DIM,
                                          width=2, state=shown,
                                          tags=("control",))
        self.buttons["maximize"]["glyphs"] = [square]
        self.buttons["maximize"]["alt"] = [
            self.cv.create_rectangle(0, 0, 0, 0, outline=FACE_DIM, width=2,
                                     state="hidden", tags=("control",))
            for _ in range(2)]
        self._layout_controls()

    def _layout_controls(self):
        shown = "normal" if self.controls_enabled else "hidden"
        cy = 34
        ordered = ("close", "maximize", "minimize")
        for i, name in enumerate(ordered):
            cx = self.w - 34 - i * self.GAP
            b = self.buttons[name]
            b["cx"], b["cy"] = cx, cy
            r = self.R
            self.cv.coords(b["ring"], cx - r, cy - r, cx + r, cy + r)
            if name == "minimize":
                for item in b["glyphs"]:
                    self.cv.coords(item, cx - 8, cy, cx + 8, cy)
            elif name == "close":
                for item in b["glyphs"]:
                    self.cv.coords(item, cx, cy)
            else:
                for item in b["glyphs"]:
                    self.cv.coords(item, cx - 7, cy - 7, cx + 7, cy + 7)
                back, front = b["alt"]
                self.cv.coords(back, cx - 9, cy - 9, cx + 1, cy + 1)
                self.cv.coords(front, cx - 1, cy - 1, cx + 9, cy + 9)
        # Only the glyph matching the current window state is visible.
        maximized = self.window_state == "full"
        self.cv.itemconfig(self.buttons["maximize"]["glyphs"][0],
                           state=shown if maximized else "hidden")
        for item in self.buttons["maximize"]["alt"]:
            self.cv.itemconfig(item, state=shown if not maximized else "hidden")

    def _hit(self, ev):
        if not self.controls_enabled:
            return None
        reach = self.R + self.SLOP
        for name, b in self.buttons.items():
            dx, dy = ev.x - b["cx"], ev.y - b["cy"]
            if dx * dx + dy * dy <= reach * reach:
                return name
        return None

    def _paint(self, name, hot):
        b = self.buttons[name]
        colour = FACE if hot else FACE_DIM
        self.cv.itemconfig(b["ring"], outline=colour)
        for item in b["glyphs"] + b["alt"]:
            if self.cv.type(item) == "line":
                self.cv.itemconfig(item, fill=colour)
            elif self.cv.type(item) == "text":
                self.cv.itemconfig(item, fill=colour)
            else:
                self.cv.itemconfig(item, outline=colour)

    def _on_motion(self, ev):
        if not self.controls_enabled:
            return
        hot = self._hit(ev)
        for name in self.buttons:
            self._paint(name, name == hot)

    def _on_click(self, ev):
        name = self._hit(ev)
        if name == "close":
            self.close(f"close button at {ev.x},{ev.y}")
        elif name == "minimize":
            self.minimize(f"minimize button at {ev.x},{ev.y}")
        elif name == "maximize":
            self.toggle_maximize()

    def request(self, action):
        """Queue a window action from the HTTP thread, applied in tick()."""
        with self._pending_lock:
            self._pending = action

    def _take_request(self):
        with self._pending_lock:
            pending, self._pending = self._pending, None
        return pending

    def apply_window(self, state):
        if state == "hidden":
            self.minimize("api")
            return
        if state not in ("full", "windowed"):
            return
        self._hide_chip()
        w, h, x, y = self._geometry(state)
        if self.root.state() == "withdrawn":
            self.root.deiconify()
        # Keep it frameless and unmanaged; only the drawn controls act on it.
        self.root.overrideredirect(True)
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.window_state = state
        self._layout_controls()

    def toggle_maximize(self):
        if self.window_state == "hidden":
            self.apply_window(self._hidden_from)
        elif self.window_state == "full":
            self.apply_window("windowed")
        else:
            self.apply_window("full")

    def _geometry(self, state):
        m = self.monitor
        if state == "windowed":
            w = max(640, int(m["w"] * 0.6))
            h = max(480, int(m["h"] * 0.6))
            return (w, h, m["x"] + (m["w"] - w) // 2,
                    m["y"] + (m["h"] - h) // 2)
        return m["w"], m["h"], m["x"], m["y"]

    def minimize(self, reason="api"):
        if self.window_state == "hidden":
            return
        print(f"face display minimized ({reason})", flush=True)
        self._hidden_from = self.window_state
        self.window_state = "hidden"
        self.root.withdraw()
        self._show_chip()

    def _show_chip(self):
        """When minimized, leave a small face in the corner to tap and restore."""
        tk = self.tk
        if self.chip is not None:
            try:
                if self.chip.winfo_exists():
                    self.chip.deiconify()
                    return
            except Exception:
                pass
        m = self.monitor
        cw, ch = 190, 64
        top = tk.Toplevel(self.root)
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        top.geometry(f"{cw}x{ch}+{m['x'] + m['w'] - cw - 24}+{m['y'] + 24}")
        cv = tk.Canvas(top, width=cw, height=ch, bg=BG, bd=0,
                       highlightthickness=1, highlightbackground=FACE_DIM)
        cv.pack(fill="both", expand=True)
        cy = ch / 2 - 6
        for dx in (-11, 11):
            cv.create_oval(cw / 2 + dx - 6, cy - 7, cw / 2 + dx + 6, cy + 7,
                           fill=FACE, outline=FACE_EDGE, width=1)
        cv.create_arc(cw / 2 - 12, cy + 2, cw / 2 + 12, cy + 20,
                      start=200, extent=140, style="arc", outline=MOUTH, width=2)
        cv.create_text(cw / 2, ch - 12, text="restore", fill=FACE_DIM,
                       font=("DejaVu Sans", 9))
        for widget in (top, cv):
            widget.bind("<Button-1>",
                        lambda e: self.apply_window(self._hidden_from))
        self.chip = top

    def _hide_chip(self):
        if self.chip is not None:
            try:
                self.chip.destroy()
            except Exception:
                pass
            self.chip = None

    def close(self, reason="api"):
        """Close deliberately: exit 0 so the supervisor leaves the face closed."""
        print(f"face display closing ({reason})", flush=True)
        self._hide_chip()
        try:
            self.root.destroy()
        except Exception:
            pass

    def _resize(self, ev):
        self.w, self.h = max(320, ev.width), max(240, ev.height)
        if hasattr(self, "buttons"):
            self._layout_controls()

    def tick(self):
        now = time.time()
        pending = self._take_request()
        if pending == "toggle":
            self.toggle_maximize()
        elif pending:
            self.apply_window(pending)
        st = STATE.snapshot()
        emo = EMOTIONS.get(st["emotion"], EMOTIONS["neutral"])
        k = st["intensity"]
        eye_open, brow, curve, m_open, pupil_scale = emo
        eye_open = 1.0 + (eye_open - 1.0) * k
        curve = curve * k
        m_open = m_open * k

        if now >= self.next_blink and now - self.blink_start > 0.35:
            self.blink_start = now
            self.next_blink = now + random.uniform(2.5, 6.5)
        b = 0.0
        if self.blink_start:
            dt = now - self.blink_start
            if dt < 0.10:
                b = dt / 0.10
            elif dt < 0.22:
                b = 1.0 - (dt - 0.10) / 0.12
        eye_open *= max(0.04, 1.0 - b)

        w, h = self.w, self.h
        s = min(w, h)
        breath = math.sin(now * 0.9) * h * 0.004
        cy = h * 0.48 + breath

        eye_w = s * 0.105
        eye_h = s * 0.125
        eye_dx = s * 0.175
        pupil_r = s * 0.038
        mouth_w = s * 0.170
        look = st["look"]

        for i, side in enumerate((-1, 1)):
            cx = w / 2 + side * eye_dx
            eh = max(2.0, eye_h * eye_open)
            self.cv.coords(self.eyes[i], cx - eye_w / 2, cy - eh / 2,
                           cx + eye_w / 2, cy + eh / 2)
            pr = pupil_r * pupil_scale
            px = cx + look[0] * eye_w * 0.26
            py = cy + look[1] * eye_h * 0.24
            self.cv.coords(self.pupils[i], px - pr, py - pr, px + pr, py + pr)
            hr = pr * 0.42
            self.cv.coords(self.high[i],
                           px - pr * 0.36 - hr, py - pr * 0.36 - hr,
                           px - pr * 0.36 + hr, py - pr * 0.36 + hr)
            bw = eye_w * 0.58
            by = cy - eye_h * (0.95 + brow * 3.0)
            tilt = (-0.30 * curve - brow * 3.0) * eye_h * 0.45
            self.cv.coords(self.brows[i],
                           cx - bw, by + tilt, cx + bw, by - tilt)
            self.cv.itemconfig(self.brows[i],
                               width=max(3, int(s * 0.010)))

        mw = mouth_w
        my = cy + h * 0.175
        mouth = st["mouth"]
        open_amt = max(m_open, mouth)
        top, bot = [], []
        n = 18
        for j in range(n + 1):
            x = -1.0 + 2.0 * j / n
            px = w / 2 + x * mw
            line = my - curve * h * 0.030 * (x * x)
            thick = max(s * 0.0045, open_amt * h * 0.055 * (1 - x * x))
            top.append((px, line - thick))
            bot.append((px, line + thick))
        pts = [c for p in top for c in p] + [c for p in reversed(bot) for c in p]
        self.cv.coords(self.mouth, *pts)

        show_blush = st["emotion"] == "happy"
        for i, side in enumerate((-1, 1)):
            if show_blush:
                bx = w / 2 + side * s * 0.30
                by = cy + s * 0.075
                r = s * 0.045
                self.cv.coords(self.blush[i], bx - r, by - r * 0.55,
                               bx + r, by + r * 0.55)
                self.cv.itemconfig(self.blush[i], state="normal")
            else:
                self.cv.itemconfig(self.blush[i], state="hidden")

        if self.debug:
            self.cv.itemconfig(self.hint, text=st["emotion"])
        self.root.after(33, self.tick)


def main():
    try:
        from config_loader import CFG
    except Exception:          # standalone use outside the project tree
        CFG = {}
    face_cfg = (CFG.get("face") or {}) if isinstance(CFG, dict) else {}

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8086)
    ap.add_argument("--monitor", type=int, default=int(face_cfg.get("monitor", 1)),
                    help="1-based monitor index from xrandr (1 = laptop, 2 = external)")
    ap.add_argument("--windowed", action="store_true")
    ap.add_argument("--debug", action="store_true",
                    help="show the emotion name on screen")
    ap.add_argument("--no-controls", "--no-close-button", dest="no_controls",
                    action="store_true",
                    help="hide the on-screen window controls (Esc still closes)")
    ap.add_argument("--list-monitors", action="store_true")
    args = ap.parse_args()

    if args.list_monitors:
        for i, m in enumerate(list_monitors(), 1):
            print(i, m)
        return 0

    mons = list_monitors()
    geo = None
    if mons and not args.windowed:
        if args.monitor > len(mons):
            print(f"monitor {args.monitor} requested but only {len(mons)} "
                  f"connected; using {len(mons)}", flush=True)
        idx = max(1, min(len(mons), args.monitor)) - 1
        geo = mons[idx]

    def serve():
        app.run(host="127.0.0.1", port=args.port, threaded=True,
                debug=False, use_reloader=False)

    threading.Thread(target=serve, daemon=True).start()

    import tkinter as tk
    root = tk.Tk()
    root.configure(bg=BG)
    root.title("xiaole face")
    if geo:
        root.overrideredirect(True)
        root.geometry(f"{geo['w']}x{geo['h']}+{geo['x']}+{geo['y']}")
        w, h = geo["w"], geo["h"]
    else:
        root.geometry("1200x800")
        w, h = 1200, 800
    view = FaceWindow(root, w, h, debug=args.debug,
                      controls=bool(face_cfg.get("controls", True))
                      and not args.no_controls,
                      monitor=geo, state="full" if geo else "windowed")
    WINDOW["view"] = view
    print(f"face on {geo or 'window'} | http://127.0.0.1:{args.port}", flush=True)
    root.mainloop()
    WINDOW["view"] = None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
