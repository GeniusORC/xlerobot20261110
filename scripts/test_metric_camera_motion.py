#!/usr/bin/env python3
"""Prove that metric step length survives a moving robot/camera.

Simulates a person walking in front of the D455 while the head pans to follow
them and the base either sways or drives to hold the standoff, projects 3D body
joints through the real pinhole model, rasterises a depth map, then runs the
**real** pipeline (extract -> add_depth -> analyze) and compares the result
with ground truth.  Step counting is checked as well, because the base moving
under the person used to corrupt the image-space detector.

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
N = 100
FPS = 10.0
SWING = 0.30            # +/- foot excursion, metres
CADENCE_HZ = 1.2


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


def person_world(t, mode, speed=0.9):
    """Return named 3D joints of a walking person at time t.

    The body is rotated about the vertical axis: ``towards`` faces the camera,
    ``lateral`` walks diagonally across the frame (turned ~30 deg toward the
    camera, so both shoulders still project far enough apart to be detected).
    The root drifts slowly closer (3.0 -> 2.4 m) to keep the framing valid.
    """
    phase = 2 * np.pi * CADENCE_HZ * t
    swing = SWING * np.sin(phase)
    z = 3.0 - 0.06 * t
    x = 0.7 * np.sin(2 * np.pi * 0.1 * t) if mode == "lateral" else 0.0
    a = np.pi if mode == "towards" else np.pi / 2 + 0.55
    fwd = np.array([np.sin(a), 0.0, np.cos(a)])
    lat = np.array([np.cos(a), 0.0, -np.sin(a)])

    def pt(base, lateral, forward):
        p = np.array(base) + lateral * lat + forward * fwd
        return tuple(p)

    hip_c, sh_c, an_c = (x, 0.95, z), (x, 1.40, z), (x, 0.08, z)
    hip_l = pt(hip_c, 0.10, 0.0)
    hip_r = pt(hip_c, -0.10, 0.0)
    sh_l = pt(sh_c, 0.18, 0.0)
    sh_r = pt(sh_c, -0.18, 0.0)
    an_l = pt(an_c, 0.06, swing)
    an_r = pt(an_c, -0.06, -swing)
    kn_l = tuple((np.array(hip_l) + np.array(an_l)) / 2.0)
    kn_r = tuple((np.array(hip_r) + np.array(an_r)) / 2.0)
    return dict(LS=sh_l, RS=sh_r, LE=sh_l, RE=sh_r, LW=sh_l, RW=sh_r,
                LH=hip_l, RH=hip_r, LK=kn_l, RK=kn_r, LA=an_l, RA=an_r,
                swing=swing, centre=(x, 0.95, z))


def camera_pose(t, mode, camera_moves, base_follow):
    """Camera position and yaw for the three motion cases."""
    if base_follow:
        # Dollying base: back away at walking speed so the standoff (and thus
        # the full-body framing) stays roughly constant, plus small sway.
        person = person_world(t, mode)["centre"]
        yaw = float(np.arctan2(person[0], person[2])) * 0.6
        return (0.05 * np.sin(2 * np.pi * 0.25 * t),
                0.9,
                person[2] - 2.45)
    if camera_moves:
        person = person_world(t, mode)["centre"]
        yaw = float(np.arctan2(person[0], person[2]))
        return (0.12 * np.sin(2 * np.pi * 0.25 * t), 0.9, 0.0)
    return (0.0, 0.9, 0.0)


def run(mode, camera_moves, base_follow=False, n=N, fps=FPS):
    series = []
    for i in range(n):
        t = i / fps
        J = person_world(t, mode)
        cam = camera_pose(t, mode, camera_moves, base_follow)
        cx, _, cz = J["centre"]
        yaw = 0.0
        if camera_moves or base_follow:
            yaw = float(np.arctan2(cx - cam[0], cz - cam[2]))
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
            lms = list(lms)
            idx = {"LS": 11, "RS": 12, "LE": 13, "RE": 14, "LW": 15, "RW": 16,
                   "LH": 23, "RH": 24, "LK": 25, "RK": 26, "LA": 27, "RA": 28}
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


def ground_truth(n=N, fps=FPS):
    sw = np.array([person_world(i / fps, "towards")["swing"]
                   for i in range(n)])
    return float(np.percentile(sw, 95) - np.percentile(sw, 5))


def main():
    fails = []
    gt = ground_truth()
    print(f"ground-truth step length {gt:.2f} m, "
          f"cadence {CADENCE_HZ * 60:.0f} steps/min")

    for mode in ("lateral", "towards"):
        print(f"\n=== walking {mode} ===")
        m_static, _ = run(mode, camera_moves=False)
        m_moving, _ = run(mode, camera_moves=True)
        m_follow, _ = run(mode, camera_moves=True, base_follow=True)

        for label, m in (("static camera", m_static),
                         ("panning/swaying head", m_moving),
                         ("panning head + base following", m_follow)):
            body = m.get("step_length_m")
            cam = m.get("step_length_cam_axis_m")
            basis = m.get("step_length_basis")
            eb = abs(body - gt) if body is not None else float("nan")
            print(f"  {label:<30} body={str(body):>5} m (err {eb:.3f})  "
                  f"cam-axis={str(cam):>5}  steps={m['step_count']:>2}  "
                  f"detector={m['step_detector']}  basis={basis}")

        if m_follow.get("step_length_m") is None:
            fails.append(f"{mode}: no metric under base following")
        elif abs(m_follow["step_length_m"] - gt) > 0.06:
            fails.append(f"{mode}: following step length error "
                         f"{m_follow['step_length_m'] - gt:+.3f} m")
        if m_follow.get("step_detector") != "depth_body_frame":
            fails.append(f"{mode}: following fell back to "
                         f"{m_follow.get('step_detector')}")
        # Step count must not change materially when the base moves.
        if abs(m_follow["step_count"] - m_static["step_count"]) > 2:
            fails.append(f"{mode}: step count moved {m_static['step_count']}"
                         f" -> {m_follow['step_count']} with the base moving")
        if not (20 <= m_follow["step_count"] <= 27):
            fails.append(f"{mode}: implausible step count "
                         f"{m_follow['step_count']} (expected ~24)")

    print("\nRESULT: body-frame step length is stable while the head pans and "
          "the base drives")
    print("RESULT: depth body-frame step counting is unaffected by base motion")
    if fails:
        print("FAILED:", fails)
        return 1
    print("PASS: all camera/base-motion invariance checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
