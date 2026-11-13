#!/usr/bin/env python3
"""Fixed health station: robot stops, head RGB + MediaPipe 2D gait/posture.

  source scripts/env.sh voice
  python scripts/health_station_web.py --port 8084

Open http://127.0.0.1:8084
The camera source defaults to the head stream from camera_web:
http://127.0.0.1:8080/snapshot/head
"""
import argparse
import glob
import io
import json
import os
import sqlite3
import threading
import time
import traceback
from datetime import datetime

import cv2
import mediapipe as mp
import numpy as np
import requests
from flask import Flask, jsonify, request, send_from_directory
from mediapipe.tasks import python
from mediapipe.tasks.python import vision
from scipy.signal import find_peaks

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEALTH_DIR = os.path.join(ROOT, "data", "health")
os.makedirs(HEALTH_DIR, exist_ok=True)
DB_PATH = os.path.join(HEALTH_DIR, "health.db")
MODEL = os.path.join(ROOT, "models", "pose_landmarker_lite.task")
ROBOT_API = os.environ.get("ROBOT_API", "http://127.0.0.1:8090")
DEFAULT_SOURCE = "http://127.0.0.1:8080/snapshot/head"

app = Flask(__name__)

STATE = {
    "running": False,
    "progress": 0.0,
    "error": None,
    "latest": None,
    "source": DEFAULT_SOURCE,
    "duration": 10.0,
    "track": False,          # head follows the person (preview + capture)
    "aiming": False,         # auto-framing loop running
    "track_message": "",
    "distance_m": None,
}
LOCK = threading.Lock()
TRACK_LOCK = threading.Lock()
STOP = threading.Event()
LANDMARKER = None
AIM_LANDMARKER = None
TRACK_SRC = None
TRACKER = None
TARGET_DISTANCE = 2.6

# Major body connections for the overlay (MediaPipe legacy connections are not
# exported by the Tasks API in mediapipe 1.x).
CONNECTIONS = [
    (11, 12), (11, 13), (13, 15), (12, 14), (14, 16),
    (11, 23), (12, 24), (23, 24), (23, 25), (25, 27),
    (24, 26), (26, 28), (27, 29), (29, 31), (28, 30), (30, 32),
    (27, 31), (28, 32),
]


def make_landmarker():
    base = python.BaseOptions(model_asset_path=MODEL)
    opts = vision.PoseLandmarkerOptions(
        base_options=base,
        running_mode=vision.RunningMode.VIDEO,
        num_poses=1,
        min_pose_detection_confidence=0.45,
        min_pose_presence_confidence=0.45,
        min_tracking_confidence=0.45,
    )
    return vision.PoseLandmarker.create_from_options(opts)


class Source:
    """Colour (and optionally metric depth) source.

    Depth-capable modes:
      * ``realsense:SERIAL``  - opens the D455 directly (single owner).
      * HTTP snapshot with ``depth=True`` - fetches ``.../snapshot/head_npz``
        from camera_web, which returns colour + aligned depth + intrinsics.
    """

    def __init__(self, source, depth=False):
        self.source = source
        self.cap = None
        self.rs_pipe = None
        self.depth_url = None
        self._depth = None
        self._K = None
        self._depth_scale = 0.001
        if source.startswith("realsense:"):
            import pyrealsense2 as rs
            serial = source.split(":", 1)[1].strip()
            cfg = rs.config()
            if serial:
                cfg.enable_device(serial)
            cfg.enable_stream(rs.stream.color, 640, 480, rs.format.bgr8, 30)
            cfg.enable_stream(rs.stream.depth, 640, 480, rs.format.z16, 30)
            self._rs = rs
            self._align = rs.align(rs.stream.color)
            self.rs_pipe = rs.pipeline()
            profile = self.rs_pipe.start(cfg)
            intr = profile.get_stream(
                rs.stream.color).as_video_stream_profile().get_intrinsics()
            self._K = (float(intr.fx), float(intr.fy), float(intr.ppx), float(intr.ppy))
            try:
                self._depth_scale = float(
                    profile.get_device().first_depth_sensor().get_depth_scale())
            except Exception:
                self._depth_scale = 0.001
        elif depth:
            self.depth_url = derive_depth_url(source)

    # -- colour ---------------------------------------------------------
    def read(self):
        if self.rs_pipe is not None:
            try:
                frames = self._align.process(self.rs_pipe.wait_for_frames())
                color = frames.get_color_frame()
                depth = frames.get_depth_frame()
                if depth:
                    self._depth = np.asanyarray(depth.get_data()).copy()
                if color:
                    return np.asanyarray(color.get_data())
            except Exception:
                return None
            return None
        if self.depth_url:
            return self._read_npz()
        if self.source.startswith("http://") or self.source.startswith("https://"):
            return self._read_http_color()
        if self.source.lower().endswith((".jpg", ".jpeg", ".png", ".bmp", ".webp")):
            return cv2.imread(self.source)
        if self.cap is None:
            self.cap = cv2.VideoCapture(self.source)
        ok, frame = self.cap.read()
        if not ok or frame is None:
            # loop a video/file for repeatable tests
            if self.cap is not None:
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, frame = self.cap.read()
        return frame if ok else None

    def _read_http_color(self):
        try:
            r = requests.get(self.source, timeout=2)
            r.raise_for_status()
            buf = np.frombuffer(r.content, np.uint8)
            return cv2.imdecode(buf, cv2.IMREAD_COLOR)
        except Exception:
            return None

    def _read_npz(self):
        try:
            r = requests.get(self.depth_url, timeout=3)
            r.raise_for_status()
            z = np.load(io.BytesIO(r.content))
            self._depth = np.asarray(z["depth"], dtype=np.uint16)
            self._K = (float(z["fx"]), float(z["fy"]),
                       float(z["cx"]), float(z["cy"]))
            self._depth_scale = float(z["depth_scale"])
            return cv2.imdecode(z["color_jpeg"], cv2.IMREAD_COLOR)
        except Exception:
            # Depth endpoint unavailable: keep the colour stream working.
            self._depth = None
            self.depth_url = None
            return self._read_http_color()

    # -- depth ----------------------------------------------------------
    def read_depth(self):
        """Return (depth_uint16_aligned_to_colour, K) or (None, None)."""
        if self._depth is None or self._K is None:
            return None, None
        return self._depth, self._K

    @property
    def depth_scale(self):
        return self._depth_scale


def derive_depth_url(source):
    """Map camera_web's colour snapshot URL to the depth-packed endpoint."""
    s = source.rstrip("/")
    if s.endswith("/snapshot/head"):
        return s + "_npz"
    return None


# ---------------------------------------------------------------------------
# Active camera: head pan/tilt and base fine positioning
# ---------------------------------------------------------------------------

class HeadClient:
    """Small client for robot_api's /head and /drive endpoints.

    The head is commanded in *relative* normalized units, so no motor-angle
    calibration is needed: we close the loop on pixels.  If the pan direction
    is wrong for a given build, the tracker notices the error growing and flips
    the sign by itself (only while the target is roughly still).
    """

    def __init__(self, api=ROBOT_API):
        self.api = api.rstrip("/")
        self.pan_sign = 1.0
        self.tilt_sign = 1.0
        self._prev = None
        self._worse = 0
        self.fails = 0
        self.search_dir = 1.0
        self.pan = 0.0
        self.tilt = 0.0

    def available(self):
        return self.fails < 5

    def _post(self, path, payload, timeout=1.2):
        try:
            r = requests.post(f"{self.api}{path}", json=payload, timeout=timeout)
            if r.status_code == 503:
                self.fails += 1
                return False
            r.raise_for_status()
            self.fails = 0
            return True
        except Exception:
            self.fails += 1
            return False

    def head(self, dpan=0.0, dtilt=0.0, pan=None, tilt=None, center=False):
        payload = {}
        if center:
            payload["center"] = True
        else:
            if pan is None and abs(dpan) > 1e-3:
                payload["dpan"] = float(dpan)
            if tilt is None and abs(dtilt) > 1e-3:
                payload["dtilt"] = float(dtilt)
        if not payload:
            return True
        return self._post("/head", payload)

    def drive(self, x=0.0, theta=0.0, duration=0.5):
        return self._post("/drive", {"x": x, "theta": theta,
                                     "duration": duration}, timeout=2.0)

    def step(self, u, v, w, h, v_frac=0.5, kp=(9.0, 6.0), dead=0.02,
             adapt=False):
        """Move the head so the target pixel moves toward (w/2, v_frac*h)."""
        ex = (u - w / 2.0) / float(w)
        ey = (v - v_frac * h) / float(h)
        if adapt and self._prev is not None:
            if abs(ex) > abs(self._prev[0]) + 0.01:
                self._worse += 1
                if self._worse >= 3:
                    self.pan_sign *= -1.0
                    self._worse = 0
                    self._prev = None
                    return False
            else:
                self._worse = 0
        self._prev = (ex, ey)
        dpan = 0.0 if abs(ex) < dead else -self.pan_sign * kp[0] * ex
        dtilt = 0.0 if abs(ey) < dead else -self.tilt_sign * kp[1] * ey
        dpan = float(np.clip(dpan, -12.0, 12.0))
        dtilt = float(np.clip(dtilt, -8.0, 8.0))
        if dpan == 0.0 and dtilt == 0.0:
            return True
        ok = self.head(dpan=dpan, dtilt=dtilt)
        if ok:
            self.pan = float(np.clip(self.pan + dpan, -70, 70))
            self.tilt = float(np.clip(self.tilt + dtilt, -30, 30))
        return ok


def get_tracker():
    global TRACKER
    if TRACKER is None:
        TRACKER = HeadClient(ROBOT_API)
    return TRACKER


def get_track_source():
    global TRACK_SRC
    if TRACK_SRC is None:
        TRACK_SRC = Source(DEFAULT_SOURCE, depth=True)
    return TRACK_SRC


def make_image_landmarker():
    base = python.BaseOptions(model_asset_path=MODEL)
    opts = vision.PoseLandmarkerOptions(
        base_options=base,
        running_mode=vision.RunningMode.IMAGE,
        num_poses=1,
        min_pose_detection_confidence=0.4,
        min_pose_presence_confidence=0.4,
    )
    return vision.PoseLandmarker.create_from_options(opts)


def detect_person(frame, depth, K, scale):
    """Detect the largest full body; return (row, distance, (u, v)) or None."""
    global AIM_LANDMARKER
    if frame is None:
        return None
    if AIM_LANDMARKER is None:
        AIM_LANDMARKER = make_image_landmarker()
    h, w = frame.shape[:2]
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    res = AIM_LANDMARKER.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
    if not res.pose_landmarks:
        return None
    lms = res.pose_landmarks[0]
    row = extract(lms, w, h)
    if row is None:
        return None
    dist = None
    if depth is not None and K is not None:
        add_depth(row, depth, K, scale)
        if np.isfinite(row.get("dist_m", np.nan)):
            dist = row["dist_m"]
    sh = row.get("px_hip")
    if sh is None:
        return None
    u = (row["px_hip"][0] + row["px_ls"][0] + row["px_rs"][0]) / 3.0
    v = (row["px_ls"][1] + row["px_rs"][1] + row["px_lh"][1] + row["px_rh"][1]) / 4.0
    return row, dist, (u, v)


def set_track(**kw):
    with TRACK_LOCK:
        STATE.update(kw)


def aim_robot(target=None, tol=0.45, timeout=40.0):
    """Drive to the working distance and aim the head at the person.

    Runs before a capture so the walk happens at 2-3 m with the full body in
    frame.  The base does *not* move during the capture itself: only the head
    pans, which the body-frame measurement is invariant to.
    """
    target = float(target or TARGET_DISTANCE)
    tracker = get_tracker()
    set_track(aiming=True, track_message="正在寻找站立位置")
    t0 = time.time()
    last = None
    try:
        while time.time() - t0 < timeout:
            if tracker.fails >= 6:
                set_track(aiming=False,
                          track_message="机器人未连接：请先启动 robot_api.py（8090）")
                return
            src = get_track_source()
            frame = src.read()
            depth, K = src.read_depth()
            person = detect_person(frame, depth, K, src.depth_scale)
            if person is None:
                # Search: sweep the head left/right until a body appears.
                tracker.head(dpan=10.0 * tracker.search_dir)
                tracker.search_dir *= -1.0
                set_track(track_message="没看到全身，正在左右寻找")
                time.sleep(0.8)
                continue
            row, dist, (u, v) = person
            h, w = frame.shape[:2]
            ex = (u - w / 2.0) / w
            set_track(distance_m=None if dist is None else round(dist, 2))
            last = (row, dist, ex)
            if dist is not None:
                if dist > target + tol:
                    dur = float(np.clip((dist - target) / 0.05, 0.3, 2.5))
                    set_track(track_message=f"距离 {dist:.1f} m，向前靠近")
                    tracker.drive(0.05, 0.0, dur)
                    time.sleep(dur + 0.35)
                    continue
                if dist < target - tol:
                    dur = float(np.clip((target - dist) / 0.05, 0.3, 2.5))
                    set_track(track_message=f"距离 {dist:.1f} m，向后退一点")
                    tracker.drive(-0.05, 0.0, dur)
                    time.sleep(dur + 0.35)
                    continue
            if abs(ex) > 0.05:
                tracker.step(u, v, w, h, v_frac=0.45, adapt=True)
                set_track(track_message=f"正在对准您（偏差 {ex * 100:+.0f}%）")
                time.sleep(0.25)
                continue
            if dist is not None and abs(dist - target) <= tol and abs(ex) <= 0.05:
                set_track(aiming=False,
                          track_message=f"已就位：距离 {dist:.1f} m，可以开始采集")
                return
            time.sleep(0.2)
        msg = "自动取景超时"
        if last is not None and last[1] is not None:
            msg += f"（最后距离 {last[1]:.1f} m）"
        set_track(aiming=False, track_message=msg + "，可手动调整后采集")
    except Exception as e:
        traceback.print_exc()
        set_track(aiming=False, track_message=f"取景失败: {type(e).__name__}: {e}")


def body_center(lms, w, h):
    """Torso centre from raw pose landmarks (works for upper-body views).

    Returns ((u, v), v_frac, visibility) or None.  Unlike ``extract`` this does
    not require the full body, so it can track someone sitting or standing
    close to the camera.
    """
    def pt(i, min_vis=0.4):
        p = lms[i]
        if getattr(p, "visibility", 0.0) < min_vis:
            return None
        return np.array([p.x * w, p.y * h], dtype=float)

    pts = [pt(i) for i in (11, 12, 23, 24)]
    pts = [p for p in pts if p is not None]
    if len(pts) >= 2:
        c = np.mean(pts, axis=0)
        return c, 0.45, 1.0
    nose = pt(0, 0.3)
    if nose is not None:
        return nose, 0.33, 0.5
    return None


def idle_track_loop():
    """Keep the head on the person while idle (visible camera motion)."""
    while True:
        try:
            if (STATE.get("track") and not STATE.get("running")
                    and not STATE.get("aiming")):
                tracker = get_tracker()
                if tracker.fails >= 5:
                    set_track(track_message="机器人未连接：请先启动 "
                                            "robot_api.py（8090）")
                    time.sleep(1.5)
                    continue
                src = get_track_source()
                frame = src.read()
                depth, K = src.read_depth()
                if frame is not None:
                    h, w = frame.shape[:2]
                    global AIM_LANDMARKER
                    if AIM_LANDMARKER is None:
                        AIM_LANDMARKER = make_image_landmarker()
                    res = AIM_LANDMARKER.detect(mp.Image(
                        image_format=mp.ImageFormat.SRGB,
                        data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)))
                    target = None
                    if res.pose_landmarks:
                        target = body_center(res.pose_landmarks[0], w, h)
                    if target is not None:
                        (u, v), v_frac, _ = target
                        tracker.step(u, v, w, h, v_frac=v_frac,
                                     kp=(8.0, 5.0), adapt=True)
                        if depth is not None and K is not None:
                            z = depth_patch(depth, u, v, radius=8,
                                            scale=src.depth_scale)
                            if np.isfinite(z):
                                set_track(distance_m=round(z, 2))
                        set_track(track_message="头部跟随中")
                    else:
                        set_track(track_message="没有看到人，头部保持不动")
        except Exception:
            traceback.print_exc()
        time.sleep(0.15)




def point(lms, idx, w, h, min_vis=0.3):
    p = lms[idx]
    if p.visibility < min_vis:
        return None
    return np.array([p.x * w, p.y * h], dtype=float)


def angle(a, b, c):
    if a is None or b is None or c is None:
        return np.nan
    v1, v2 = a - b, c - b
    n1, n2 = np.linalg.norm(v1), np.linalg.norm(v2)
    if n1 < 1e-6 or n2 < 1e-6:
        return np.nan
    cosv = float(np.clip(np.dot(v1, v2) / (n1 * n2), -1.0, 1.0))
    return float(np.degrees(np.arccos(cosv)))


def extract(lms, w, h):
    idx = {
        "LS": 11, "RS": 12, "LH": 23, "RH": 24,
        "LK": 25, "RK": 26, "LA": 27, "RA": 28,
        "LE": 13, "RE": 14, "LW": 15, "RW": 16,
    }
    p = {k: point(lms, i, w, h) for k, i in idx.items()}
    need = ("LS", "RS", "LH", "RH", "LK", "RK", "LA", "RA")
    if any(p[k] is None for k in need):
        return None
    # Reject partial bodies: head/torso close-ups often produce hallucinated
    # leg landmarks outside the frame, which would create fake gait numbers.
    for k in need:
        lm = lms[idx[k]]
        if not (0.01 < lm.x < 0.99 and 0.01 < lm.y < 0.99):
            return None
    shoulder_w = float(np.linalg.norm(p["LS"] - p["RS"]))
    if shoulder_w < 15:
        return None
    hip_mid = (p["LH"] + p["RH"]) / 2.0
    sh_mid = (p["LS"] + p["RS"]) / 2.0
    trunk = float(np.degrees(np.arctan2(sh_mid[0] - hip_mid[0], hip_mid[1] - sh_mid[1])))
    return {
        "knee_l": angle(p["LH"], p["LK"], p["LA"]),
        "knee_r": angle(p["RH"], p["RK"], p["RA"]),
        "hip_l": angle(p["LS"], p["LH"], p["LK"]),
        "hip_r": angle(p["RS"], p["RH"], p["RK"]),
        "trunk": trunk,
        "ankle_lx": float(p["LA"][0] / shoulder_w),
        "ankle_rx": float(p["RA"][0] / shoulder_w),
        "ankle_ly": float(p["LA"][1] / h),
        "ankle_ry": float(p["RA"][1] / h),
        "hip_x": float(hip_mid[0] / shoulder_w),
        "shoulder_w": shoulder_w,
        # Pixel positions kept for metric depth sampling (not stored in DB).
        "px_hip": hip_mid,
        "px_ankle_l": p["LA"],
        "px_ankle_r": p["RA"],
        "px_ls": p["LS"],
        "px_rs": p["RS"],
        "px_lh": p["LH"],
        "px_rh": p["RH"],
    }


def depth_patch(depth, u, v, radius=6, scale=0.001, lo=0.15, hi=9.0):
    """Median metric depth (metres) in a small window; NaN if unusable."""
    if depth is None:
        return float("nan")
    h, w = depth.shape[:2]
    x0, x1 = max(0, int(u) - radius), min(w, int(u) + radius + 1)
    y0, y1 = max(0, int(v) - radius), min(h, int(v) + radius + 1)
    if x1 <= x0 or y1 <= y0:
        return float("nan")
    patch = depth[y0:y1, x0:x1].astype(np.float32) * float(scale)
    patch = patch[(patch > lo) & (patch < hi)]
    if patch.size < 5:
        return float("nan")
    return float(np.median(patch))


def _unproject(u, v, z, K):
    """Pixel + metric depth -> 3D point in the camera frame (metres)."""
    fx, fy, cx, cy = K
    return np.array([(u - cx) * z / fx, (v - cy) * z / fy, z], dtype=float)


def add_depth(row, depth, K, scale=0.001):
    """Attach metric depth features to one pose row.

    Two coordinate sets are produced:

    1. **Body frame** (preferred, robust to camera motion).  Shoulders, hips and
       ankles are unprojected to 3D with their own depth; the body's up and
       lateral axes give the facing direction ``fwd``; the foot excursion is
       ``(ankle - hip_mid) . fwd``.  Because both the foot vector and the axis
       live in the same camera frame, panning/tilting/driving the robot does
       not change this number.

    2. **Camera axis** (fallback).  Foot depth relative to the hip along the
       dominant camera axis.  Only valid when the camera is static.
    """
    fx, fy, cx, cy = K
    hip = row.get("px_hip")
    la = row.get("px_ankle_l")
    ra = row.get("px_ankle_r")
    if hip is None or la is None or ra is None:
        return row

    def dz(px, radius):
        return depth_patch(depth, px[0], px[1], radius=radius, scale=scale)

    z_hip = dz(hip, 10)
    z_l = dz(la, 5)
    z_r = dz(ra, 5)
    row["dist_m"] = z_hip
    row["hip_x_m"] = (float(hip[0]) - cx) * z_hip / fx
    row["foot_l_z"] = z_l
    row["foot_r_z"] = z_r
    row["foot_l_x"] = (float(la[0]) - cx) * z_l / fx
    row["foot_r_x"] = (float(ra[0]) - cx) * z_r / fx
    row["hip_z_m"] = z_hip

    # ---- body frame (camera-motion invariant) ---------------------------
    px = {k: row.get(f"px_{k}") for k in ("ls", "rs", "lh", "rh")}
    if any(v is None for v in px.values()) or not np.isfinite(z_hip):
        return row
    z_ls, z_rs = dz(px["ls"], 8), dz(px["rs"], 8)
    z_lh, z_rh = dz(px["lh"], 8), dz(px["rh"], 8)
    zs = {"ls": z_ls, "rs": z_rs, "lh": z_lh, "rh": z_rh}
    used_fallback = False
    for name, val in list(zs.items()):
        if not np.isfinite(val):
            zs[name] = z_hip
            used_fallback = True
    P = lambda pxk, z: _unproject(float(px[pxk][0]), float(px[pxk][1]), z, K)
    p_ls, p_rs = P("ls", zs["ls"]), P("rs", zs["rs"])
    p_lh, p_rh = P("lh", zs["lh"]), P("rh", zs["rh"])
    sh_mid = (p_ls + p_rs) / 2.0
    hip_mid = (p_lh + p_rh) / 2.0
    up = sh_mid - hip_mid
    n_up = np.linalg.norm(up)
    if n_up < 1e-3:
        return row
    up = up / n_up
    right = p_rs - p_ls
    right = right - np.dot(right, up) * up
    n_right = np.linalg.norm(right)
    if n_right < 1e-3:
        return row
    right = right / n_right
    fwd = np.cross(up, right)
    fwd = fwd - np.dot(fwd, up) * up
    n_fwd = np.linalg.norm(fwd)
    if n_fwd < 1e-3:
        return row
    fwd = fwd / n_fwd
    p_la = _unproject(float(la[0]), float(la[1]),
                      z_l if np.isfinite(z_l) else z_hip, K)
    p_ra = _unproject(float(ra[0]), float(ra[1]),
                      z_r if np.isfinite(z_r) else z_hip, K)
    row["bf_l"] = float(np.dot(p_la - hip_mid, fwd))
    row["bf_r"] = float(np.dot(p_ra - hip_mid, fwd))
    row["bf_valid"] = not used_fallback
    return row



def draw_overlay(frame, lms):
    h, w = frame.shape[:2]
    out = frame.copy()
    pts = []
    for p in lms:
        pts.append((int(p.x * w), int(p.y * h)) if p.visibility > 0.3 else None)
    for a, b in CONNECTIONS:
        if pts[a] is not None and pts[b] is not None:
            cv2.line(out, pts[a], pts[b], (0, 220, 0), 2)
    for pt in pts:
        if pt is not None:
            cv2.circle(out, pt, 3, (0, 255, 0), -1)
    return out


def analyze(series, duration, fps):
    if not series:
        return None
    def arr(key):
        return np.array([s.get(key, np.nan) for s in series], dtype=float)
    t = np.arange(len(series)) / max(fps, 1e-6)
    kL, kR = arr("knee_l"), arr("knee_r")
    hL, hR = arr("hip_l"), arr("hip_r")
    trunk = arr("trunk")
    aL, aR = arr("ankle_ly"), arr("ankle_ry")
    step_count = 0
    if len(series) > 5:
        try:
            pl, _ = find_peaks(-aL, distance=max(3, int(0.35 * fps)), prominence=0.005)
            pr, _ = find_peaks(-aR, distance=max(3, int(0.35 * fps)), prominence=0.005)
            step_count = int(len(pl) + len(pr))
        except Exception:
            step_count = 0
    sym = float(np.nanmean(np.abs(kL - kR)) /
                max(1e-6, np.nanmean((kL + kR) / 2)) * 100)
    metrics = {
        "frames": len(series),
        "duration_s": round(duration, 2),
        "step_count": step_count,
        "cadence_spm": round(step_count / max(duration, 1e-6) * 60, 1),
        "knee_left_mean_deg": round(float(np.nanmean(kL)), 1),
        "knee_right_mean_deg": round(float(np.nanmean(kR)), 1),
        "knee_left_range_deg": round(float(np.nanmax(kL) - np.nanmin(kL)), 1),
        "knee_right_range_deg": round(float(np.nanmax(kR) - np.nanmin(kR)), 1),
        "hip_left_mean_deg": round(float(np.nanmean(hL)), 1),
        "hip_right_mean_deg": round(float(np.nanmean(hR)), 1),
        "trunk_mean_deg": round(float(np.nanmean(trunk)), 1),
        "trunk_std_deg": round(float(np.nanstd(trunk)), 1),
        "knee_symmetry_pct": round(sym, 1),
        "step_length_norm": round(float(np.nanmax(np.abs(arr("ankle_lx") - arr("ankle_rx")))), 3),
        "sway_norm": round(float(np.nanstd(arr("hip_x"))), 3),
    }
    metrics.update(metric_depth_metrics(series))
    return metrics


def _excursion(values, lo=0.02, hi=1.2):
    v = values[np.isfinite(values)]
    if v.size < 8:
        return None
    span = float(np.percentile(v, 95) - np.percentile(v, 5))
    if lo < span < hi:
        return span
    return None


def metric_depth_metrics(series):
    """Metre-scale distance and step length from the aligned D455 depth.

    Preferred estimate: **body frame**.  Each foot's forward offset from the
    hip midpoint is measured along the person's own facing direction, so the
    value is invariant to the robot panning, tilting or driving during the
    walk.  That is what makes an actively moving head camera usable for
    metric gait measurement.

    Fallback: camera axis.  Foot depth (or lateral metres) relative to the
    body along the dominant camera axis; only valid for a static camera.
    """
    out = {}
    z = np.array([s.get("dist_m", np.nan) for s in series], dtype=float)
    hx = np.array([s.get("hip_x_m", np.nan) for s in series], dtype=float)
    z_ok = z[np.isfinite(z)]
    out["depth_frames"] = int(z_ok.size)
    if z_ok.size < 5:
        out["depth_used"] = False
        out["distance_m"] = None
        out["step_length_m"] = None
        out["walk_axis"] = None
        return out
    out["depth_used"] = True
    out["distance_m"] = round(float(np.median(z_ok)), 2)

    # --- preferred: body frame -----------------------------------------
    bf_l = np.array([s.get("bf_l", np.nan) for s in series], dtype=float)
    bf_r = np.array([s.get("bf_r", np.nan) for s in series], dtype=float)
    body_frames = int(np.isfinite(bf_l).sum())
    out["body_frames"] = body_frames
    spans = [s for s in (_excursion(bf_l), _excursion(bf_r)) if s is not None]
    if spans:
        out["step_length_m"] = round(float(np.mean(spans)), 2)
        out["step_length_basis"] = "body_frame"
        out["walk_axis"] = "body"
        out["foot_excursion_m"] = [round(v, 2) for v in spans]

    # --- fallback: camera axis -----------------------------------------
    z_span = float(np.nanmax(z_ok) - np.nanmin(z_ok))
    hx_ok = hx[np.isfinite(hx)]
    x_span = float(np.nanmax(hx_ok) - np.nanmin(hx_ok)) if hx_ok.size >= 5 else 0.0
    axis = "z" if z_span >= x_span else "x"
    body = z if axis == "z" else hx
    ex_l = np.array([s.get(f"foot_l_{axis}", np.nan) for s in series], dtype=float)
    ex_r = np.array([s.get(f"foot_r_{axis}", np.nan) for s in series], dtype=float)
    cam_spans = [s for s in (_excursion(body - ex_l), _excursion(body - ex_r))
                 if s is not None]
    out["walk_axis_cam"] = axis
    out["step_length_cam_axis_m"] = (round(float(np.mean(cam_spans)), 2)
                                     if cam_spans else None)
    if "step_length_m" not in out:
        out["step_length_m"] = out["step_length_cam_axis_m"]
        out["step_length_basis"] = "camera_axis" if out["step_length_m"] else None
        out["walk_axis"] = axis if out["step_length_m"] else None
    return out



def plot_gait(series, fps, path):
    t = np.arange(len(series)) / max(fps, 1e-6)
    def a(k):
        return np.array([s.get(k, np.nan) for s in series], dtype=float)
    fwd_l = a("dist_m") - a("foot_l_z")
    fwd_r = a("dist_m") - a("foot_r_z")
    has_depth = bool(np.isfinite(fwd_l).sum() > 5)
    rows = 4 if has_depth else 3
    fig, axs = plt.subplots(rows, 1, figsize=(9, 2.6 * rows + 0.6), sharex=True)
    axs[0].plot(t, a("knee_l"), label="left knee")
    axs[0].plot(t, a("knee_r"), label="right knee")
    axs[0].set_ylabel("knee angle (deg)")
    axs[0].legend(loc="best")
    axs[1].plot(t, a("hip_l"), label="left hip")
    axs[1].plot(t, a("hip_r"), label="right hip")
    axs[1].set_ylabel("hip angle (deg)")
    axs[1].legend(loc="best")
    axs[2].plot(t, a("ankle_ly"), label="left ankle y")
    axs[2].plot(t, a("ankle_ry"), label="right ankle y")
    axs[2].set_ylabel("ankle y (norm)")
    axs[2].legend(loc="best")
    if has_depth:
        axs[3].plot(t, fwd_l, label="left foot fwd (m)")
        axs[3].plot(t, fwd_r, label="right foot fwd (m)")
        axs[3].plot(t, a("dist_m"), "--", color="#888", label="body distance (m)")
        axs[3].set_ylabel("metres")
        axs[3].legend(loc="best")
    axs[-1].set_xlabel("time (s)")
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def run_capture(source, duration, fps):
    global LANDMARKER
    with LOCK:
        STATE.update(running=True, progress=0.0, error=None)
    STOP.clear()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    src = Source(source, depth=True)
    depth_ok = 0
    series = []
    last_overlay = None
    start = time.time()
    lm = LANDMARKER
    try:
        if lm is None:
            lm = make_landmarker()
            LANDMARKER = lm
        while not STOP.is_set():
            elapsed = time.time() - start
            if elapsed >= duration:
                break
            frame = src.read()
            if frame is not None:
                h, w = frame.shape[:2]
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                res = lm.detect_for_video(mp_img, int(elapsed * 1000) + 1)
                if res.pose_landmarks:
                    lms = res.pose_landmarks[0]
                    row = extract(lms, w, h)
                    if row is not None:
                        depth, K = src.read_depth()
                        if depth is not None and K is not None:
                            add_depth(row, depth, K, src.depth_scale)
                            if np.isfinite(row.get("dist_m", np.nan)):
                                depth_ok += 1
                                with LOCK:
                                    STATE["distance_m"] = round(
                                        float(row["dist_m"]), 2)
                        if STATE.get("track"):
                            u = (row["px_hip"][0] + row["px_ls"][0]
                                 + row["px_rs"][0]) / 3.0
                            v = (row["px_ls"][1] + row["px_rs"][1]
                                 + row["px_lh"][1] + row["px_rh"][1]) / 4.0
                            get_tracker().step(u, v, w, h, v_frac=0.42,
                                               adapt=False)
                        series.append(row)
                        last_overlay = draw_overlay(frame, lms)
                with LOCK:
                    STATE["progress"] = min(1.0, elapsed / duration)
            time.sleep(max(0.0, 1.0 / fps - 0.005))
        elapsed = time.time() - start
        if not series:
            with LOCK:
                STATE.update(running=False, progress=1.0,
                             error="没有检测到完整人体，请让老人站在画面中并露出全身")
            return
        metrics = analyze(series, min(elapsed, duration), fps)
        pose_path = os.path.join(HEALTH_DIR, f"pose_{ts}.png")
        gait_path = os.path.join(HEALTH_DIR, f"gait_{ts}.png")
        json_path = os.path.join(HEALTH_DIR, f"metrics_{ts}.json")
        if last_overlay is not None:
            cv2.imwrite(pose_path, last_overlay)
        plot_gait(series, fps, gait_path)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(metrics, f, ensure_ascii=False, indent=2)
        report, tips, risk = make_report(metrics)
        pose_name = os.path.basename(pose_path) if os.path.exists(pose_path) else None
        gait_name = os.path.basename(gait_path)
        json_name = os.path.basename(json_path)
        session_id = save_session(ts, metrics, report, tips, risk,
                                  pose_name, gait_name, json_name)
        latest = {
            "session_id": session_id,
            "ts": ts,
            "metrics": metrics,
            "report": report,
            "tips": tips,
            "risk": risk,
            "pose": pose_name,
            "gait": gait_name,
            "json": json_name,
        }
        with LOCK:
            STATE.update(running=False, progress=1.0, latest=latest, error=None)
    except Exception as e:
        traceback.print_exc()
        with LOCK:
            STATE.update(running=False, progress=1.0, error=f"{type(e).__name__}: {e}")


def load_latest_result():
    """Load the newest database session; migrate the newest file if needed."""
    try:
        with sqlite3.connect(DB_PATH) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
        if row is not None:
            STATE["latest"] = row_to_latest(row)
            return
    except Exception as e:
        print("DB load failed:", e, flush=True)

    files = sorted(glob.glob(os.path.join(HEALTH_DIR, "metrics_*.json")))
    if not files:
        return
    json_path = files[-1]
    ts = os.path.basename(json_path)[len("metrics_"):-len(".json")]
    try:
        with open(json_path, encoding="utf-8") as f:
            metrics = json.load(f)
    except Exception:
        return
    report, tips, risk = make_report(metrics)
    pose = f"pose_{ts}.png"
    gait = f"gait_{ts}.png"
    pose = pose if os.path.exists(os.path.join(HEALTH_DIR, pose)) else None
    gait = gait if os.path.exists(os.path.join(HEALTH_DIR, gait)) else None
    sid = save_session(ts, metrics, report, tips, risk, pose, gait,
                       os.path.basename(json_path))
    STATE["latest"] = {
        "session_id": sid,
        "ts": ts,
        "metrics": metrics,
        "report": report,
        "tips": tips,
        "risk": risk,
        "pose": pose,
        "gait": gait,
        "json": os.path.basename(json_path),
    }


def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT UNIQUE,
            duration REAL,
            frames INTEGER,
            step_count INTEGER,
            cadence REAL,
            symmetry REAL,
            risk TEXT,
            metrics TEXT,
            report TEXT,
            tips TEXT,
            pose TEXT,
            gait TEXT,
            json TEXT,
            created_at TEXT
        )
        """)
        # Migrate older databases that predate the D455 depth metrics.
        cols = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
        for name, typ in (("distance_m", "REAL"),
                          ("step_length_m", "REAL"),
                          ("depth", "INTEGER")):
            if name not in cols:
                db.execute(f"ALTER TABLE sessions ADD COLUMN {name} {typ}")
        db.commit()


def make_report(m):
    """Rule-based report and health tips. Not a medical diagnosis."""
    steps = int(m.get("step_count", 0) or 0)
    dur = float(m.get("duration_s", 0) or 0)
    cad = float(m.get("cadence_spm", 0) or 0)
    sym = float(m.get("knee_symmetry_pct", 0) or 0)
    kr = (float(m.get("knee_left_range_deg", 0) or 0) +
          float(m.get("knee_right_range_deg", 0) or 0)) / 2
    tm = float(m.get("trunk_mean_deg", 0) or 0)
    ts = float(m.get("trunk_std_deg", 0) or 0)
    sway = float(m.get("sway_norm", 0) or 0)
    step_norm = float(m.get("step_length_norm", 0) or 0)
    dist = m.get("distance_m")
    dist = float(dist) if isinstance(dist, (int, float)) else None
    step_m = m.get("step_length_m")
    step_m = float(step_m) if isinstance(step_m, (int, float)) else None
    depth_used = bool(m.get("depth_used"))

    if steps < 2 or dur < 2.5:
        report = (
            "本次采集没有形成连续步行，可能是站立、转身或人体未完整入镜。"
            "这组数据不能用于步态风险判断，建议让老人自然走 5–10 步后重新采集。"
        )
        if dist is not None:
            report += f"\n深度测得站位距离约 {dist:.1f} 米。"
        tips = [
            "请站在距机器人 2–3 米处，保证头、髋、膝、踝都在画面内。",
            "采集时自然连续走 5–10 步，不要只站着或转身。",
            "如老人行动不便，请家人在旁保护，必要时使用助行器。",
        ]
        return report, tips, "数据不足"

    findings = []
    if cad < 60:
        findings.append("步频偏慢")
    elif cad > 120:
        findings.append("步频偏快")
    if sym > 15:
        findings.append("左右膝活动存在明显不对称")
    if kr < 10:
        findings.append("膝关节活动范围偏小")
    elif kr > 55:
        findings.append("膝关节活动幅度偏大")
    if abs(tm) > 10:
        findings.append("躯干前倾/侧倾较明显")
    if ts > 5:
        findings.append("躯干左右摆动较大")
    if sway > 0.08:
        findings.append("重心左右摆动偏大")
    # Prefer the metric step length when depth is available.
    if step_m is not None:
        if step_m < 0.35:
            findings.append("步幅偏小")
        elif step_m > 0.95:
            findings.append("步幅偏大")
    elif step_norm < 0.8:
        findings.append("步幅偏小")

    if not findings:
        risk = "低"
        summary = "步态节律和左右对称性基本正常，未发现明显异常。"
    elif len(findings) <= 2:
        risk = "中"
        summary = "发现 " + "、".join(findings) + "，建议关注并定期复测。"
    else:
        risk = "高"
        summary = "发现 " + "、".join(findings) + "，建议尽快由家人陪同评估。"

    report = (
        f"本次有效采集 {dur:.1f} 秒、{steps} 步，步频约 {cad:.0f} 步/分；"
        f"左右膝平均活动范围约 {kr:.1f}°，膝对称性偏差 {sym:.1f}%。\n"
    )
    if depth_used and dist is not None:
        report += f"站位距离约 {dist:.1f} 米。\n"
    if step_m is not None:
        report += f"估算步长约 {step_m:.2f} 米（D455 深度换算，供趋势参考）。\n"
    report += (
        f"观察结果：{summary}\n"
        "说明：这是基于 RGB 2D 姿态 + 深度估算的筛查结果，不是医学诊断。"
    )
    if depth_used and dist is not None and not (1.2 <= dist <= 4.5):
        report += (
            f"\n注意：本次站位距离 {dist:.1f} 米，超出建议的 2–3 米，"
            "步长与角度可能偏差，建议调整位置后复测。"
        )
    tips = []
    if cad < 60:
        tips.append("每天分次慢走 10–20 分钟，先热身，量力而行。")
    if sym > 15 or abs(tm) > 10 or sway > 0.08:
        tips.append("可做扶墙单腿站立、重心转移等平衡练习，旁边一定要有人保护。")
    if kr < 10:
        tips.append("做温和的膝关节屈伸和股四头肌拉伸，避免久坐后突然起身。")
    if (step_m is not None and step_m < 0.35) or (step_m is None and step_norm < 0.8):
        tips.append("步幅不要刻意迈大，先求稳，再逐步增加步幅。")
    tips.append("如出现膝髋疼痛、头晕、胸闷或走路突然不稳，请及时就医。")
    tips.append("建议每周在相同位置复测一次，观察趋势而不是单次结果。")
    return report, tips, risk


def save_session(ts, metrics, report, tips, risk, pose, gait, json_name):
    with sqlite3.connect(DB_PATH) as db:
        cur = db.execute(
            """INSERT OR REPLACE INTO sessions
               (ts, duration, frames, step_count, cadence, symmetry, risk,
                metrics, report, tips, pose, gait, json, created_at,
                distance_m, step_length_m, depth)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ts, metrics.get("duration_s"), metrics.get("frames"),
             metrics.get("step_count"), metrics.get("cadence_spm"),
             metrics.get("knee_symmetry_pct"), risk,
             json.dumps(metrics, ensure_ascii=False),
             report, json.dumps(tips, ensure_ascii=False),
             pose, gait, json_name, datetime.now().isoformat(timespec="seconds"),
             metrics.get("distance_m"), metrics.get("step_length_m"),
             1 if metrics.get("depth_used") else 0),
        )
        db.commit()
        return cur.lastrowid


def row_to_latest(row):
    d = dict(row)
    return {
        "session_id": d["id"],
        "ts": d["ts"],
        "metrics": json.loads(d["metrics"]),
        "report": d["report"],
        "tips": json.loads(d["tips"]),
        "risk": d["risk"],
        "pose": d["pose"],
        "gait": d["gait"],
        "json": d["json"],
    }


def stop_robot():
    try:
        requests.post(f"{ROBOT_API}/command", json={"intent": "stop"}, timeout=5)
    except Exception as e:
        print("robot stop failed:", e, flush=True)


@app.get("/")
def index():
    return HTML


@app.post("/api/prepare")
def prepare():
    stop_robot()
    return jsonify({"ok": True, "source": STATE["source"], "message": "机器人已停稳"})


@app.post("/api/start")
def start():
    if STATE["running"]:
        return jsonify({"error": "already running"}), 409
    data = request.get_json(force=True, silent=True) or {}
    duration = float(data.get("duration", 10.0))
    source = str(data.get("source", STATE["source"]))
    STATE["source"] = source
    STATE["duration"] = duration
    stop_robot()
    threading.Thread(target=run_capture, args=(source, duration, 10), daemon=True).start()
    return jsonify({"ok": True, "duration": duration, "source": source})


@app.post("/api/stop")
def stop():
    STOP.set()
    stop_robot()
    return jsonify({"ok": True})


@app.post("/api/track")
def api_track():
    data = request.get_json(force=True, silent=True) or {}
    on = bool(data.get("on", True))
    set_track(track=on,
              track_message="头部跟随已开启" if on else "头部跟随已关闭")
    if not on:
        try:
            get_tracker().head(center=True)
        except Exception:
            pass
    return jsonify({"ok": True, "track": on})


@app.post("/api/aim")
def api_aim():
    if STATE.get("aiming"):
        return jsonify({"ok": False, "error": "already aiming"}), 409
    data = request.get_json(force=True, silent=True) or {}
    target = float(data.get("target", TARGET_DISTANCE))
    threading.Thread(target=aim_robot, args=(target,), daemon=True).start()
    return jsonify({"ok": True, "target": target})


@app.get("/api/history")
def api_history():
    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT * FROM sessions ORDER BY id DESC LIMIT 12").fetchall()
    return jsonify({"sessions": [row_to_latest(r) for r in rows]})


@app.get("/api/session/<int:sid>")
def api_session(sid):
    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if row is None:
        return jsonify({"error": "not found"}), 404
    return jsonify({"latest": row_to_latest(row)})


@app.get("/api/status")
def status():
    with LOCK:
        return jsonify(dict(STATE))


@app.get("/files/<name>")
def files(name):
    return send_from_directory(HEALTH_DIR, name)


HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>固定健康站</title>
<style>
body{background:#111;color:#eee;font-family:system-ui,sans-serif;margin:0;padding:18px}
h1{font-size:22px;margin:0 0 14px}
.grid{display:grid;grid-template-columns:1.05fr 1fr;gap:16px}
.card{background:#1d1d1f;border-radius:12px;padding:16px}
button{font-size:17px;padding:13px 18px;margin:5px;border:0;border-radius:10px;cursor:pointer}
.prep{background:#36c;color:#fff}.start{background:#2a8;color:#fff}.stop{background:#d33;color:#fff}
.aim{background:#963;color:#fff}.track{background:#3a7;color:#fff}
.track.on{background:#0a5}
button:disabled{opacity:.5;cursor:default}
img{width:100%;background:#000;border-radius:8px;margin-top:8px}
#bar{height:10px;background:#333;border-radius:5px;overflow:hidden;margin:10px 0}
#fill{height:100%;width:0;background:#2a8;transition:width .2s}
table{width:100%;border-collapse:collapse;font-size:14px}
td{padding:5px;border-bottom:1px solid #333}
td:first-child{color:#aaa;width:55%}
#msg{font-size:13px;color:#9c9}
small{color:#888}
h3{margin:14px 0 6px;font-size:15px;color:#ddd}
#risk{font-size:14px;margin-bottom:6px}
#report{font-size:13px;line-height:1.6;white-space:pre-wrap;color:#ddd;background:#151517;border-radius:8px;padding:10px}
#tips{margin:6px 0 0 18px;padding:0;font-size:13px;line-height:1.6;color:#ccc}
#tips li{margin:3px 0}
.hist td{font-size:12px}
.hist button{font-size:12px;padding:5px 8px;margin:0}
</style></head><body>
<h1>固定健康站 <small>· 机器人停稳 + D455 彩色/深度 + MediaPipe</small></h1>
<div class="grid">
  <div class="card">
    <button class="prep" onclick="prepare()">准备（停车）</button>
    <button class="start" onclick="startCapture()">开始采集 10 秒</button>
    <button class="stop" onclick="stopCapture()">停止</button>
    <button class="aim" onclick="aim()">自动取景（走到 2–3 米）</button>
    <button id="btn_track" class="track" onclick="toggleTrack()">头部跟随：关</button>
    <div id="track_msg" style="font-size:13px;color:#9cf;margin:6px 0">机器人未连接时仅能采集，不能自动取景。</div>
    <div id="bar"><div id="fill"></div></div>
    <div id="msg">请让老人站在头部相机前，露出全身；距离 2–3 米。</div>
    <img id="live" src="http://127.0.0.1:8080/stream/head">
    <small>实时头部相机 D455（彩色 + 对齐深度）</small>
    <table id="metrics">
      <tr><td>状态</td><td id="m_status">未开始</td></tr>
      <tr><td>当前距离（米）</td><td id="m_live_dist">-</td></tr>
      <tr><td>步数</td><td id="m_steps">-</td></tr>
      <tr><td>步频 (步/分)</td><td id="m_cadence">-</td></tr>
      <tr><td>左/右膝平均角</td><td id="m_knee">-</td></tr>
      <tr><td>左/右膝活动范围</td><td id="m_krange">-</td></tr>
      <tr><td>躯干平均/波动角</td><td id="m_trunk">-</td></tr>
      <tr><td>膝对称性</td><td id="m_sym">-</td></tr>
      <tr><td>站位距离（米，深度）</td><td id="m_dist">-</td></tr>
      <tr><td>步长（米，深度估算）</td><td id="m_step_m">-</td></tr>
      <tr><td>步长（归一化）</td><td id="m_step">-</td></tr>
      <tr><td>重心摆动（归一化）</td><td id="m_sway">-</td></tr>
    </table>
    <h3>健康报告</h3>
    <div id="risk">风险等级：-</div>
    <div id="report">还没有报告。</div>
    <h3>健康建议</h3>
    <ul id="tips"><li>采集后自动生成。</li></ul>
  </div>
  <div class="card">
    <strong>采集结果</strong>
    <div id="results"><small>还没有结果</small></div>
    <img id="pose" style="display:none">
    <img id="gait" style="display:none">
  </div>
</div>
<div class="card" style="margin-top:16px">
  <h3 style="margin-top:0">历史记录（已保存到 SQLite）</h3>
  <table class="hist"><thead><tr><td>时间</td><td>风险</td><td>步数</td><td>步频</td><td>对称性</td><td></td></tr></thead><tbody id="history"></tbody></table>
</div>
<script>
async function prepare(){
  await fetch('/api/prepare',{method:'POST'});
  document.getElementById('msg').textContent='机器人已停稳，请站到相机前。';
}
async function startCapture(){
  await fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({duration:10})});
  document.getElementById('msg').textContent='采集中，请自然走几步...';
  poll();
}
async function stopCapture(){ await fetch('/api/stop',{method:'POST'}); }
async function aim(){
  const r=await fetch('/api/aim',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target:2.6})});
  const j=await r.json();
  if(j.error) document.getElementById('track_msg').textContent=j.error;
  else poll();
}
let tracking=false;
async function toggleTrack(){
  tracking=!tracking;
  await fetch('/api/track',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({on:tracking})});
  const b=document.getElementById('btn_track');
  b.textContent='头部跟随：'+(tracking?'开':'关');
  b.classList.toggle('on',tracking);
}
function set(id,v){ document.getElementById(id).textContent=v; }
let lastSession=null;
function renderLatest(l){
  if(!l || !l.metrics) return;
  const m=l.metrics;
  set('m_status','完成 '+l.ts);
  set('m_steps',m.step_count);
  set('m_cadence',m.cadence_spm);
  set('m_knee',m.knee_left_mean_deg+' / '+m.knee_right_mean_deg+' °');
  set('m_krange',m.knee_left_range_deg+' / '+m.knee_right_range_deg+' °');
  set('m_trunk',m.trunk_mean_deg+' / '+m.trunk_std_deg+' °');
  set('m_sym',m.knee_symmetry_pct+' %');
  set('m_dist', (m.distance_m!==null && m.distance_m!==undefined) ? m.distance_m+' m' : '无深度');
  set('m_step_m', (m.step_length_m!==null && m.step_length_m!==undefined) ? m.step_length_m+' m' : '无深度');
  set('m_step',m.step_length_norm);
  set('m_sway',m.sway_norm);
  if(l.risk) document.getElementById('risk').textContent='风险等级：'+l.risk;
  if(l.report) document.getElementById('report').textContent=l.report;
  if(l.tips){
    const ul=document.getElementById('tips'); ul.innerHTML='';
    for(const t of l.tips){ const li=document.createElement('li'); li.textContent=t; ul.appendChild(li); }
  }
  if(l.pose){ const p=document.getElementById('pose'); p.src='/files/'+l.pose+'?t='+Date.now(); p.style.display='block'; }
  if(l.gait){ const g=document.getElementById('gait'); g.src='/files/'+l.gait+'?t='+Date.now(); g.style.display='block'; }
  document.getElementById('results').innerHTML='<small>session: '+l.session_id+'<br>pose: '+l.pose+'<br>gait: '+l.gait+'<br>json: '+l.json+'</small>';
  if(l.session_id!==lastSession){ lastSession=l.session_id; loadHistory(); }
}
async function loadHistory(){
  try{
    const r=await fetch('/api/history'); const j=await r.json();
    const tb=document.getElementById('history'); tb.innerHTML='';
    for(const s of j.sessions){
      const tr=document.createElement('tr');
      tr.innerHTML='<td>'+s.ts+'</td><td>'+s.risk+'</td><td>'+s.metrics.step_count+'</td><td>'+s.metrics.cadence_spm+'</td><td>'+s.metrics.knee_symmetry_pct+'%</td><td><button onclick="loadSession('+s.session_id+')">查看</button></td>';
      tb.appendChild(tr);
    }
  }catch(e){}
}
async function loadSession(id){
  try{
    const r=await fetch('/api/session/'+id); const j=await r.json();
    if(j.latest) renderLatest(j.latest);
  }catch(e){}
}
let pollTimer=null;
async function poll(){
  try{
    const r=await fetch('/api/status'); const j=await r.json();
    document.getElementById('fill').style.width=Math.round((j.progress||0)*100)+'%';
    if(j.error) document.getElementById('msg').textContent=j.error;
    if(j.track_message) document.getElementById('track_msg').textContent=j.track_message;
    if(j.distance_m!==null && j.distance_m!==undefined)
      document.getElementById('m_live_dist').textContent=j.distance_m+' m';
    tracking=!!j.track;
    const b=document.getElementById('btn_track');
    b.textContent='头部跟随：'+(tracking?'开':'关');
    b.classList.toggle('on',tracking);
    if(j.latest) renderLatest(j.latest);
  }catch(e){}
  clearTimeout(pollTimer);
  pollTimer=setTimeout(poll, 1200);
}
poll(); loadHistory();
</script></body></html>
"""


def main():
    global LANDMARKER, TARGET_DISTANCE
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8084)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--source", default=DEFAULT_SOURCE)
    ap.add_argument("--duration", type=float, default=10.0)
    ap.add_argument("--track", action="store_true",
                    help="start with head tracking enabled")
    ap.add_argument("--target-distance", type=float, default=2.6,
                    help="auto-framing distance in metres")
    args = ap.parse_args()
    STATE["source"] = args.source
    STATE["duration"] = args.duration
    STATE["track"] = bool(args.track)
    TARGET_DISTANCE = args.target_distance
    init_db()
    load_latest_result()
    LANDMARKER = make_landmarker()
    threading.Thread(target=idle_track_loop, daemon=True).start()
    print(f"health station on http://{args.host}:{args.port}", flush=True)
    print(f"source: {args.source}; track={STATE['track']}; "
          f"target={TARGET_DISTANCE} m", flush=True)
    print(f"source: {args.source}", flush=True)
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
