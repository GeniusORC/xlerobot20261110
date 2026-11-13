#!/usr/bin/env python3
"""Tests for the D455 depth-assisted health station.

  source scripts/env.sh voice
  python scripts/test_health_depth.py

Checks:
  1. camera_web /snapshot/head_npz returns colour + aligned depth + intrinsics.
  2. add_depth() converts pixels to metres with the pinhole model.
  3. metric_depth_metrics() recovers a known synthetic step length.
  4. analyze() + make_report() surface the metre values without crashing.
"""
import io
import sys

import numpy as np

sys.path.insert(0, "scripts")
import health_station_web as hs

NPZ_URL = "http://127.0.0.1:8080/snapshot/head_npz"
FAILS = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def test_npz_endpoint():
    try:
        import requests
        r = requests.get(NPZ_URL, timeout=5)
        r.raise_for_status()
        z = np.load(io.BytesIO(r.content))
    except Exception as e:
        check("npz endpoint reachable", False, f"{type(e).__name__}: {e} "
              "(is camera_web.py running with the new code?)")
        return
    check("npz endpoint reachable", True, f"{len(r.content)//1024} KiB")
    color = __import__("cv2").imdecode(z["color_jpeg"], 1)
    depth = z["depth"]
    check("colour decodes", color is not None and color.shape[2] == 3, str(None if color is None else color.shape))
    check("depth shape matches colour",
          depth.shape == color.shape[:2], f"{depth.shape} vs {color.shape[:2]}")
    check("depth dtype uint16", depth.dtype == np.uint16, str(depth.dtype))
    fx, fy, cx, cy = float(z["fx"]), float(z["fy"]), float(z["cx"]), float(z["cy"])
    check("intrinsics plausible", 200 < fx < 900 and 200 < fy < 900
          and 0 < cx < 640 and 0 < cy < 480, f"fx={fx:.1f} fy={fy:.1f} cx={cx:.1f} cy={cy:.1f}")
    scale = float(z["depth_scale"])
    check("depth scale sane", 0.0005 < scale < 0.01, f"{scale}")
    valid = depth[depth > 0] * scale
    if valid.size:
        print(f"      depth stats: min={valid.min():.2f} m "
              f"median={np.median(valid):.2f} m max={valid.max():.2f} m "
              f"valid={valid.size}/{depth.size}")


def test_add_depth_geometry():
    fx, fy, cx, cy = 600.0, 600.0, 320.0, 240.0
    depth = np.full((480, 640), 2500, dtype=np.uint16)   # flat wall at 2.5 m
    row = {"px_hip": np.array([320.0, 240.0]),
           "px_ankle_l": np.array([260.0, 460.0]),
           "px_ankle_r": np.array([380.0, 460.0])}
    hs.add_depth(row, depth, (fx, fy, cx, cy), 0.001)
    check("hip distance is 2.5 m", abs(row["dist_m"] - 2.5) < 0.02, f"{row['dist_m']:.3f}")
    xl = (260.0 - cx) * 2.5 / fx
    xr = (380.0 - cx) * 2.5 / fx
    check("ankle X from pinhole model",
          abs(row["foot_l_x"] - xl) < 1e-6 and abs(row["foot_r_x"] - xr) < 1e-6,
          f"{row['foot_l_x']:.3f} / {row['foot_r_x']:.3f}")
    check("foot forward is 0 on a flat wall",
          abs(row["foot_l_z"] - row["dist_m"]) < 0.02)


def test_step_length_recovery():
    fps, n = 10.0, 120
    t = np.arange(n) / fps
    body = 3.0 - 0.01 * t                      # walks towards the camera
    swing = 0.30 * np.sin(2 * np.pi * 0.9 * t)  # 0.30 m amplitude
    series = []
    for i in range(n):
        series.append({
            "dist_m": body[i],
            "hip_x_m": 0.02 * np.sin(2 * np.pi * 0.45 * t[i]),
            "foot_l_z": body[i] - swing[i],
            "foot_r_z": body[i] + swing[i],
            "foot_l_x": 0.0, "foot_r_x": 0.0,
        })
    m = hs.metric_depth_metrics(series)
    true_p2p = float(np.percentile(swing, 95) - np.percentile(swing, 5))
    got = m.get("step_length_m")
    check("depth_used", m.get("depth_used") is True, str(m.get("walk_axis")))
    check("step length recovers synthetic swing",
          got is not None and abs(got - true_p2p) < 0.03,
          f"got={got} true={true_p2p:.2f}")
    check("distance is median body distance",
          m.get("distance_m") is not None and abs(m["distance_m"] - np.median(body)) < 0.05,
          f"{m.get('distance_m')} vs {np.median(body):.2f}")


def test_lateral_walk_axis():
    """Walking across the frame must use the lateral axis, not depth."""
    n = 80
    series = []
    for i in range(n):
        z = 2.5
        x = -0.5 + 1.0 * i / n
        swing = 0.25 * np.sin(2 * np.pi * i / 20.0)
        series.append({
            "dist_m": z, "hip_x_m": x,
            "foot_l_z": z, "foot_r_z": z,
            "foot_l_x": x - swing, "foot_r_x": x + swing,
        })
    m = hs.metric_depth_metrics(series)
    check("lateral walk selects x axis", m.get("walk_axis") == "x", str(m.get("walk_axis")))
    check("lateral step length recovered", m.get("step_length_m") is not None
          and 0.3 < m["step_length_m"] < 0.6, str(m.get("step_length_m")))


def test_no_depth_fallback():
    series = [{"knee_l": 170.0, "knee_r": 171.0, "hip_l": 172.0, "hip_r": 173.0,
               "trunk": -2.0, "ankle_ly": 0.9, "ankle_ry": 0.91,
               "ankle_lx": 0.1, "ankle_rx": 0.12, "hip_x": 0.05} for _ in range(20)]
    m = hs.analyze(series, 2.0, 10.0)
    check("no-depth still produces metrics", m is not None and m["depth_used"] is False)
    check("no-depth step length is None", m.get("step_length_m") is None)
    report, tips, risk = hs.make_report(m)
    check("report works without depth", "步长约" not in report and risk in ("低", "中", "高", "数据不足"))


def test_report_with_depth():
    m = {
        "steps": 10, "step_count": 10, "duration_s": 9.0, "cadence_spm": 66.0,
        "knee_symmetry_pct": 4.0, "knee_left_range_deg": 30.0,
        "knee_right_range_deg": 31.0, "trunk_mean_deg": -3.0,
        "trunk_std_deg": 2.0, "sway_norm": 0.05, "step_length_norm": 0.4,
        "depth_used": True, "distance_m": 2.4, "step_length_m": 0.28,
    }
    report, tips, risk = hs.make_report(m)
    check("report mentions metre step length", "0.28 米" in report, report.splitlines()[0])
    check("small step flags a finding", "步幅偏小" in report)
    check("tips non-empty", len(tips) >= 2)


def main():
    test_npz_endpoint()
    test_add_depth_geometry()
    test_step_length_recovery()
    test_lateral_walk_axis()
    test_no_depth_fallback()
    test_report_with_depth()
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {FAILS}")
        return 1
    print("all depth health tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
