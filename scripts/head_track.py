#!/usr/bin/env python3
"""Point the robot's head at the user's face while it talks to them.

Closed loop on pixels, not on motor angles: the D455 gives the face box, we
convert the centre offset into a small relative ``dpan``/``dtilt`` and let
robot_api /head move the servos.  No calibration is needed, and the direction
signs are flipped automatically if a build disagrees (the error would grow).

While tracking it also mirrors the user's smile onto the laptop face, and
after a few seconds without a face it sweeps the head to look for them.

With ``--follow`` the base follows too, holding ``--standoff`` metres, and
turns only once the head has run out of travel (a cascade, so the two loops
cannot fight each other).  Distance safety lives in ``Follower`` and in
robot_api's follow latch, not in the prompt: it refuses to drive forward
closer than 0.85 m, backs off below 0.55 m, and any voice "停下" revokes
follow mode so the next control step cannot resume driving.

Run:
  .venv/bin/python scripts/head_track.py                 # track + smile mirror
  .venv/bin/python scripts/head_track.py --no-face       # head only
  .venv/bin/python scripts/head_track.py --follow        # head + base follows
  .venv/bin/python scripts/head_track.py --follow --standoff 1.2
"""
import argparse
import os
import signal
import time
from collections import deque

import numpy as np
import requests
from config_loader import service_url, CFG

import face_client as face
import vision_context as v

ROBOT_API = service_url("robot_api")

# Measured on this head: +dpan turns the camera right, -dtilt tilts it up.
PAN_SIGN = -1.0
TILT_SIGN = -1.0
KP = (34.0, 45.0)
DEAD = 0.06          # ignore offsets smaller than 6% of the image
MAX_STEP = (9.0, 6.0)
CMD_GAP = 0.18       # servos need time to move before we judge again

# The Haar cascade fires on desk clutter (a phone, a white box) often enough
# that the tilt loop once walked the head down until the camera was staring at
# the table, and stayed there.  Tracking may not tilt more than this from the
# pose the process was launched in; idle returns to it.
REST_SPAN = 25.0


class Head:
    def __init__(self):
        self.pan = 0.0
        self.tilt = 0.0
        self.rest_pan = None
        self.rest_tilt = None
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
        """Ramp to the calibration midpoint and redefine it as 0/0.

        Not used by this process any more: on this build the midpoint points
        at the table, so restoring to it is worse than leaving the head where
        it is.  ``restore()`` walks back to the launch pose instead.
        """
        return self.post({"center": True})

    def restore(self, tries=12):
        """Walk back to the launch pose.

        Absolute /head moves are clamped to MAX_STEP per request, so this
        needs a few round trips; it is deliberately slow rather than a jump.
        """
        for _ in range(tries):
            if not self.post({"pan": self.rest_pan, "tilt": self.rest_tilt},
                             timeout=3.0):
                return False
            if not self.sync():
                return False
            if (abs(self.pan - self.rest_pan) < 1.0
                    and abs(self.tilt - self.rest_tilt) < 1.0):
                return True
            time.sleep(0.1)
        return True

    def sync(self):
        """Re-read pan/tilt from the server.

        ``self.pan`` is otherwise a running sum of commanded deltas, which
        drifts from reality (the head may already be off-centre when this
        process starts, and robot_api clamps steps).  The follow cascade uses
        pan to decide when to turn the base, so it has to be the real value.
        """
        try:
            r = requests.get(f"{ROBOT_API}/head", timeout=1.0)
            r.raise_for_status()
            d = r.json()
            self.pan = float(d.get("pan", self.pan))
            self.tilt = float(d.get("tilt", self.tilt))
            return True
        except Exception:
            return False

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
        if self.rest_tilt is not None:
            lo = self.rest_tilt - REST_SPAN
            hi = self.rest_tilt + REST_SPAN
            dtilt = float(np.clip(self.tilt + dtilt, lo, hi) - self.tilt)
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


class Follower:
    """Hold the person at ``standoff`` metres using robot_api /velocity.

    Two loops that cannot fight each other:

    * **distance** sets forward speed.  It never drives forward below
      ``NO_FORWARD_M``, and backs off after ``BACKOFF_S`` spent closer than
      ``BACKOFF_M`` (a person leaning in is not a reason to reverse, but being
      walked into is).
    * **head pan** sets turning, and only after the head has used
      ``PAN_SOFT`` of its travel.  The head therefore keeps the face centred
      for small movements and the base only turns when it runs out of neck.

    All motion goes through the 0.6 s velocity lease, and only while
    robot_api's follow latch is on, so a voice "停下" is respected: the latch
    is revoked and this loop just stops asking.
    """

    MAX_FWD = 0.26          # m/s toward the person
    MAX_BACK = 0.16         # m/s away from the person
    DEADBAND = 0.15         # m
    NO_FORWARD_M = 0.85     # never drive forward closer than this
    BACKOFF_M = 0.55        # closer than this for BACKOFF_S -> reverse
    BACKOFF_S = 0.6
    JUMP_M = 0.70           # depth jump this large is a new surface
    PAN_SOFT = 42.0         # start turning the base beyond this head pan
    MAX_TURN = 12.0         # deg/s
    K_DIST = 0.35
    K_TURN = 0.55

    def __init__(self, standoff=1.0):
        self.standoff = float(standoff)
        self.on = False          # mirror of robot_api's latch
        self.sent = (0.0, 0.0)
        self.hist = deque(maxlen=5)
        self.close_since = 0.0
        self.last_error = ""

    def _post(self, path, payload, timeout=0.8):
        try:
            r = requests.post(f"{ROBOT_API}{path}", json=payload, timeout=timeout)
        except Exception as e:
            self.last_error = type(e).__name__
            return None
        if r.status_code == 200:
            self.last_error = ""
            return r.json()
        self.last_error = f"HTTP {r.status_code}"
        return None

    def enable(self, tries=20, gap=0.5):
        """Ask robot_api for follow mode; retry while it is still booting."""
        for i in range(tries):
            out = self._post("/follow", {"on": True})
            if out and out.get("follow"):
                self.on = True
                return True
            time.sleep(gap)
        print(f"head_track: follow unavailable ({self.last_error}); "
              "tracking the head only", flush=True)
        return False

    def disable(self):
        self.on = False
        self._post("/velocity", {"stop": True})
        self._post("/follow", {"on": False})
        self.sent = (0.0, 0.0)

    def latch(self):
        """Heartbeat + re-read the latch.

        A stop revokes follow and a voice command can re-enable it, so follow
        must be re-read instead of assumed.  The heartbeat is separate from the
        velocity stream so /status can tell a running follower from a dead one
        even while follow is paused.
        """
        try:
            r = requests.post(f"{ROBOT_API}/follow/heartbeat", timeout=0.8)
            r.raise_for_status()
            on = bool(r.json().get("follow"))
        except Exception:
            return
        if on != self.on:
            self.on = on
            print(f"head_track: follow {'enabled' if on else 'paused'}", flush=True)
            if not on:
                self.hist.clear()
                self.sent = (0.0, 0.0)

    def _filter(self, dist):
        """Median of the last few depths, with a jump gate.

        The cascade sometimes locks onto a different bright box for a frame,
        and one wild distance would make the base lurch.
        """
        if dist is None or not np.isfinite(dist):
            self.hist.clear()
            return None
        if self.hist and abs(dist - float(np.median(self.hist))) > self.JUMP_M:
            self.hist.clear()
            return None
        self.hist.append(float(dist))
        return float(np.median(self.hist))

    def update(self, dist, pan, seen=True):
        """One control step.  Returns (x, theta) that was requested.

        A command is sent on every step while follow is on, even a zero one:
        that keeps the 0.6 s lease alive and lets /status tell the voice side
        whether a follower is really running.
        """
        if not self.on:
            return (0.0, 0.0)
        now = time.time()
        d = self._filter(dist)
        if d is None:
            # Person lost: hold still rather than creep forward blind.
            x = 0.0
            self.close_since = 0.0
        elif d < self.BACKOFF_M:
            if not self.close_since:
                self.close_since = now
            x = -self.MAX_BACK if now - self.close_since > self.BACKOFF_S else 0.0
        else:
            self.close_since = 0.0
            err = d - self.standoff
            if abs(err) < self.DEADBAND:
                x = 0.0
            else:
                x = float(np.clip(err * self.K_DIST, -self.MAX_BACK, self.MAX_FWD))
                if x > 0 and d < self.NO_FORWARD_M:
                    x = 0.0

        # Cascade: head pan positive = head turned right, theta positive = left.
        # Only turn while the person is actually in view: when the head is
        # sweeping to find them, pan is a search position, not an error, and
        # turning the base on it would spin the robot around the room.
        theta = 0.0
        if seen:
            excess = 0.0
            if pan > self.PAN_SOFT:
                excess = pan - self.PAN_SOFT
            elif pan < -self.PAN_SOFT:
                excess = pan + self.PAN_SOFT
            theta = float(np.clip(-self.K_TURN * excess,
                                  -self.MAX_TURN, self.MAX_TURN))

        if self._post("/velocity", {"x": x, "theta": theta}) is None:
            # 409 = a stop revoked follow; latch() would also see it.
            if "409" in self.last_error:
                self.on = False
                self.sent = (0.0, 0.0)
                print("head_track: follow paused by a stop command", flush=True)
            return (0.0, 0.0)
        self.sent = (x, theta)
        return (x, theta)


def _on_sigterm(signum, frame):
    raise KeyboardInterrupt


def main():
    # pkill sends SIGTERM, which by default skips the finally block that
    # returns the head to its launch pose.
    signal.signal(signal.SIGTERM, _on_sigterm)

    ap = argparse.ArgumentParser()
    ap.add_argument("--no-face", action="store_true",
                    help="do not drive the laptop face display")
    ap.add_argument("--hz", type=float, default=8.0)
    ap.add_argument("--search-after", type=float, default=5.0,
                    help="seconds without a face before sweeping to look for one")
    ap.add_argument("--follow", action="store_true",
                    help="let the base follow the person, holding --standoff")
    ap.add_argument("--standoff", type=float, default=CFG["robot"]["follow_standoff_m"],
                    help="metres to hold from the person's face (default 1.0)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    head = Head()
    head.sync()
    head.rest_pan = head.pan          # launch pose = where idle returns to
    head.rest_tilt = head.tilt
    follower = Follower(args.standoff) if args.follow else None
    if follower is not None:
        follower.enable()
    last_seen = time.time()
    last_smile = None
    search_at = 0.0
    search_dir = 1.0
    last_sync = 0.0
    last_latch = 0.0
    period = 1.0 / max(1.0, args.hz)

    print("head_track: running (ctrl-c to stop)", flush=True)
    if follower is not None:
        print(f"head_track: following at {args.standoff:.1f} m", flush=True)
    try:
        while True:
            t0 = time.time()
            now = time.time()
            # The head estimate drifts; the follow cascade needs the real pan.
            if now - last_sync > 1.0:
                last_sync = now
                head.sync()
            # Re-read the latch: a voice stop revokes follow, and a voice
            # "跟着我" turns it back on, without restarting this process.
            if follower is not None and now - last_latch > 1.0:
                last_latch = now
                follower.latch()

            s = v.state_fast()
            dist = None
            if s and s.get("present"):
                last_seen = time.time()
                f = s["face"]
                u = f["x"] + f["w"] / 2.0
                cy = f["y"] + f["h"] / 2.0
                dist = f.get("distance_m")
                head.step(u, cy, s["width"], s["height"])
                if not args.no_face:
                    smiling = bool(s.get("smiling"))
                    if smiling != last_smile:
                        last_smile = smiling
                        face.emotion("happy" if smiling else "listening",
                                     hold=4 if smiling else 3)
                if args.verbose:
                    print(f"face at ({u:.0f},{cy:.0f}) d={dist}m "
                          f"pan={head.pan:+.1f} tilt={head.tilt:+.1f}", flush=True)
            else:
                if not args.no_face and last_smile is not None:
                    last_smile = None
                    face.emotion("neutral", hold=2)
                idle = time.time() - last_seen
                if idle > args.search_after and time.time() - search_at > 0.8:
                    search_at = time.time()
                    payload = {"dpan": 7.0 * search_dir}
                    if head.rest_tilt is not None:
                        dtilt = float(np.clip(
                            (head.rest_tilt - head.tilt) * 0.5, -6.0, 6.0))
                        payload["dtilt"] = 0.0 if abs(dtilt) < 1.0 else dtilt
                    ok = head.post(payload)
                    if ok:
                        head.pan += 7.0 * search_dir
                        head.tilt += payload.get("dtilt", 0.0)
                    if abs(head.pan) > 60:
                        search_dir *= -1.0

            if follower is not None:
                x, theta = follower.update(dist, head.pan,
                                           seen=bool(s and s.get("present")))
                if args.verbose and (x or theta):
                    print(f"follow x={x:+.2f} m/s theta={theta:+.1f} deg/s "
                          f"d={dist}", flush=True)

            time.sleep(max(0.0, period - (time.time() - t0)))
    except KeyboardInterrupt:
        print("\nhead_track: stopped", flush=True)
    finally:
        if follower is not None:
            follower.disable()
        # Return to the launch pose without calling /head {"center": true}:
        # center re-references the head wherever it is, which once declared a
        # desk-staring pose to be "forward".
        if head.rest_tilt is not None:
            head.restore()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
