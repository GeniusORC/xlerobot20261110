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
app = Flask(__name__)


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
    def __init__(self, root, w, h, fps=33, debug=False):
        import tkinter as tk
        self.tk = tk
        self.root = root
        self.debug = debug
        self.cv = tk.Canvas(root, width=w, height=h, bg=BG,
                            highlightthickness=0, bd=0)
        self.cv.pack(fill="both", expand=True)
        self.w, self.h = w, h
        self.cv.bind("<Configure>", self._resize)
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

        self.root.bind("<Escape>", lambda e: self.root.destroy())
        self.root.after(200, self.tick)

    def _resize(self, ev):
        self.w, self.h = max(320, ev.width), max(240, ev.height)

    def tick(self):
        now = time.time()
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
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8086)
    ap.add_argument("--monitor", type=int, default=1,
                    help="1-based monitor index from xrandr")
    ap.add_argument("--windowed", action="store_true")
    ap.add_argument("--debug", action="store_true",
                    help="show the emotion name on screen")
    ap.add_argument("--list-monitors", action="store_true")
    args = ap.parse_args()

    if args.list_monitors:
        for i, m in enumerate(list_monitors(), 1):
            print(i, m)
        return 0

    mons = list_monitors()
    geo = None
    if mons and not args.windowed:
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
    FaceWindow(root, w, h, debug=args.debug)
    print(f"face on {geo or 'window'} | http://127.0.0.1:{args.port}", flush=True)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
