#!/usr/bin/env python3
"""Point the robot's head at the user's face while it talks to them.

Closed loop on pixels, not on motor angles: the D455 gives the face box, we
convert the centre offset into a small relative ``dpan``/``dtilt`` and let
robot_api /head move the servos.  No calibration is needed, and the direction
signs are flipped automatically if a build disagrees (the error would grow).

While tracking it also mirrors the user's smile onto the laptop face, and
after a few seconds without a face it sweeps the head to look for them.

Run:
  .venv/bin/python scripts/head_track.py            # track + smile mirror
  .venv/bin/python scripts/head_track.py --no-face  # head only
"""
import argparse
import os
import time

import numpy as np
import requests

import face_client as face
import vision_context as v

ROBOT_API = os.environ.get("ROBOT_API", "http://127.0.0.1:8090").rstrip("/")

# Measured on this head: +dpan turns the camera right, -dtilt tilts it up.
PAN_SIGN = -1.0
TILT_SIGN = -1.0
KP = (34.0, 45.0)
DEAD = 0.06          # ignore offsets smaller than 6% of the image
MAX_STEP = (9.0, 6.0)
CMD_GAP = 0.18       # servos need time to move before we judge again


class Head:
    def __init__(self):
        self.pan = 0.0
        self.tilt = 0.0
        self.fails = 0
        self.last_cmd = 0.0
        self.prev = None
        self.worse = [0, 0]

    def post(self, payload, timeout=1.2):
        try:
            r = requests.post(f"{ROBOT_API}/head", json=payload, timeout=timeout)
            if r.status_code == 503:
                self.fails += 1
                return False
            r.raise_for_status()
            self.fails = 0
            return True
        except Exception:
            self.fails += 1
            return False

    def center(self):
        return self.post({"center": True})

    def step(self, u, v, w, h, adapt=True):
        ex = (u - w / 2.0) / float(w)
        ey = (v - 0.45 * h) / float(h)
        if adapt and self.prev is not None:
            for axis, err, prev in ((0, ex, self.prev[0]), (1, ey, self.prev[1])):
                if abs(err) > abs(prev) + 0.02:
                    self.worse[axis] += 1
                    if self.worse[axis] >= 3:
                        global PAN_SIGN, TILT_SIGN
                        if axis == 0:
                            PAN_SIGN *= -1.0
                        else:
                            TILT_SIGN *= -1.0
                        self.worse = [0, 0]
                        self.prev = None
                        print(f"head_track: flipped {'pan' if axis == 0 else 'tilt'} sign",
                              flush=True)
                        return True
                else:
                    self.worse[axis] = 0
        self.prev = (ex, ey)

        dpan = 0.0 if abs(ex) < DEAD else -PAN_SIGN * KP[0] * ex
        dtilt = 0.0 if abs(ey) < DEAD else -TILT_SIGN * KP[1] * ey
        dpan = float(np.clip(dpan, -MAX_STEP[0], MAX_STEP[0]))
        dtilt = float(np.clip(dtilt, -MAX_STEP[1], MAX_STEP[1]))
        if dpan == 0.0 and dtilt == 0.0:
            return True
        if time.monotonic() - self.last_cmd < CMD_GAP:
            return True
        ok = self.post({"dpan": dpan, "dtilt": dtilt})
        if ok:
            self.last_cmd = time.monotonic()
            self.pan = float(np.clip(self.pan + dpan, -98, 98))
            self.tilt = float(np.clip(self.tilt + dtilt, -98, 98))
        return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-face", action="store_true",
                    help="do not drive the laptop face display")
    ap.add_argument("--hz", type=float, default=8.0)
    ap.add_argument("--search-after", type=float, default=5.0,
                    help="seconds without a face before sweeping to look for one")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    head = Head()
    last_seen = time.time()
    last_smile = None
    search_at = 0.0
    search_dir = 1.0
    period = 1.0 / max(1.0, args.hz)

    print("head_track: running (ctrl-c to stop)", flush=True)
    try:
        while True:
            t0 = time.time()
            s = v.state_fast()
            if s and s.get("present"):
                last_seen = time.time()
                f = s["face"]
                u = f["x"] + f["w"] / 2.0
                cy = f["y"] + f["h"] / 2.0
                head.step(u, cy, s["width"], s["height"])
                if not args.no_face:
                    smiling = bool(s.get("smiling"))
                    if smiling != last_smile:
                        last_smile = smiling
                        face.emotion("happy" if smiling else "listening",
                                     hold=4 if smiling else 3)
                if args.verbose:
                    print(f"face at ({u:.0f},{cy:.0f}) d={f['distance_m']}m "
                          f"pan={head.pan:+.1f} tilt={head.tilt:+.1f}", flush=True)
            else:
                if not args.no_face and last_smile is not None:
                    last_smile = None
                    face.emotion("neutral", hold=2)
                idle = time.time() - last_seen
                if idle > args.search_after and time.time() - search_at > 0.8:
                    search_at = time.time()
                    ok = head.post({"dpan": 7.0 * search_dir})
                    if ok:
                        head.pan += 7.0 * search_dir
                    if abs(head.pan) > 60:
                        search_dir *= -1.0
            time.sleep(max(0.0, period - (time.time() - t0)))
    except KeyboardInterrupt:
        print("\nhead_track: stopped", flush=True)
        head.center()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
