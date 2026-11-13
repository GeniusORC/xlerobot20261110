#!/usr/bin/env python3
"""Safe hardware test for the pan/tilt head.

  source scripts/env.sh robot
  python scripts/test_head.py                 # read only
  python scripts/test_head.py --move          # tiny nudge, then return
  python scripts/test_head.py --move --center # also ramp back to midpoint

Requires robot_api.py to be running and the robot powered (12 V on).

Movements are tiny (5 normalized units) and the test always returns the head
to where it started.  ``--center`` ramps smoothly to the calibration midpoint
(0), which can be a large move if the head is resting far from it; it is
therefore opt-in.
"""
import argparse
import sys
import time

import requests

API = "http://127.0.0.1:8090"


def get(path):
    r = requests.get(API + path, timeout=5)
    r.raise_for_status()
    return r.json()


def post(path, payload):
    r = requests.post(API + path, json=payload, timeout=5)
    if r.status_code >= 400:
        raise RuntimeError(f"{path} -> {r.status_code}: {r.text[:200]}")
    return r.json()


def wait_still(timeout=10.0):
    """Wait until the head stops moving (center ramps over several steps)."""
    t0 = time.time()
    last = get("/head")
    while time.time() - t0 < timeout:
        time.sleep(0.25)
        now = get("/head")
        if abs(now["pan"] - last["pan"]) < 0.3 and \
                abs(now["tilt"] - last["tilt"]) < 0.3:
            return now
        last = now
    return last


def main():
    global API
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=API)
    ap.add_argument("--move", action="store_true")
    ap.add_argument("--center", action="store_true")
    ap.add_argument("--step", type=float, default=5.0)
    args = ap.parse_args()
    API = args.api.rstrip("/")

    try:
        st = get("/status")
    except Exception as e:
        print(f"robot_api not reachable: {e}")
        return 1
    if not st.get("connected"):
        print(f"robot not connected: {st.get('message')}")
        return 1

    h0 = get("/head")
    print(f"head now:  pan={h0['pan']:+.1f} tilt={h0['tilt']:+.1f}")
    print(f"reference: pan={h0['pan_ref']:+.1f} tilt={h0['tilt_ref']:+.1f}")
    print(f"tracking window: pan {h0['pan_window']}  tilt {h0['tilt_window']}")
    print(f"hard limit {h0['hard_limit']}, max step {h0['max_step']}/command")
    if not args.move:
        print("read-only; add --move to test motion")
        return 0

    ok = True
    post("/head", {"dpan": args.step})
    time.sleep(0.6)
    hp = get("/head")
    d = hp["pan"] - h0["pan"]
    print(f"pan  {h0['pan']:+.1f} -> {hp['pan']:+.1f} (delta {d:+.1f})")
    ok = ok and abs(d) > 0.5
    print("PASS: pan responds" if abs(d) > 0.5 else "FAIL: pan did not move")
    post("/head", {"dpan": -args.step})
    time.sleep(0.6)

    post("/head", {"dtilt": args.step})
    time.sleep(0.6)
    ht = get("/head")
    d = ht["tilt"] - hp["tilt"]
    print(f"tilt {hp['tilt']:+.1f} -> {ht['tilt']:+.1f} (delta {d:+.1f})")
    ok = ok and abs(d) > 0.5
    print("PASS: tilt responds" if abs(d) > 0.5 else "FAIL: tilt did not move")
    post("/head", {"dtilt": -args.step})
    time.sleep(0.6)

    if args.center:
        print("centering (ramped)...")
        post("/head", {"center": True})
        hc = wait_still()
        print(f"centred:   pan={hc['pan']:+.1f} tilt={hc['tilt']:+.1f}")
    else:
        hc = get("/head")
        print(f"returned:  pan={hc['pan']:+.1f} tilt={hc['tilt']:+.1f}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
