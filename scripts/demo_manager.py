#!/usr/bin/env python3
"""Single-owner demo supervisor with rotating output and bounded restart delay."""
import argparse
import fcntl
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from config_loader import CFG, project_path

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "data" / "run"
STOP = threading.Event()


def commands():
    robot = str(ROOT / ".venv-robot/bin/python")
    voice = str(ROOT / ".venv/bin/python")
    cmds = {
        "camera_web": [robot, "scripts/camera_web.py", "--port", "8080"],
        "robot_api": [robot, "scripts/robot_api.py", "--port", "8090"],
        "health_station_web": [voice, "scripts/health_station_web.py", "--port", "8084"],
        "voice_auto_web": [voice, "scripts/voice_auto_web.py", "--port", "8082"],
        "face_display": [voice, "scripts/face_display.py", "--monitor", "1"],
        "head_track": [voice, "scripts/head_track.py"],
    }
    if os.environ.get("FOLLOW", "0") == "1":
        cmds["head_track"].append("--follow")
    return cmds


def logger(name):
    log = logging.getLogger(name)
    log.setLevel(logging.INFO)
    log.propagate = False
    directory = project_path(os.environ.get("LOG", CFG["logs"]["dir"]))
    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(directory / f"{name}.log",
                                  maxBytes=CFG["logs"]["max_bytes"],
                                  backupCount=CFG["logs"]["backup_count"])
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    log.addHandler(handler)
    return log


def drain(proc, log):
    for line in iter(proc.stdout.readline, b""):
        log.info(line.decode("utf-8", errors="replace").rstrip())


def supervise():
    RUN.mkdir(parents=True, exist_ok=True)
    with open(RUN / "demo.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        pidfile = RUN / "demo.pid"
        pidfile.write_text(str(os.getpid()))
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: STOP.set())
        children, readers, retry_at, failures = {}, [], {}, {}
        log = logger("watchdog")
        cmds = commands()
        # Follow stays opt-in and is intentionally not restarted after failure.
        if os.environ.get("FOLLOW", "0") != "1" and "head_track" in cmds:
            cmds.pop("head_track")
        if not os.environ.get("DISPLAY"):
            cmds.pop("face_display")
        logs = {name: logger(name) for name in cmds}
        try:
            while not STOP.is_set():
                for name, command in cmds.items():
                    proc = children.get(name)
                    if proc and proc.poll() is not None:
                        log.warning("%s exited with code %s", name, proc.returncode)
                        del children[name]
                        # A clean exit from the face display means the operator
                        # clicked its close control; relaunching it would undo
                        # that.  A crash (non-zero) is still restarted.
                        operator_closed_face = name == "face_display" and proc.returncode == 0
                        if (name == "head_track" or operator_closed_face
                                or os.environ.get("WATCHDOG", "1") == "0"):
                            retry_at[name] = float("inf")
                            if operator_closed_face:
                                log.info("face_display closed by operator; not restarting")
                        else:
                            failures[name] = failures.get(name, 0) + 1
                            retry_at[name] = time.monotonic() + min(60, 5 * failures[name])
                    if (name not in children and (name != "head_track" or failures.get(name, 0) == 0)
                            and retry_at.get(name, 0) != float("inf")
                            and time.monotonic() >= retry_at.get(name, 0)):
                        env = dict(os.environ, PYTHONUNBUFFERED="1")
                        proc = subprocess.Popen(command, cwd=ROOT, env=env,
                                                stdin=subprocess.DEVNULL,
                                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                                start_new_session=True)
                        children[name] = proc
                        reader = threading.Thread(target=drain, args=(proc, logs[name]), daemon=True)
                        reader.start()
                        readers.append(reader)
                        log.info("started %s pid=%s", name, proc.pid)
                STOP.wait(0.5)
        finally:
            # Stop supervision before signalling any child; no child can respawn.
            for proc in children.values():
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGTERM)
            deadline = time.monotonic() + 8
            for proc in children.values():
                try:
                    proc.wait(timeout=max(0.01, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
            for reader in readers:
                reader.join(timeout=1)
            pidfile.unlink(missing_ok=True)
    return 0


def stop():
    pidfile = RUN / "demo.pid"
    try:
        pid = int(pidfile.read_text())
        # Guard against a stale PID being reused by an unrelated process.
        if b"demo_manager.py" not in Path(f"/proc/{pid}/cmdline").read_bytes():
            raise RuntimeError("supervisor PID does not belong to demo_manager")
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 12
        while pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        if pidfile.exists():
            raise RuntimeError("supervisor did not finish shutdown")
    except FileNotFoundError:
        return 0
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "stop"))
    args = parser.parse_args()
    sys.exit(supervise() if args.action == "run" else stop())
