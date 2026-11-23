#!/usr/bin/env python3
"""Wait for service readiness, returning failure for a degraded startup."""
import sys
import time
import requests
from config_loader import service_url


def ready(name, endpoint):
    try:
        response = requests.get(service_url(name) + endpoint, timeout=1)
        response.raise_for_status()
        if name == "camera_web" and response.json().get("ok") is not True:
            return False
        return True
    except (requests.RequestException, ValueError):
        return False


def main():
    pending = dict(robot_api="/status", camera_web="/health", voice_web="/",
                   health_web="/", face_web="/state")
    deadline = time.monotonic() + 60
    while pending and time.monotonic() < deadline:
        for name, endpoint in list(pending.items()):
            if ready(name, endpoint):
                print(f"{name}: ready ({service_url(name)})", flush=True)
                del pending[name]
        if pending:
            time.sleep(0.5)
    if pending:
        print("Startup degraded: " + ", ".join(pending) + "; inspect data/logs/", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
