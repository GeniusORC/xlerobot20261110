#!/usr/bin/env python3
"""Tiny client for scripts/face_display.py (port 8086).

Every call is best-effort: if the face is not running the robot still talks.
"""
import os

import requests
from config_loader import service_url

FACE_API = service_url("face_web")


def _post(path, payload, timeout=0.6):
    try:
        requests.post(f"{FACE_API}{path}", json=payload, timeout=timeout)
        return True
    except Exception:
        return False


def emotion(name, intensity=1.0, hold=0.0):
    """neutral happy surprised sad concerned listening thinking sleepy"""
    return _post("/emotion", {"emotion": name, "intensity": intensity,
                              "hold": hold})


def say(audio=None, duration=None, emotion=None):
    payload = {}
    if audio:
        payload["audio"] = audio
    if duration:
        payload["duration"] = duration
    if emotion:
        payload["emotion"] = emotion
    return _post("/say", payload)


def look(dx=0.0, dy=0.0):
    return _post("/look", {"dx": dx, "dy": dy})


def blink():
    return _post("/blink", {})


def state():
    try:
        r = requests.get(f"{FACE_API}/state", timeout=0.6)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None
