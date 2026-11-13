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
}
LOCK = threading.Lock()
STOP = threading.Event()
LANDMARKER = None

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
    def __init__(self, source):
        self.source = source
        self.cap = None
        self.rs_pipe = None
        if source.startswith("realsense:"):
            import pyrealsense2 as rs
            serial = source.split(":", 1)[1].strip()
            cfg = rs.config()
            if serial:
                cfg.enable_device(serial)
            cfg.enable_stream(rs.stream.color, 640, 480, rs.format.bgr8, 30)
            self.rs_pipe = rs.pipeline()
            self.rs_pipe.start(cfg)

    def read(self):
        if self.rs_pipe is not None:
            try:
                frames = self.rs_pipe.wait_for_frames()
                color = frames.get_color_frame()
                if color:
                    return np.asanyarray(color.get_data())
            except Exception:
                return None
            return None
        if self.source.startswith("http://") or self.source.startswith("https://"):
            try:
                r = requests.get(self.source, timeout=2)
                r.raise_for_status()
                buf = np.frombuffer(r.content, np.uint8)
                return cv2.imdecode(buf, cv2.IMREAD_COLOR)
            except Exception:
                return None
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
    }


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
        return np.array([s[key] for s in series], dtype=float)
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
    return {
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


def plot_gait(series, fps, path):
    t = np.arange(len(series)) / max(fps, 1e-6)
    def a(k):
        return np.array([s[k] for s in series], dtype=float)
    fig, axs = plt.subplots(3, 1, figsize=(9, 8), sharex=True)
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
    axs[2].set_xlabel("time (s)")
    axs[2].legend(loc="best")
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def run_capture(source, duration, fps):
    global LANDMARKER
    with LOCK:
        STATE.update(running=True, progress=0.0, error=None)
    STOP.clear()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    src = Source(source)
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

    if steps < 2 or dur < 2.5:
        report = (
            "本次采集没有形成连续步行，可能是站立、转身或人体未完整入镜。"
            "这组数据不能用于步态风险判断，建议让老人自然走 5–10 步后重新采集。"
        )
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
    if step_norm < 0.8:
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
        f"观察结果：{summary}\n"
        "说明：这是基于 RGB 2D 姿态的筛查结果，不是医学诊断。"
    )
    tips = []
    if cad < 60:
        tips.append("每天分次慢走 10–20 分钟，先热身，量力而行。")
    if sym > 15 or abs(tm) > 10 or sway > 0.08:
        tips.append("可做扶墙单腿站立、重心转移等平衡练习，旁边一定要有人保护。")
    if kr < 10:
        tips.append("做温和的膝关节屈伸和股四头肌拉伸，避免久坐后突然起身。")
    if step_norm < 0.8:
        tips.append("步幅不要刻意迈大，先求稳，再逐步增加步幅。")
    tips.append("如出现膝髋疼痛、头晕、胸闷或走路突然不稳，请及时就医。")
    tips.append("建议每周在相同位置复测一次，观察趋势而不是单次结果。")
    return report, tips, risk


def save_session(ts, metrics, report, tips, risk, pose, gait, json_name):
    with sqlite3.connect(DB_PATH) as db:
        cur = db.execute(
            """INSERT OR REPLACE INTO sessions
               (ts, duration, frames, step_count, cadence, symmetry, risk,
                metrics, report, tips, pose, gait, json, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ts, metrics.get("duration_s"), metrics.get("frames"),
             metrics.get("step_count"), metrics.get("cadence_spm"),
             metrics.get("knee_symmetry_pct"), risk,
             json.dumps(metrics, ensure_ascii=False),
             report, json.dumps(tips, ensure_ascii=False),
             pose, gait, json_name, datetime.now().isoformat(timespec="seconds")),
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
<h1>固定健康站 <small>· 机器人停稳 + RGB + MediaPipe</small></h1>
<div class="grid">
  <div class="card">
    <button class="prep" onclick="prepare()">准备（停车）</button>
    <button class="start" onclick="startCapture()">开始采集 10 秒</button>
    <button class="stop" onclick="stopCapture()">停止</button>
    <div id="bar"><div id="fill"></div></div>
    <div id="msg">请让老人站在头部相机前，露出全身；距离 2–3 米。</div>
    <img id="live" src="http://127.0.0.1:8080/stream/head">
    <small>实时头部相机 /dev/video0</small>
    <table id="metrics">
      <tr><td>状态</td><td id="m_status">未开始</td></tr>
      <tr><td>步数</td><td id="m_steps">-</td></tr>
      <tr><td>步频 (步/分)</td><td id="m_cadence">-</td></tr>
      <tr><td>左/右膝平均角</td><td id="m_knee">-</td></tr>
      <tr><td>左/右膝活动范围</td><td id="m_krange">-</td></tr>
      <tr><td>躯干平均/波动角</td><td id="m_trunk">-</td></tr>
      <tr><td>膝对称性</td><td id="m_sym">-</td></tr>
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
async function poll(){
  try{
    const r=await fetch('/api/status'); const j=await r.json();
    document.getElementById('fill').style.width=Math.round((j.progress||0)*100)+'%';
    if(j.error) document.getElementById('msg').textContent=j.error;
    if(j.latest) renderLatest(j.latest);
    if(j.running) setTimeout(poll,700);
  }catch(e){}
}
poll(); loadHistory();
</script></body></html>
"""


def main():
    global LANDMARKER
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8084)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--source", default=DEFAULT_SOURCE)
    ap.add_argument("--duration", type=float, default=10.0)
    args = ap.parse_args()
    STATE["source"] = args.source
    STATE["duration"] = args.duration
    init_db()
    load_latest_result()
    LANDMARKER = make_landmarker()
    print(f"health station on http://{args.host}:{args.port}", flush=True)
    print(f"source: {args.source}", flush=True)
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
