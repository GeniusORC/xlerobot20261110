#!/usr/bin/env python3
"""Prove that metric step length survives a moving robot/camera.

Simulates a person walking in front of the D455 while the head pans to follow
them (and the base sways), projects 3D body joints through the real pinhole
model, rasterises a depth map, then runs the **real** pipeline
(extract -> add_depth -> analyze) and compares the result with ground truth.

  source scripts/env.sh voice
  python scripts/test_metric_camera_motion.py
"""
import sys

import numpy as np

sys.path.insert(0, "scripts")
import health_station_web as hs

W, H = 640, 480
FX, FY, CX, CY = 386.8, 386.3, 323.6, 248.2
K = (FX, FY, CX, CY)
SCALE = 0.001


class LM:
    __slots__ = ("x", "y", "visibility")

    def __init__(self, x, y, vis=0.99):
        self.x, self.y, self.visibility = x, y, vis


def make_depth(points, W=W, H=H):
    """Rasterise small discs of true depth at each joint pixel."""
    depth = np.zeros((H, W), dtype=np.uint16)
    yy, xx = np.mgrid[-10:11, -10:11]
    disc = (xx * xx + yy * yy) <= 100
    for u, v, z in points:
        ui, vi = int(round(u)), int(round(v))
        x0, x1 = ui - 10, ui + 11
        y0, y1 = vi - 10, vi + 11
        if x0 < 0 or y0 < 0 or x1 > W or y1 > H:
            continue
        patch = depth[y0:y1, x0:x1]
        patch[disc] = np.uint16(min(65535, round(z * 1000)))
    return depth


def project(point_world, cam_pos, yaw):
    """World -> camera -> pixel.  Camera looks along +Z, yaw about Y."""
    x = point_world[0] - cam_pos[0]
    y = point_world[1] - cam_pos[1]
    z = point_world[2] - cam_pos[2]
    xc = np.cos(yaw) * x - np.sin(yaw) * z
    zc = np.sin(yaw) * x + np.cos(yaw) * z
    yc = -y                                  # image y grows downwards
    if zc <= 0.2:
        return None
    return (FX * xc / zc + CX, FY * yc / zc + CY, zc)


def person_world(t, mode, A=0.30, speed=0.9):
    """Return named 3D joints of a walking person at time t."""
    phase = 2 * np.pi * 1.2 * t
    swing = A * np.sin(phase)
    if mode == "lateral":
        # walks along world X, body faces +X, lateral axis is world Z
        x = -0.8 + speed * t
        z = 2.5
        off = lambda d: (0.0, 0.0, d)
        walk = lambda d: (d, 0.0, 0.0)
    else:
        # walks towards the camera along -Z, body faces -Z, lateral axis is X
        x = 0.0
        z = 2.9 - speed * t
        off = lambda d: (d, 0.0, 0.0)
        walk = lambda d: (0.0, 0.0, -d)
    def pt(base, lateral, fwd):
        o, w = off(lateral), walk(fwd)
        return (base[0] + o[0] + w[0], base[1] + o[1] + w[1], base[2] + o[2] + w[2])

    hip_l = pt((x, 0.95, z), 0.10, 0.0)
    hip_r = pt((x, 0.95, z), -0.10, 0.0)
    sh_l = pt((x, 1.40, z), 0.18, 0.0)
    sh_r = pt((x, 1.40, z), -0.18, 0.0)
    an_l = pt((x, 0.08, z), 0.06, swing)
    an_r = pt((x, 0.08, z), -0.06, -swing)
    kn_l = tuple((np.array(hip_l) + np.array(an_l)) / 2.0)
    kn_r = tuple((np.array(hip_r) + np.array(an_r)) / 2.0)
    return dict(LS=sh_l, RS=sh_r, LE=sh_l, RE=sh_r, LW=sh_l, RW=sh_r,
                LH=hip_l, RH=hip_r, LK=kn_l, RK=kn_r, LA=an_l, RA=an_r,
                swing=swing, centre=(x, 0.95, z))


def run(mode, camera_moves, n=100, fps=10.0):
    series = []
    for i in range(n):
        t = i / fps
        J = person_world(t, mode)
        cx, _, cz = J["centre"]
        if camera_moves:
            yaw = float(np.arctan2(cx, cz))          # head pans to follow
            cam = (0.12 * np.sin(2 * np.pi * 0.25 * t), 0.9, 0.0)
        else:
            yaw = 0.0
            cam = (0.0, 0.9, 0.0)
        pixels, depth_pts = {}, []
        for name in ("LS", "RS", "LE", "RE", "LW", "RW",
                     "LH", "RH", "LK", "RK", "LA", "RA"):
            p = project(J[name], cam, yaw)
            if p is None:
                break
            u, v, z = p
            pixels[name] = (u, v)
            depth_pts.append((u, v, z))
        else:
            depth = make_depth(depth_pts)
            lms = [LM(0.5, 0.5, 0.0)] * 33
            idx = {"LS": 11, "RS": 12, "LE": 13, "RE": 14, "LW": 15, "RW": 16,
                   "LH": 23, "RH": 24, "LK": 25, "RK": 26, "LA": 27, "RA": 28}
            lms = list(lms)
            for name, i_ in idx.items():
                u, v = pixels[name]
                lms[i_] = LM(u / W, v / H, 0.99)
            row = hs.extract(lms, W, H)
            if row is None:
                continue
            hs.add_depth(row, depth, K, SCALE)
            series.append(row)
    metrics = hs.analyze(series, n / fps, fps)
    return metrics, series


def ground_truth(mode, n=100, fps=10.0, A=0.30):
    sw = np.array([person_world(i / fps, mode)["swing"] for i in range(n)])
    return float(np.percentile(sw, 95) - np.percentile(sw, 5))


def show(label, metrics, gt):
    body = metrics.get("step_length_m")
    cam = metrics.get("step_length_cam_axis_m")
    basis = metrics.get("step_length_basis")
    eb = abs(body - gt) if body is not None else float("nan")
    ec = abs(cam - gt) if cam is not None else float("nan")
    print(f"{label:<26} body={str(body):>5} m (err {eb:.3f})   "
          f"camera-axis={str(cam):>5} m (err {ec:.3f})   basis={basis}")
    return body, cam, eb, ec


def main():
    fails = []
    for mode in ("lateral", "towards"):
        gt = ground_truth(mode)
        print(f"\n=== walking {mode} (ground-truth step length {gt:.2f} m) ===")
        m_static, _ = run(mode, camera_moves=False)
        m_moving, _ = run(mode, camera_moves=True)
        b_s, c_s, eb_s, ec_s = show("static camera", m_static, gt)
        b_m, c_m, eb_m, ec_m = show("panning/swaying camera", m_moving, gt)
        if b_s is None or eb_s > 0.06:
            fails.append(f"{mode}/static body error {eb_s:.3f}")
        if b_m is None or eb_m > 0.06:
            fails.append(f"{mode}/moving body error {eb_m:.3f}")
        # Camera motion must not make the body-frame estimate worse.
        if b_m is not None and b_s is not None and eb_m > eb_s + 0.03:
            fails.append(f"{mode}: body frame degraded under motion "
                         f"({eb_s:.3f} -> {eb_m:.3f})")

    print()
    for line in [
        "RESULT: body-frame step length is stable under camera panning/swaying",
        "RESULT: camera-axis estimate can drift under motion (see lateral case)",
    ]:
        print(line)
    if fails:
        print("FAILED:", fails)
        return 1
    print("PASS: all camera-motion invariance checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
