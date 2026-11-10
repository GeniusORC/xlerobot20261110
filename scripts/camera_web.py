#!/usr/bin/env python3
"""Live MJPEG web view of the three XLeRobot RGB cameras.

  source scripts/env.sh robot
  python scripts/camera_web.py --port 8080

Then open http://127.0.0.1:8080
"""
import argparse
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import cv2

CAMS = {
    "head": "/dev/video0",
    "left": "/dev/video6",
    "right": "/dev/video2",
}


class Camera:
    def __init__(self, name, dev, width, height, fps):
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


class Handler(BaseHTTPRequestHandler):
    cameras = {}

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(self.page().encode())
            return
        if path.startswith("/stream/"):
            name = path.rsplit("/", 1)[-1]
            cam = self.cameras.get(name)
            if cam is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            try:
                while True:
                    jpeg = cam.snapshot()
                    if jpeg is not None:
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n")
                        self.wfile.write(f"Content-Length: {len(jpeg)}\r\n\r\n".encode())
                        self.wfile.write(jpeg)
                        self.wfile.write(b"\r\n")
                    time.sleep(1.0 / max(1, cam.fps))
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if path.startswith("/snapshot/"):
            name = path.rsplit("/", 1)[-1]
            cam = self.cameras.get(name)
            jpeg = cam.snapshot() if cam else None
            if jpeg is None:
                self.send_error(503)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(jpeg)))
            self.end_headers()
            self.wfile.write(jpeg)
            return
        self.send_error(404)

    def page(self):
        cards = []
        for name, cam in self.cameras.items():
            cards.append(
                f'<div class="card"><h2>{name} <small>{cam.dev}</small></h2>'
                f'<img src="/stream/{name}"></div>'
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
<h1>XLeRobot RGB cameras</h1>
<div class="grid">{''.join(cards)}</div>
</body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--fps", type=int, default=30)
    args = ap.parse_args()

    cams = {
        name: Camera(name, dev, args.width, args.height, args.fps)
        for name, dev in CAMS.items()
    }
    Handler.cameras = cams
    for cam in cams.values():
        cam.start()
    print(f"serving http://127.0.0.1:{args.port}")
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
