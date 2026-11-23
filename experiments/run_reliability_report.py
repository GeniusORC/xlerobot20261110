#!/usr/bin/env python3
"""Route A experiment: service reliability from the rotating logs.

Parses data/logs to quantify operational behavior without re-running the
robot:

  - per-service start count and restart backoff events
  - face-display operator closes vs. crashes (clean exit code 0 vs. non-zero)
  - camera D455 reset attempts
  - motor-bus "Missing motor IDs" events
  - LLM connectivity failures recorded by agent_brain
  - log time span, used to express events per day

The output is a descriptive engineering-reliability report, not a claim of
certified availability; it depends on how long the logs actually cover.
"""
import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "experiments"))
import run_common  # noqa: E402

LOG_DIR = ROOT / "data" / "logs"

START_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) [\d:,]+ started (\w+) pid=")
EXIT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) [\d:,]+ (\w+) exited with code (-?\d+)")
CLOSE_RE = re.compile(r"closed by operator")
MISSING_RE = re.compile(r"Missing motor IDs")
RESET_RE = re.compile(r"(D455|depth).*reset|reset.*(D455|depth)", re.IGNORECASE)
LLM_FAIL_RE = re.compile(r"LLM (unavailable|.*failed)")
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} [\d:]+)")


def parse_dt(line):
    m = TS_RE.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    starts = defaultdict(int)
    clean_exit, crash_exit = defaultdict(int), defaultdict(int)
    operator_closes = 0
    missing_motor = 0
    camera_resets = 0
    llm_failures = 0
    earliest, latest = None, None
    files = sorted(LOG_DIR.glob("*.log")) if LOG_DIR.exists() else []

    for path in files:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            dt = parse_dt(line)
            if dt:
                earliest = dt if earliest is None else min(earliest, dt)
                latest = dt if latest is None else max(latest, dt)
            if START_RE.search(line):
                starts[START_RE.search(line).group(2)] += 1
            em = EXIT_RE.search(line)
            if em:
                name, code = em.group(2), int(em.group(3))
                if code == 0:
                    clean_exit[name] += 1
                else:
                    crash_exit[name] += 1
            if CLOSE_RE.search(line):
                operator_closes += 1
            if MISSING_RE.search(line):
                missing_motor += 1
            if RESET_RE.search(line):
                camera_resets += 1
            if LLM_FAIL_RE.search(line):
                llm_failures += 1

    span_days = None
    if earliest and latest:
        span_days = round(max((latest - earliest).total_seconds() / 86400.0,
                              1 / 24), 3)

    report = {
        "log_files": [p.name for p in files],
        "log_span_days": span_days,
        "first_log": earliest.isoformat() if earliest else None,
        "last_log": latest.isoformat() if latest else None,
        "starts_by_service": dict(sorted(starts.items())),
        "clean_exits_by_service": dict(sorted(clean_exit.items())),
        "crash_exits_by_service": dict(sorted(crash_exit.items())),
        "face_operator_closes": operator_closes,
        "missing_motor_id_events": missing_motor,
        "camera_reset_events": camera_resets,
        "llm_failure_events": llm_failures,
    }

    out_dir = run_common.run_dir("reliability", args.out)
    (out_dir / "reliability.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["## Service reliability (from logs)", "",
             f"Log span: {span_days} days ({report['first_log']} -> "
             f"{report['last_log']})", "",
             "| Service | Starts | Clean exits | Crashes |",
             "|---|---:|---:|---:|"]
    names = sorted(set(starts) | set(clean_exit) | set(crash_exit))
    for name in names:
        lines.append(f"| {name} | {starts.get(name, 0)} | "
                     f"{clean_exit.get(name, 0)} | {crash_exit.get(name, 0)} |")
    lines += [
        "",
        "### Hardware / integration events",
        "",
        "| Event | Count |",
        "|---|---:|",
        f"| Face operator closes (not restarted) | {operator_closes} |",
        f"| Missing motor ID events | {missing_motor} |",
        f"| Camera reset events | {camera_resets} |",
        f"| LLM failure events | {llm_failures} |",
        "",
        "Descriptive counts over the available logs; not a certified "
        "availability figure.",
    ]
    (out_dir / "reliability.md").write_text("\n".join(lines) + "\n",
                                            encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nWrote {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
