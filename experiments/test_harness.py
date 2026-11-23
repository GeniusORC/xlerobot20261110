#!/usr/bin/env python3
"""Regression tests for the Route A safety guards and run-directory logic.

Run with:  .venv/bin/python experiments/test_harness.py

These are offline unit tests: no robot, no network, no LLM endpoint. They lock
the two behaviors a reviewer would most want to see verified:

  1. deterministic narration/metaphor text never reaches a robot tool, while
     short imperative commands still do;
  2. every harness in one invocation writes into the same run directory.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "experiments"))
os.environ.setdefault("XLEROBOT_AGENT_SETTINGS",
                      str(ROOT / "data" / "experiments" / "_test_settings.json"))

import agent_brain  # noqa: E402
import voice_web  # noqa: E402


def check(label, got, want):
    ok = got == want
    print(f"{'ok ' if ok else 'FAIL'} {label}: got={got!r} want={want!r}")
    return ok


def test_stop_and_intent():
    good = []
    # Short imperative stops still halt.
    for t in ("停下", "别动", "停止", "不要动", "停住"):
        good.append(check(f"stop {t}", voice_web.map_intent(t), "stop"))
    # Metaphorical / narrative stop must not.
    for t in ("你能停下来听我说吗", "他想停下来歇会儿再走"):
        good.append(check(f"no-stop {t}", voice_web.map_intent(t), "unknown"))
    return all(good)


def test_narration_guard():
    good = []
    # Narration containing motion words is not a direct command.
    for t in ("我孙子跟着我去公园了", "我昨天后退了一步，吓了一跳",
              "左转的时候要小心车", "看到你过来我就开心",
              "你能停下来听我说吗"):
        good.append(check(f"narration {t}", agent_brain.is_direct_command(t), False))
    # Real short commands are direct commands.
    for t in ("前进", "后退", "左转", "过来", "看我", "跟着我", "停下"):
        good.append(check(f"command {t}", agent_brain.is_direct_command(t), True))
    return all(good)


def test_run_dir_sharing():
    import run_common
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XLEROBOT_EXPERIMENT_ROOT"] = tmp
        os.environ["XLEROBOT_RUN_ID"] = "20990101_000000"
        a = run_common.run_dir("llm_eval")
        b = run_common.run_dir("latency")
        ok = check("shared run id", a.parent == b.parent, True)
        ok &= check("run id value", a.parent.name, "20990101_000000")
        # --out still wins.
        custom = run_common.run_dir("gait_check", out=Path(tmp) / "custom")
        ok &= check("explicit --out", custom.name, "custom")
        ok &= check("latest_run", run_common.latest_run().name, "20990101_000000")
        return ok


def _stub_tracker():
    """Replace tracker side effects with an in-memory call log (no subprocess)."""
    calls = []

    def fake_start(mode, standoff=None):
        if mode not in voice_web.track_manager.MODES:
            return False, {"error": "bad mode"}
        calls.append(("start", mode))
        return True, {"running": True, "mode": mode, "pid": 4242}

    def fake_stop():
        calls.append(("stop", None))
        return True, {"running": False, "stopped_pid": 4242}

    voice_web.track_manager.start = fake_start
    voice_web.track_manager.stop = fake_stop
    voice_web.robot_follow = lambda on: {"ok": True, "follow": on}
    voice_web.handle_intent = lambda intent, text="": {"intent": intent, "reply": "ok"}
    voice_web.speech_file = lambda t: None
    voice_web._push_history = lambda *a, **k: None
    return calls


def test_head_track_routing():
    calls = _stub_tracker()
    good = []

    # Head-only phrases start the tracker in head mode and never touch base.
    for t in ("看着我", "盯着我", "看着我的脸"):
        calls.clear()
        out = voice_web.route_text(t)
        good.append(check(f"head intent {t}", out.get("intent"), "head_track"))
        good.append(check(f"head engine {t}", out.get("engine"), "keyword"))
        good.append(check(f"head mode {t}", calls, [("start", "head")]))

    # Base follow phrases start the follower in follow mode.
    for t in ("跟着我", "跟我走"):
        calls.clear()
        out = voice_web.route_text(t)
        good.append(check(f"follow intent {t}", out.get("intent"), "follow_me"))
        good.append(check(f"follow mode {t}", ("start", "follow") in calls, True))

    # Off / stop phrases stop the tracker process, not just the base latch.
    for t in ("别跟了", "别看着我", "停下"):
        calls.clear()
        voice_web.route_text(t)
        good.append(check(f"stop tracker {t}", ("stop", None) in calls, True))

    # Narrative sentences containing the phrases must not start anything.
    for t in ("我孙子看着我去公园", "昨天我跟着我儿子买菜"):
        calls.clear()
        voice_web.route_text(t)
        good.append(check(f"no motion {t}", any(c[0] == "start" for c in calls), False))
    return all(good)


def test_track_manager_state():
    import track_manager
    good = []
    # Invalid modes are rejected.
    ok, payload = track_manager.start("bogus")
    good.append(check("reject bad mode", ok, False))
    good.append(check("bad mode error", "error" in payload, True))
    # A stale PID left by a dead process is reconciled to "not running".
    track_manager.RUN_DIR.mkdir(parents=True, exist_ok=True)
    track_manager.PID_FILE.write_text("999999")
    track_manager.MODE_FILE.write_text("head")
    st = track_manager.status()
    good.append(check("stale pid cleared", st["running"], False))
    good.append(check("stale pid value", st["pid"], None))
    good.append(check("pid file removed", track_manager.PID_FILE.exists(), False))
    # Stop with nothing running is a clean no-op.
    ok, payload = track_manager.stop()
    good.append(check("stop idle ok", ok, True))
    return all(good)


def main():
    results = [test_stop_and_intent(), test_narration_guard(),
               test_run_dir_sharing(), test_head_track_routing(),
               test_track_manager_state()]
    print("\nALL PASS" if all(results) else "\nFAILURES PRESENT")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
