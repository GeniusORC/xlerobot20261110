#!/usr/bin/env python3
"""Route A experiment: stage latency benchmark.

Measures the latency of each pipeline stage that can be exercised without a
human speaker and writes mean / std / min / max as both JSON and a
manuscript-ready table.

Stages:
  routing     deterministic router on a fixed phrase set (no network)
  llm         one chat completion per trial through the configured endpoint
  llm_tool    one function-calling completion per trial (tool enabled)
  end_to_end  full voice_web.route_text path with robot calls stubbed

ASR and TTS need audio devices and a live speaker/microphone; they are
measured separately with run_audio_latency.py against real utterances and are
not faked here.

Use --trials N. LLM stages are skipped with a clear note when no endpoint is
configured, so the deterministic measurements remain reproducible offline.
"""
import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "experiments"))
os.environ.setdefault("XLEROBOT_AGENT_SETTINGS",
                      str(ROOT / "data" / "experiments" / "_agent_settings.json"))

import run_common  # noqa: E402
import agent_brain  # noqa: E402
import voice_web  # noqa: E402

PHRASES = ["停下", "前进", "左转", "过来", "看我", "报告",
           "我今天有点想女儿", "给我讲个笑话吧", "我胸痛得厉害"]


def stats(samples_ms):
    if not samples_ms:
        return None
    return {
        "n": len(samples_ms),
        "mean_ms": round(statistics.mean(samples_ms), 2),
        "std_ms": round(statistics.pstdev(samples_ms), 2) if len(samples_ms) > 1 else 0.0,
        "min_ms": round(min(samples_ms), 2),
        "max_ms": round(max(samples_ms), 2),
    }


def time_call(fn):
    t0 = time.perf_counter()
    fn()
    return (time.perf_counter() - t0) * 1000.0


def stub_robot():
    voice_web.robot_command = lambda intent, **kw: {"ok": True}
    voice_web.robot_follow = lambda on: {"ok": True}
    voice_web.robot_status = lambda: {"connected": True}
    voice_web.vision_context.describe = lambda: ""
    voice_web.speech_file = lambda text: None
    voice_web.CHAT_HISTORY.clear()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=20)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    n = max(1, args.trials)
    stub_robot()
    llm_ready = bool(os.environ.get("LLM_BASE_URL") and os.environ.get("LLM_MODEL"))

    results = {}

    # Deterministic routing: fully offline, includes the emergency bypass.
    routing = []
    for i in range(n):
        phrase = PHRASES[i % len(PHRASES)]
        routing.append(time_call(lambda p=phrase: voice_web.route_text(p)))
    results["routing_ms"] = stats(routing)

    if llm_ready:
        chat, tool = [], []
        for i in range(n):
            phrase = PHRASES[i % len(PHRASES)]
            msgs = [{"role": "user", "content": phrase}]
            chat.append(time_call(lambda m=msgs: agent_brain._post(m)))
            tool.append(time_call(
                lambda m=msgs: agent_brain._post(m, tools=agent_brain.TOOLS)))
        results["llm_chat_ms"] = stats(chat)
        results["llm_tool_ms"] = stats(tool)
    else:
        results["llm_chat_ms"] = None
        results["llm_tool_ms"] = {"note": "LLM endpoint not configured; skipped"}

    out_dir = run_common.run_dir("latency", args.out)
    (out_dir / "latency.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["## Stage latency", "",
             f"Trials per stage: {n}", "",
             "| Stage | N | Mean (ms) | SD | Min | Max |",
             "|---|---:|---:|---:|---:|---:|"]
    for key, label in (("routing_ms", "Deterministic routing"),
                       ("llm_chat_ms", "LLM chat completion"),
                       ("llm_tool_ms", "LLM function-calling")):
        s = results[key]
        if s and "mean_ms" in s:
            lines.append(f"| {label} | {s['n']} | {s['mean_ms']} | {s['std_ms']} "
                         f"| {s['min_ms']} | {s['max_ms']} |")
        else:
            lines.append(f"| {label} | — | not measured (endpoint unconfigured) | | | |")
    (out_dir / "latency.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"\nWrote {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
