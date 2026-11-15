#!/usr/bin/env python3
"""Turn the head camera into a one-line sentence the chat model can use.

The D455 is owned by ``camera_web.py`` (port 8080), so this only fetches
``/snapshot/head_npz``: colour + depth + intrinsics in one request (~25 ms).
A Haar cascade finds the face, the depth map gives a real distance in metres,
and the result is cached so a burst of turns costs one capture.

Two filters keep the cascade honest on this scene:

* metric face width — a real face is 0.10–0.30 m wide; the cascade happily
  fires on a bright patch of wall, which measures ~0.8 m at 1.6 m range;
* exposure — a face whose mean intensity is above 245 is a light source, not
  a person.

When no frontal face is found, a HOG people detector still reports presence
(so "有人背对着你" is distinguishable from "没人").
"""
import io
import os
import threading
import time

import cv2
import numpy as np
import requests

CAMERA = os.environ.get("CAMERA_API", "http://127.0.0.1:8080").rstrip("/")
CACHE_S = 1.2
FACE_W_M = (0.10, 0.30)   # plausible width of a human face

_LOCK = threading.Lock()
_CACHE = {"at": 0.0, "value": None}
_CASCADE = None
_PROFILE = None
_SMILE = None
_HOG = None


def _load_cascades():
    global _CASCADE, _PROFILE, _SMILE
    if _CASCADE is not None:
        return
    roots = ["/usr/share/opencv4/haarcascades",
             "/usr/local/share/opencv4/haarcascades",
             os.path.join(os.path.dirname(cv2.__file__), "data")]
    for root in roots:
        f = os.path.join(root, "haarcascade_frontalface_default.xml")
        if not os.path.exists(f):
            continue
        _CASCADE = cv2.CascadeClassifier(f)
        p = os.path.join(root, "haarcascade_profileface.xml")
        _PROFILE = cv2.CascadeClassifier(p) if os.path.exists(p) else None
        s = os.path.join(root, "haarcascade_smile.xml")
        _SMILE = cv2.CascadeClassifier(s) if os.path.exists(s) else None
        return


def _hog():
    global _HOG
    if _HOG is None:
        hog = cv2.HOGDescriptor()
        hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
        _HOG = hog
    return _HOG


def grab(timeout=1.5):
    """Return (bgr, depth_m, fx, cx) or None."""
    try:
        r = requests.get(f"{CAMERA}/snapshot/head_npz", timeout=timeout)
        r.raise_for_status()
        d = np.load(io.BytesIO(r.content))
        img = cv2.imdecode(d["color_jpeg"], cv2.IMREAD_COLOR)
        depth = d["depth"].astype(np.float32) * float(d["depth_scale"])
        return img, depth, float(d["fx"]), float(d["cx"])
    except Exception:
        return None


def _best_face(gray, cascade, fx):
    out = []
    for (x, y, w, h) in cascade.detectMultiScale(gray, 1.08, 8, minSize=(60, 60)):
        out.append((x, y, w, h))
    return out


def analyse_once():
    got = grab()
    if got is None:
        return None
    img, depth, fx, cx = got
    h, w = img.shape[:2]
    _load_cascades()
    if _CASCADE is None or _CASCADE.empty():
        return {"present": False, "error": "no cascade file",
                "width": w, "height": h}

    gray = cv2.equalizeHist(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
    boxes = _best_face(gray, _CASCADE, fx)
    if _PROFILE is not None and not _PROFILE.empty():
        boxes += list(_PROFILE.detectMultiScale(gray, 1.08, 8, minSize=(60, 60)))

    best = None
    for (x, y, fw, fh) in boxes:
        mx, my = int(fw * 0.25), int(fh * 0.25)
        roi = depth[y + my:y + fh - my, x + mx:x + fw - mx]
        roi = roi[np.isfinite(roi) & (roi > 0.05) & (roi < 8.0)]
        if not roi.size:
            continue
        dist = float(np.median(roi))
        width_m = fw * dist / fx
        bright = float(np.mean(gray[y:y + fh, x:x + fw]))
        if not (FACE_W_M[0] <= width_m <= FACE_W_M[1]):
            continue           # a wall patch, not a face
        if bright > 245:
            continue           # a light source, not a face
        cand = {"x": int(x), "y": int(y), "w": int(fw), "h": int(fh),
                "distance_m": round(dist, 2), "width_m": round(width_m, 2),
                "brightness": round(bright, 1)}
        if best is None or fw * fh > best["w"] * best["h"]:
            best = cand

    if best is not None:
        off = (best["x"] + best["w"] / 2 - w / 2) / (w / 2)
        side = "左侧" if off < -0.18 else "右侧" if off > 0.18 else "中间"
        res = {"present": True, "face": best, "offset": round(off, 2),
               "side": side, "width": w, "height": h}
        if _SMILE is not None and not _SMILE.empty():
            lower = gray[best["y"] + int(best["h"] * 0.55):best["y"] + best["h"],
                         best["x"]:best["x"] + best["w"]]
            res["smiling"] = bool(len(_SMILE.detectMultiScale(
                lower, 1.7, 22, minSize=(25, 15))))
        return res

    # No usable face: ask HOG whether a person is in frame at all.  Kept small
    # (256 px) because this runs on the same CPU as the ASR.
    small = cv2.resize(gray, (256, int(256 * h / w)))
    try:
        rects, weights = _hog().detectMultiScale(
            small, winStride=(16, 16), padding=(8, 8), scale=1.08)
        person = any(float(wt) > 0.3 for wt in np.ravel(weights)) if len(rects) else False
    except Exception:
        person = False
    return {"present": False, "person_no_face": bool(person),
            "width": w, "height": h}


def _merge(a, b):
    if a is None:
        return b
    if b is None:
        return a
    if a.get("present") and b.get("present"):
        return b
    # Require two frames to agree before claiming a face.
    if a.get("present") != b.get("present"):
        weaker = b if a.get("present") else a
        weaker = dict(weaker)
        weaker["present"] = False
        weaker.pop("face", None)
        return weaker
    return b


def state(force=False):
    now = time.time()
    with _LOCK:
        if not force and _CACHE["value"] is not None and now - _CACHE["at"] < CACHE_S:
            return _CACHE["value"]
        value = analyse_once()
        _CACHE.update(at=now, value=value)
    return value


def describe(force=False):
    """One sentence for the LLM, or None when the camera is unreachable."""
    s = state(force=force)
    if s is None:
        return None
    if s.get("error"):
        return "头部相机可用，但人脸检测器没有加载"
    if not s.get("present"):
        if s.get("person_no_face"):
            return "画面里有一个人，但背对或侧对着机器人，看不到正脸"
        return "画面里没有看到正脸"
    f = s["face"]
    parts = [f"看到一个人，距离约{f['distance_m']:.1f}米",
             f"在画面{s['side']}"]
    if s.get("smiling"):
        parts.append("好像在笑")
    if f["brightness"] < 60:
        parts.append("光线偏暗")
    return "，".join(parts)


if __name__ == "__main__":
    import json
    t = time.time()
    s = state(force=True)
    print(json.dumps(s, ensure_ascii=False))
    print(describe(force=False))
    print(f"{(time.time() - t) * 1000:.0f} ms")
