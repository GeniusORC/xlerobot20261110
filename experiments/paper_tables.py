#!/usr/bin/env python3
"""Collect the latest experiment outputs into one manuscript-ready file.

Reads the newest run under the experiment root for each harness and
concatenates their Markdown tables into papers/generated_tables.md, so the
numbers cited in the manuscript always trace to a regenerable run directory.

Pass --run <id> to collect a specific run, or set XLEROBOT_RUN_ID; otherwise
the newest timestamped run is used.
"""
import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "experiments"))
import run_common  # noqa: E402

OUT = ROOT / "papers" / "generated_tables.md"

SOURCES = [
    ("llm_eval/mock-faithful/summary.md", "LLM routing — faithful model"),
    ("llm_eval/mock-adversarial/summary.md", "LLM routing — adversarial model"),
    ("latency/latency.md", "Stage latency"),
    ("reliability/reliability.md", "Service reliability"),
    ("gait_check/gait_check.md", "Gait pipeline data quality"),
]


def select_run(run_id):
    run_id = run_id or os.environ.get("XLEROBOT_RUN_ID")
    if run_id:
        path = run_common.experiment_root() / run_id
        return path if path.is_dir() else None
    return run_common.latest_run()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default=None,
                    help="run id (folder name) to collect instead of the newest")
    args = ap.parse_args()

    run = select_run(args.run)
    if run is None:
        print("no experiment runs found; run the harnesses first")
        return 2
    blocks = [
        "# Generated experiment tables",
        "",
        f"Source run: `{run.name}`. Regenerate with the scripts in "
        "`experiments/`. Do not hand-edit the numbers here.",
        "",
    ]
    found = 0
    for rel, title in SOURCES:
        path = run / rel
        if not path.exists():
            blocks += [f"## {title}", "", "_not run in this run_", ""]
            continue
        found += 1
        # Demote the source file's own headings so they nest under this title.
        body = "\n".join(
            ("#" + line) if line.lstrip().startswith("#") else line
            for line in path.read_text(encoding="utf-8").strip().splitlines())
        blocks += [f"## {title}", "", body, ""]
    blocks += [
        "---",
        f"Collected {datetime.now().isoformat(timespec='seconds')}. "
        "Gait outputs are feasibility data, not clinical evidence.",
    ]
    OUT.write_text("\n".join(blocks) + "\n", encoding="utf-8")
    print(f"Wrote {OUT} from {run.name} ({found} measured sections)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
