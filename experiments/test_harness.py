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


def main():
    results = [test_stop_and_intent(), test_narration_guard(), test_run_dir_sharing()]
    print("\nALL PASS" if all(results) else "\nFAILURES PRESENT")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
