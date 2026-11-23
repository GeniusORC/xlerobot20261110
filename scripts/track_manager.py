#!/usr/bin/env python3
"""Runtime manager for the face/head tracker (``head_track.py``).

The tracking logic already lives in ``head_track.py``: a Haar face cascade on
the D455 head camera closes a pixel loop on the head pan/tilt servos, sweeps to
re-acquire a lost face, and (with ``--follow``) lets the base trail the person
under the safety limits in ``robot_api``.

This module adds the one missing piece — a way to turn that process on and off
at runtime without relaunching the whole supervised stack:

* ``start(mode)`` spawns ``head_track.py`` in its own session, records the PID
  under ``data/run/`` and tees output to ``data/logs/head_track.log``;
* ``stop()`` terminates it with SIGTERM (the tracker traps SIGTERM and restores
  the head to its launch pose in its ``finally`` block);
* ``status()`` reports whether it is alive and which mode it is in, and
  reconciles a stale PID file left by a killed process.

Modes:
  ``head``   head pan/tilt follows the face, the base never moves.
  ``follow`` head tracking plus base follow at the configured standoff.

Only one tracker may run at a time. Switching mode stops the current process
and starts the new one. The manager never sends wheel commands itself; base
motion stays entirely inside head_track/robot_api and remains revocable by any
voice "停下".
"""
import fcntl
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from config_loader import CFG, project_path

ROOT = Path(__file__).resolve().parent.parent
RUN_DIR = project_path("data/run")
LOG_DIR = project_path(CFG["logs"]["dir"])
PID_FILE = RUN_DIR / "head_track.pid"
MODE_FILE = RUN_DIR / "head_track.mode"
LOCK_FILE = RUN_DIR / "head_track.lock"
LOG_FILE = LOG_DIR / "head_track.log"

MODES = ("head", "follow")
# Give the tracker time to restore the head pose on shutdown before SIGKILL.
_STOP_GRACE_S = 6.0


@contextmanager
def _file_lock():
    """Serialize start/stop across threads *and* processes.

    Flask runs threaded, so two rapid POST /api/track calls (or a web click
    racing a voice command) can enter start() together. Each caller opens its
    own file descriptor, so this advisory flock genuinely contends (a shared
    fd would be re-entrant) and makes the check-then-spawn critical section
    atomic. The lock file lives under gitignored data/run.
    """
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    fh = LOCK_FILE.open("a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()


def _read_pid():
    try:
        return int(PID_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def _alive(pid):
    """True only when ``pid`` belongs to a live process we can signal."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _is_our_tracker(pid):
    """Guard against a recycled PID being reused by an unrelated process."""
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return b"head_track.py" in cmd


def _clear_state():
    PID_FILE.unlink(missing_ok=True)
    MODE_FILE.unlink(missing_ok=True)


def status():
    """Reconcile the PID file and return the current tracker state."""
    pid = _read_pid()
    if pid is not None and not (_alive(pid) and _is_our_tracker(pid)):
        # Process died (crash or external kill) without cleaning up.
        _clear_state()
        pid = None
    mode = None
    if pid is not None:
        try:
            mode = MODE_FILE.read_text().strip() or "head"
        except FileNotFoundError:
            mode = "head"
    return {
        "running": pid is not None,
        "mode": mode,
        "pid": pid,
        "log": str(LOG_FILE.relative_to(ROOT)) if LOG_FILE.exists() else None,
    }


def start(mode="head", standoff=None):
    """Start the tracker in ``head`` or ``follow`` mode.

    Returns ``(ok, payload)``. Switching mode restarts the process; asking for
    the already-running mode is a no-op success. Serialized by an advisory
    file lock so concurrent requests can never spawn two trackers.
    """
    if mode not in MODES:
        return False, {"error": f"mode must be one of {MODES}"}
    with _file_lock():
        return _start_locked(mode, standoff)


def _start_locked(mode, standoff):
    current = status()
    if current["running"]:
        if current["mode"] == mode:
            return True, {"already": True, **current}
        _stop_locked()  # restart in the new mode (lock already held)

    if not _robot_reachable():
        return False, {"error": "robot_api not connected; head servos unavailable"}

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    cmd = [sys.executable, str(ROOT / "scripts" / "head_track.py")]
    if mode == "follow":
        cmd += ["--follow", "--standoff",
                str(standoff or CFG["robot"]["follow_standoff_m"])]

    log_fh = LOG_FILE.open("a", encoding="utf-8")
    log_fh.write(f"\n# tracker start mode={mode} at "
                 f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    log_fh.flush()
    # New session + own process group so demo_manager stop does not reap it as
    # a child, and so we can signal the whole group if needed.
    proc = subprocess.Popen(
        cmd, cwd=str(ROOT), stdin=subprocess.DEVNULL,
        stdout=log_fh, stderr=subprocess.STDOUT,
        start_new_session=True, env=dict(os.environ, PYTHONUNBUFFERED="1"))

    # Record ownership immediately, BEFORE the liveness wait: holding the lock
    # means no other caller can spawn, and the early PID means status() never
    # reports a tracker that is actually running as "absent".
    PID_FILE.write_text(str(proc.pid))
    MODE_FILE.write_text(mode)

    # Wait briefly so an immediate crash (bad import, camera busy) is reported
    # instead of claiming success for a process that already exited.
    time.sleep(1.2)
    if proc.poll() is not None:
        _clear_state()
        return False, {"error": f"head_track exited immediately (code "
                                f"{proc.returncode}); see {LOG_FILE}"}

    return True, {"running": True, "mode": mode, "pid": proc.pid,
                  "log": str(LOG_FILE.relative_to(ROOT))}


def stop():
    """Stop the tracker and wait for its head-restore cleanup to finish."""
    with _file_lock():
        return _stop_locked()


def _stop_locked():
    current = status()
    pid = current.get("pid")
    if not pid:
        return True, {"running": False, "already": True}

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _clear_state()
        return True, {"running": False}

    deadline = time.monotonic() + _STOP_GRACE_S
    while time.monotonic() < deadline:
        if not _alive(pid):
            break
        time.sleep(0.15)
    if _alive(pid):
        # Should not happen: SIGTERM is trapped, but guarantee a clean stop.
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    _clear_state()
    return True, {"running": False, "stopped_pid": pid}


def _robot_reachable():
    """The head servos are only usable when robot_api owns a connected robot."""
    import requests
    from config_loader import service_url
    try:
        r = requests.get(service_url("robot_api") + "/status", timeout=2.0)
        r.raise_for_status()
        return bool(r.json().get("connected"))
    except Exception:
        return False
