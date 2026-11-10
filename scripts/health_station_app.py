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
        self.source = hs.Source(source)
        self.lock = threading.Lock()
        self.frame = None
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def start(self):
        self.thread.start()

    def loop(self):
        while not self.stop.is_set():
            f = self.source.read()
            if f is not None:
                with self.lock:
                    self.frame = f
            time.sleep(1.0 / 15.0)

    def latest(self):
        with self.lock:
            return None if self.frame is None else self.frame.copy()


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
                             "无深度相机，步长为归一化值。",
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
        self.set_status("采集中：请自然走几步，保持全身在画面内...")
        self.stop_robot()
        threading.Thread(target=self.capture_loop, daemon=True).start()

    def stop(self):
        self.stop_event.set()
        self.set_status("正在停止...")
        self.stop_robot()

    def capture_loop(self):
        lm = None
        series = []
        last_overlay = None
        start = time.time()
        try:
            lm = hs.make_landmarker()
            while not self.stop_event.is_set():
                elapsed = time.time() - start
                if elapsed >= self.args.duration:
                    break
                frame = self.grabber.latest()
                if frame is None:
                    time.sleep(0.05)
                    continue
                h, w = frame.shape[:2]
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                res = lm.detect_for_video(mp_img, int(elapsed * 1000) + 1)
                if res.pose_landmarks:
                    lms = res.pose_landmarks[0]
                    row = hs.extract(lms, w, h)
                    if row is not None:
                        series.append(row)
                        last_overlay = hs.draw_overlay(frame, lms)
                        self.root.after(0, self.show_frame, last_overlay)
                else:
                    self.root.after(0, self.show_frame, frame)
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
            self.root.after(0, self.finish_ok, metrics,
                            pose_path if os.path.exists(pose_path) else "",
                            gait_path, json_path)
        except Exception as e:
            traceback.print_exc()
            self.root.after(0, self.finish_error, f"{type(e).__name__}: {e}")
        finally:
            if lm is not None:
                try:
                    lm.close()
                except Exception:
                    pass

    def finish_ok(self, metrics, pose, gait, json_path):
        self.latest = {"metrics": metrics, "pose": pose, "gait": gait, "json": json_path}
        for k, var in self.metrics.items():
            var.set(str(metrics.get(k, "-")))
        self.result.set(f"骨架图: {pose}\n曲线: {gait}\n指标: {json_path}")
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
    ap.add_argument("--source", default="/dev/video0")
    ap.add_argument("--robot-api", default=hs.ROBOT_API)
    ap.add_argument("--duration", type=float, default=10.0)
    args = ap.parse_args()
    root = tk.Tk()
    App(root, args)
    root.mainloop()


if __name__ == "__main__":
    main()
