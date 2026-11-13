#!/usr/bin/env python3
"""Safe hardware test for the pan/tilt head.

  source scripts/env.sh robot
  python scripts/test_head.py                 # read only
  python scripts/test_head.py --move          # small nudge, then re-center

Requires robot_api.py to be running and the robot powered (12 V on).
Movements are tiny (5 normalized units) and always end with the head centred.
"""
import argparse
import sys
import time

import requests

API = "http://127.0.0.1:8090"


def get(path):
    r = requests.get(API + path, timeout=3)
    r.raise_for_status()
    return r.json()


def post(path, payload):
    r = requests.post(API + path, json=payload, timeout=3)
    if r.status_code >= 400:
        raise RuntimeError(f"{path} -> {r.status_code}: {r.text[:200]}")
    return r.json()


def main():
    global API
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=API)
    ap.add_argument("--move", action="store_true")
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
    print(f"head before: pan={h0['pan']:+.1f} tilt={h0['tilt']:+.1f} "
          f"(limits {h0['pan_limit']} {h0['tilt_limit']})")
    if not args.move:
        print("read-only; add --move to test motion")
        return 0

    post("/head", {"center": True})
    time.sleep(0.6)
    hc = get("/head")
    print(f"centred:     pan={hc['pan']:+.1f} tilt={hc['tilt']:+.1f}")

    post("/head", {"dpan": args.step})
    time.sleep(0.5)
    hr = get("/head")
    delta = hr["pan"] - hc["pan"]
    print(f"after +{args.step:g} pan: pan={hr['pan']:+.1f} (delta {delta:+.1f})")
    ok = abs(delta) > 0.5
    print("PASS: pan responds" if ok else "FAIL: pan did not move")

    post("/head", {"dtilt": args.step})
    time.sleep(0.5)
    ht = get("/head")
    dtil = ht["tilt"] - hr["tilt"]
    print(f"after +{args.step:g} tilt: tilt={ht['tilt']:+.1f} (delta {dtil:+.1f})")
    ok = ok and abs(dtil) > 0.5
    print("PASS: tilt responds" if abs(dtil) > 0.5 else "FAIL: tilt did not move")

    post("/head", {"center": True})
    time.sleep(0.6)
    he = get("/head")
    print(f"final:       pan={he['pan']:+.1f} tilt={he['tilt']:+.1f}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
