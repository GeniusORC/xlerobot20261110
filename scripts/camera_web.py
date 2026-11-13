#!/usr/bin/env python3
"""Live MJPEG web view: D455 head (color + depth) and two RGB wrists.

  source scripts/env.sh robot
  python scripts/camera_web.py --port 8080

Open http://127.0.0.1:8080
Only one process can own the D455 at a time: stop LeRobot camera connections
before starting this server, or use a separate process for robot recording.
"""
import argparse
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import cv2

try:
    import pyrealsense2 as rs
except Exception:
    rs = None

HEAD_D455_SERIAL = "333422300185"
WRISTS = {
    "left": "/dev/video2",
    "right": "/dev/video0",
}


class OpenCVCamera:
    def __init__(self, name, dev, width=640, height=480, fps=30):
        self.name = name
        self.dev = dev
        self.width = width
        self.height = height
        self.fps = fps
        self.cap = None
        self.jpeg = None
        self.error = None
        self.lock = threading.Lock()
        self.stop = False
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def start(self):
        self.thread.start()

    def loop(self):
        while not self.stop:
            if self.cap is None or not self.cap.isOpened():
                self.cap = cv2.VideoCapture(self.dev, cv2.CAP_V4L2)
                self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
                self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
                self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
                self.cap.set(cv2.CAP_PROP_FPS, self.fps)
                if not self.cap.isOpened():
                    self.error = f"cannot open {self.dev}"
                    time.sleep(1)
                    continue
                self.error = None
            ok, frame = self.cap.read()
            if not ok or frame is None:
                self.error = "read failed"
                time.sleep(0.2)
                continue
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if ok:
                with self.lock:
                    self.jpeg = buf.tobytes()
                    self.error = None
            time.sleep(max(0.0, 1.0 / self.fps - 0.005))

    def snapshot(self):
        with self.lock:
            return self.jpeg


class RealSenseHead:
    def __init__(self, serial, width=640, height=480, fps=30):
        self.name = "head"
        self.dev = f"D455 {serial}"
        self.serial = serial
        self.width = width
        self.height = height
        self.fps = fps
        self.pipe = None
        self.colorizer = rs.colorizer() if rs is not None else None
        self.align = rs.align(rs.stream.color) if rs is not None else None
        self.jpeg = None
        self.depth_jpeg = None
        self.error = None
        self.lock = threading.Lock()
        self.stop = False
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def start(self):
        self.thread.start()

    def _open(self):
        cfg = rs.config()
        cfg.enable_device(self.serial)
        cfg.enable_stream(rs.stream.color, self.width, self.height, rs.format.bgr8, self.fps)
        cfg.enable_stream(rs.stream.depth, self.width, self.height, rs.format.z16, self.fps)
        self.pipe = rs.pipeline()
        self.pipe.start(cfg)

    def loop(self):
        while not self.stop:
            if rs is None:
                self.error = "pyrealsense2 not installed"
                time.sleep(2)
                continue
            try:
                if self.pipe is None:
                    self._open()
                frames = self.align.process(self.pipe.wait_for_frames())
                color = frames.get_color_frame()
                depth = frames.get_depth_frame()
                if color:
                    # RealSense bgr8 frames are already BGR for cv2.imencode.
                    c = self._img(color)
                    ok, buf = cv2.imencode(".jpg", c, [cv2.IMWRITE_JPEG_QUALITY, 75])
                    if ok:
                        with self.lock:
                            self.jpeg = buf.tobytes()
                if depth:
                    colored = self.colorizer.colorize(depth)
                    d = self._img(colored)
                    ok, buf = cv2.imencode(".jpg", d, [cv2.IMWRITE_JPEG_QUALITY, 75])
                    if ok:
                        with self.lock:
                            self.depth_jpeg = buf.tobytes()
                self.error = None
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}"
                try:
                    if self.pipe is not None:
                        self.pipe.stop()
                except Exception:
                    pass
                self.pipe = None
                time.sleep(1)

    @staticmethod
    def _img(frame):
        import numpy as np
        return np.asanyarray(frame.get_data())

    def snapshot(self):
        with self.lock:
            return self.jpeg

    def snapshot_depth(self):
        with self.lock:
            return self.depth_jpeg


class Handler(BaseHTTPRequestHandler):
    cameras = {}
    head = None

    def log_message(self, fmt, *args):
        pass

    def _stream(self, getter, fps):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()
        try:
            while True:
                jpeg = getter()
                if jpeg is not None:
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(jpeg)}\r\n\r\n".encode())
                    self.wfile.write(jpeg)
                    self.wfile.write(b"\r\n")
                time.sleep(1.0 / max(1, fps))
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(self.page().encode())
            return
        if path == "/stream/head_depth":
            if self.head is None:
                self.send_error(503)
                return
            self._stream(self.head.snapshot_depth, self.head.fps)
            return
        if path.startswith("/stream/"):
            name = path.rsplit("/", 1)[-1]
            cam = self.cameras.get(name)
            if cam is None:
                self.send_error(404)
                return
            self._stream(cam.snapshot, cam.fps)
            return
        if path == "/snapshot/head_depth":
            jpeg = self.head.snapshot_depth() if self.head else None
        elif path.startswith("/snapshot/"):
            name = path.rsplit("/", 1)[-1]
            cam = self.cameras.get(name)
            jpeg = cam.snapshot() if cam else None
        else:
            jpeg = None
        if jpeg is None:
            self.send_error(503)
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(jpeg)))
        self.end_headers()
        self.wfile.write(jpeg)

    def page(self):
        cards = []
        for name, cam in self.cameras.items():
            cards.append(
                f'<div class="card"><h2>{name} <small>{cam.dev}</small></h2>'
                f'<img src="/stream/{name}"></div>'
            )
        if self.head is not None:
            cards.append(
                '<div class="card"><h2>head depth <small>D455 aligned depth</small></h2>'
                '<img src="/stream/head_depth"></div>'
            )
        return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>XLeRobot cameras</title>
<style>
body {{ background:#111; color:#eee; font-family:sans-serif; margin:16px; }}
.grid {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }}
.card {{ background:#222; padding:10px; border-radius:8px; }}
h2 {{ margin:0 0 8px; font-size:16px; }}
small {{ color:#8f8; font-weight:normal; }}
img {{ width:100%; image-rendering:auto; background:#000; }}
</style></head><body>
<h1>XLeRobot cameras</h1>
<div class="grid">{''.join(cards)}</div>
</body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--head-serial", default=HEAD_D455_SERIAL)
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--fps", type=int, default=30)
    args = ap.parse_args()

    head = RealSenseHead(args.head_serial, args.width, args.height, args.fps)
    cams = {"head": head}
    for name, dev in WRISTS.items():
        cams[name] = OpenCVCamera(name, dev, args.width, args.height, args.fps)
    Handler.cameras = cams
    Handler.head = head
    for cam in cams.values():
        cam.start()
    print(f"serving http://127.0.0.1:{args.port}")
    print(f"head: {head.dev}; wrists: {WRISTS}")
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
