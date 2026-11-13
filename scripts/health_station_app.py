#!/usr/bin/env python3
"""Desktop GUI: fixed health station (RGB + MediaPipe 2D gait/posture).

This is the non-web version of the health station. It opens a Tk window with
live camera, pose capture, metrics, screenshots and gait curves.

  source scripts/env.sh voice
  python scripts/health_station_app.py

The head camera is used directly if available; otherwise the app falls back to
the camera_web snapshot URL (http://127.0.0.1:8080/snapshot/head).
"""
import argparse
import json
import os
import threading
import time
import traceback
from datetime import datetime

import cv2
import mediapipe as mp
import numpy as np
import requests
import tkinter as tk
from PIL import Image, ImageTk
from tkinter import ttk

import health_station_web as hs

HTTP_SNAPSHOT = "http://127.0.0.1:8080/snapshot/head"
VIDEO_W, VIDEO_H = 720, 540


def resolve_source(source: str) -> str:
    if source.startswith("realsense:"):
        return source
    if not source.startswith("/dev/"):
        return source
    cap = cv2.VideoCapture(source, cv2.CAP_V4L2)
    ok, frame = cap.read()
    cap.release()
    if ok and frame is not None:
        return source
    print(f"cannot open {source}; falling back to {HTTP_SNAPSHOT}", flush=True)
    return HTTP_SNAPSHOT


class Grabber:
    def __init__(self, source):
        self.source = hs.Source(source, depth=True)
        self.lock = threading.Lock()
        self.frame = None
        self.depth = None
        self.K = None
        self.scale = 0.001
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def start(self):
        self.thread.start()

    def loop(self):
        while not self.stop.is_set():
            f = self.source.read()
            if f is not None:
                d, K = self.source.read_depth()
                with self.lock:
                    self.frame = f
                    self.depth = d
                    self.K = K
                    self.scale = self.source.depth_scale
            time.sleep(1.0 / 15.0)

    def latest(self):
        with self.lock:
            return None if self.frame is None else self.frame.copy()

    def latest_with_depth(self):
        """Frame plus the depth map captured alongside it (may be None)."""
        with self.lock:
            if self.frame is None:
                return None, None, None, 0.001
            return (self.frame.copy(),
                    None if self.depth is None else self.depth.copy(),
                    self.K, self.scale)


class App:
    def __init__(self, root, args):
        self.root = root
        self.args = args
        self.source = resolve_source(args.source)
        self.grabber = Grabber(self.source)
        self.grabber.start()
        self.capture_running = False
        self.stop_event = threading.Event()
        self.latest = None
        self.photo = None
        self.follow_on = bool(getattr(args, "follow", False))
        self.follow_distance = float(getattr(args, "follow_distance", 1.0))

        root.title("XLeRobot 固定健康站")
        root.geometry("1220x780")
        root.configure(bg="#111")
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.build_ui()
        self.update_preview()
        self.set_status(f"相机: {self.source}")

    def build_ui(self):
        main = tk.Frame(self.root, bg="#111")
        main.pack(fill="both", expand=True, padx=12, pady=12)

        left = tk.Frame(main, bg="#111")
        left.pack(side="left", fill="both", expand=True)
        self.video = tk.Label(left, bg="#000", width=VIDEO_W, height=VIDEO_H)
        self.video.pack(fill="both", expand=True)
        self.progress = ttk.Progressbar(left, maximum=100, mode="determinate")
        self.progress.pack(fill="x", pady=(8, 0))
        self.status = tk.Label(left, text="", fg="#9c9", bg="#111",
                               font=("Sans", 11), anchor="w")
        self.status.pack(fill="x", pady=(6, 0))

        right = tk.Frame(main, bg="#1d1d1f", padx=16, pady=16)
        right.pack(side="right", fill="y", padx=(14, 0))

        title = tk.Label(right, text="固定健康站", fg="#eee", bg="#1d1d1f",
                         font=("Sans", 18, "bold"))
        title.pack(anchor="w")

        btns = tk.Frame(right, bg="#1d1d1f")
        btns.pack(fill="x", pady=(10, 6))
        self.btn_prepare = tk.Button(btns, text="准备（停车）", command=self.prepare,
                                     bg="#36c", fg="white", font=("Sans", 12),
                                     width=14)
        self.btn_prepare.pack(side="left", padx=(0, 6))
        self.btn_start = tk.Button(btns, text="开始采集 10 秒", command=self.start,
                                   bg="#2a8", fg="white", font=("Sans", 12),
                                   width=14)
        self.btn_start.pack(side="left", padx=(0, 6))
        self.btn_stop = tk.Button(btns, text="停止", command=self.stop,
                                  bg="#d33", fg="white", font=("Sans", 12),
                                  width=8)
        self.btn_stop.pack(side="left")

        follow = tk.Frame(right, bg="#1d1d1f")
        follow.pack(fill="x", pady=(0, 6))
        self.follow_var = tk.BooleanVar(value=self.follow_on)
        tk.Checkbutton(follow, text="采集时跟随", variable=self.follow_var,
                       command=self.on_follow_toggle, bg="#1d1d1f", fg="#eee",
                       selectcolor="#333", activebackground="#1d1d1f",
                       activeforeground="#fff",
                       font=("Sans", 11)).pack(side="left")
        tk.Label(follow, text="目标距离", fg="#aaa", bg="#1d1d1f",
                 font=("Sans", 10)).pack(side="left", padx=(12, 4))
        self.follow_dist_var = tk.StringVar(value=f"{self.follow_distance:.1f}")
        tk.Spinbox(follow, from_=0.5, to=3.0, increment=0.1, width=5,
                   textvariable=self.follow_dist_var,
                   font=("Sans", 10)).pack(side="left")
        tk.Label(follow, text="m", fg="#aaa", bg="#1d1d1f",
                 font=("Sans", 10)).pack(side="left", padx=(3, 0))
        self.follow_status = tk.Label(right, text="", fg="#9cf", bg="#1d1d1f",
                                      font=("Sans", 9), anchor="w",
                                      justify="left", wraplength=360)
        self.follow_status.pack(fill="x", pady=(0, 4))

        self.metrics = {}
        names = [
            ("frames", "有效帧"),
            ("step_count", "步数"),
            ("cadence_spm", "步频 (步/分)"),
            ("knee_left_mean_deg", "左膝平均角 (°)"),
            ("knee_right_mean_deg", "右膝平均角 (°)"),
            ("knee_left_range_deg", "左膝活动范围 (°)"),
            ("knee_right_range_deg", "右膝活动范围 (°)"),
            ("hip_left_mean_deg", "左髋平均角 (°)"),
            ("hip_right_mean_deg", "右髋平均角 (°)"),
            ("trunk_mean_deg", "躯干平均角 (°)"),
            ("trunk_std_deg", "躯干波动角 (°)"),
            ("knee_symmetry_pct", "膝对称性 (%)"),
            ("distance_m", "站位距离 (米·深度)"),
            ("step_length_m", "步长 (米·深度估算)"),
            ("step_length_norm", "步长 (归一化)"),
            ("sway_norm", "重心摆动 (归一化)"),
        ]
        table = tk.Frame(right, bg="#1d1d1f")
        table.pack(fill="x", pady=(10, 4))
        for i, (key, label) in enumerate(names):
            tk.Label(table, text=label, fg="#aaa", bg="#1d1d1f",
                     font=("Sans", 11), anchor="w").grid(row=i, column=0, sticky="w", pady=2)
            var = tk.StringVar(value="-")
            self.metrics[key] = var
            tk.Label(table, textvariable=var, fg="#eee", bg="#1d1d1f",
                     font=("Sans", 11), anchor="e").grid(row=i, column=1, sticky="e", padx=(20, 0), pady=2)

        self.result = tk.StringVar(value="还没有结果")
        tk.Label(right, textvariable=self.result, fg="#9c9", bg="#1d1d1f",
                 wraplength=360, justify="left", font=("Sans", 10),
                 anchor="w").pack(fill="x", pady=(10, 4))
        tk.Button(right, text="打开结果文件夹", command=self.open_folder,
                  bg="#444", fg="white", font=("Sans", 11)).pack(anchor="w", pady=(2, 0))
        tk.Label(right, text="提示：老人站在 2–3 米处，露出全身。\n"
                             "D455 深度用于真实步长（米）；无深度时退回归一化值。",
                 fg="#888", bg="#1d1d1f", font=("Sans", 9),
                 justify="left", anchor="w").pack(fill="x", pady=(10, 0))

    def set_status(self, text):
        self.status.config(text=text)

    def show_frame(self, frame):
        if frame is None:
            return
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(rgb)
        img = img.resize((VIDEO_W, VIDEO_H), Image.Resampling.BILINEAR)
        self.photo = ImageTk.PhotoImage(img)
        self.video.configure(image=self.photo)

    def update_preview(self):
        if not self.capture_running:
            f = self.grabber.latest()
            if f is not None:
                self.show_frame(f)
        self.root.after(60, self.update_preview)

    def stop_robot(self):
        try:
            requests.post(f"{self.args.robot_api}/command",
                          json={"intent": "stop"}, timeout=5)
            return True
        except Exception as e:
            print("robot stop failed:", e, flush=True)
            return False

    def prepare(self):
        ok = self.stop_robot()
        self.set_status("机器人已停稳" if ok else "机器人 API 未连接；仍可采集")

    def start(self):
        if self.capture_running:
            return
        self.capture_running = True
        self.stop_event.clear()
        self.progress["value"] = 0
        self.btn_start.config(state="disabled")
        self.btn_prepare.config(state="disabled")
        self.follow_on = bool(self.follow_var.get())
        try:
            self.follow_distance = float(self.follow_dist_var.get())
        except ValueError:
            self.follow_distance = 1.0
        self.follow_distance = min(3.0, max(0.5, self.follow_distance))
        if self.follow_on:
            self.set_status("采集中：底盘保持站位，请自然走几步...")
        else:
            self.set_status("采集中：请自然走几步，保持全身在画面内...")
        self.stop_robot()
        threading.Thread(target=self.capture_loop, daemon=True).start()

    def on_follow_toggle(self):
        self.follow_on = bool(self.follow_var.get())
        if not self.follow_on:
            try:
                hs.BaseFollower(self.args.robot_api).stop()
            except Exception:
                pass
            self.follow_status.config(text="采集时跟随：关")

    def stop(self):
        self.stop_event.set()
        self.set_status("正在停止...")
        self.stop_robot()

    def capture_loop(self):
        lm = None
        series = []
        last_overlay = None
        start = time.time()
        follower = None
        if self.follow_on:
            follower = hs.BaseFollower(self.args.robot_api)
            follower.reset()
            msg = f"跟随准备中（目标 {self.follow_distance:.1f} m）"
            self.root.after(0, lambda t=msg: self.follow_status.config(text=t))
        try:
            lm = hs.make_landmarker()
            while not self.stop_event.is_set():
                elapsed = time.time() - start
                if elapsed >= self.args.duration:
                    break
                dist_now = None
                frame, depth, K, scale = self.grabber.latest_with_depth()
                if frame is None:
                    time.sleep(0.05)
                    continue
                h, w = frame.shape[:2]
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                res = lm.detect_for_video(
                    mp_img, int(time.monotonic() * 1000))
                if res.pose_landmarks:
                    lms = res.pose_landmarks[0]
                    row = hs.extract(lms, w, h)
                    if row is not None:
                        row["t"] = round(elapsed, 4)
                        if depth is not None and K is not None:
                            hs.add_depth(row, depth, K, scale)
                            if np.isfinite(row.get("dist_m", np.nan)):
                                dist_now = float(row["dist_m"])
                                if (follower is not None
                                        and follower.target is None):
                                    dmin = hs.min_full_body_distance(
                                        row, dist_now, K[1], h)
                                    if dmin is None:
                                        msg = "还没量到身高，暂不跟随"
                                        self.root.after(
                                            0, lambda t=msg:
                                            self.follow_status.config(text=t))
                                    else:
                                        eff = max(self.follow_distance, dmin)
                                        follower.target = float(eff)
                                        if eff > self.follow_distance + 0.03:
                                            msg = (f"跟随 {eff:.1f} m："
                                                   f"{self.follow_distance:.1f} m "
                                                   f"时全身会出框，已自动拉远")
                                        else:
                                            msg = f"跟随 {eff:.1f} m"
                                        self.root.after(
                                            0, lambda t=msg:
                                            self.follow_status.config(text=t))
                        series.append(row)
                        last_overlay = hs.draw_overlay(frame, lms)
                        self.root.after(0, self.show_frame, last_overlay)
                else:
                    self.root.after(0, self.show_frame, frame)
                if follower is not None:
                    follower.update(dist_now)
                pct = min(100, int(elapsed / self.args.duration * 100))
                self.root.after(0, lambda p=pct: self.progress.configure(value=p))
                time.sleep(0.1)
            elapsed = time.time() - start
            if not series:
                self.root.after(0, self.finish_error,
                                "没有检测到完整人体；请让老人站到画面中并露出全身")
                return
            metrics = hs.analyze(series, min(elapsed, self.args.duration), 10)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            pose_path = os.path.join(hs.HEALTH_DIR, f"pose_{ts}.png")
            gait_path = os.path.join(hs.HEALTH_DIR, f"gait_{ts}.png")
            json_path = os.path.join(hs.HEALTH_DIR, f"metrics_{ts}.json")
            if last_overlay is not None:
                cv2.imwrite(pose_path, last_overlay)
            hs.plot_gait(series, 10, gait_path)
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(metrics, f, ensure_ascii=False, indent=2)
            report, tips, risk = hs.make_report(metrics)
            try:
                hs.init_db()
                hs.save_session(ts, metrics, report, tips, risk,
                                os.path.basename(pose_path)
                                if os.path.exists(pose_path) else None,
                                os.path.basename(gait_path),
                                os.path.basename(json_path))
            except Exception:
                traceback.print_exc()
            self.root.after(0, self.finish_ok, metrics,
                            pose_path if os.path.exists(pose_path) else "",
                            gait_path, json_path, report, risk)
        except Exception as e:
            traceback.print_exc()
            self.root.after(0, self.finish_error, f"{type(e).__name__}: {e}")
        finally:
            if follower is not None:
                follower.stop()
                self.root.after(0, lambda: self.follow_status.config(
                    text="采集结束，底盘已停"))
            if lm is not None:
                try:
                    lm.close()
                except Exception:
                    pass

    def finish_ok(self, metrics, pose, gait, json_path, report="", risk=""):
        self.latest = {"metrics": metrics, "pose": pose, "gait": gait,
                       "json": json_path, "report": report, "risk": risk}
        for k, var in self.metrics.items():
            v = metrics.get(k)
            var.set("-" if v is None else str(v))
        self.result.set(f"风险等级：{risk or '-'}\n{report}\n"
                        f"骨架图: {pose}\n曲线: {gait}\n指标: {json_path}")
        self.capture_running = False
        self.progress["value"] = 100
        self.btn_start.config(state="normal")
        self.btn_prepare.config(state="normal")
        self.set_status("采集完成")

    def finish_error(self, msg):
        self.capture_running = False
        self.btn_start.config(state="normal")
        self.btn_prepare.config(state="normal")
        self.set_status(msg)

    def open_folder(self):
        try:
            import subprocess
            subprocess.Popen(["xdg-open", hs.HEALTH_DIR])
        except Exception as e:
            self.set_status(f"打开文件夹失败: {e}")

    def on_close(self):
        self.stop_event.set()
        self.grabber.stop.set()
        try:
            self.stop_robot()
        except Exception:
            pass
        self.root.destroy()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="http://127.0.0.1:8080/snapshot/head")
    ap.add_argument("--robot-api", default=hs.ROBOT_API)
    ap.add_argument("--duration", type=float, default=10.0)
    ap.add_argument("--follow", action="store_true",
                    help="start with base following enabled")
    ap.add_argument("--follow-distance", type=float, default=1.0,
                    help="requested standoff while capturing (clamped so the "
                         "full body stays in frame)")
    args = ap.parse_args()
    hs.init_db()
    root = tk.Tk()
    App(root, args)
    root.mainloop()


if __name__ == "__main__":
    main()
