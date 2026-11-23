#!/usr/bin/env python3
"""Route A experiment: gait pipeline repeatability and data-quality check.

Replays every recorded health session from data/health/health.db and its
metrics JSON to characterize the *software pipeline*, not human gait:

  - how many sessions produced usable vs. insufficient frames,
  - frame acceptance ratio (effect of the full-body visibility guard),
  - internal consistency between the database row and the metrics JSON,
  - RGB-only vs depth-assisted step detection,
  - ranges of the computed metrics, with no normal/abnormal interpretation.

This deliberately makes no clinical claim. It shows the pipeline runs end to
end, records its own quality flags, and where its measurements come from. The
Route B cohort study with ground truth is what would support clinical use.
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "experiments"))
import run_common  # noqa: E402

DB = ROOT / "data" / "health" / "health.db"
HEALTH_DIR = ROOT / "data" / "health"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    if not DB.exists():
        print(f"no health database at {DB}", flush=True)
        return 2

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM sessions ORDER BY id").fetchall()

    sessions, mismatches = [], []
    usable = insufficient = 0
    frame_ratios, cadences, symmetries = [], [], []
    depth_count = rgb_count = 0

    for row in rows:
        r = dict(row)
        metrics = {}
        jpath = HEALTH_DIR / (r.get("json") or "")
        if jpath.exists():
            try:
                metrics = json.loads(jpath.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                mismatches.append(f"session {r['id']}: metrics JSON unreadable")
        # Cross-check the headline numbers stored in both places.
        for db_key, json_key in (("step_count", "step_count"),
                                 ("cadence", "cadence_spm"),
                                 ("symmetry", "knee_symmetry_pct")):
            if json_key in metrics and r.get(db_key) is not None:
                try:
                    if abs(float(r[db_key]) - float(metrics[json_key])) > 0.51:
                        mismatches.append(
                            f"session {r['id']}: {db_key} db={r[db_key]} "
                            f"json={metrics[json_key]}")
                except (TypeError, ValueError):
                    pass
        is_depth = bool(metrics.get("depth_used"))
        if is_depth:
            depth_count += 1
        else:
            rgb_count += 1
        ratio = metrics.get("frame_ratio")
        if ratio is not None:
            frame_ratios.append(float(ratio))
        if int(r.get("frames") or 0) >= 10:
            usable += 1
            cadences.append(float(r.get("cadence") or 0.0))
            symmetries.append(float(r.get("symmetry") or 0.0))
        else:
            insufficient += 1
        sessions.append({
            "id": r["id"], "ts": r["ts"], "frames": r.get("frames"),
            "steps": r.get("step_count"), "cadence": r.get("cadence"),
            "risk_label": r.get("risk"), "depth_used": is_depth,
            "frame_ratio": ratio,
            "step_length_m": metrics.get("step_length_m"),
        })

    def rng(vals):
        return {"min": min(vals), "max": max(vals)} if vals else None

    def fmt_range(d, digits=2):
        if not d:
            return "n/a"
        return f"{d['min']:.{digits}f}–{d['max']:.{digits}f}"

    summary = {
        "sessions_total": len(rows),
        "usable_sessions_frames_ge_10": usable,
        "insufficient_sessions": insufficient,
        "depth_assisted_sessions": depth_count,
        "rgb_only_sessions": rgb_count,
        "frame_acceptance_ratio": rng(frame_ratios),
        "cadence_spm_range": rng(cadences),
        "knee_symmetry_pct_range": rng(symmetries),
        "db_json_mismatches": mismatches,
        "note": "Pipeline/data-quality characterization only; no clinical "
                "interpretation, single environment, repeated captures.",
    }

    out_dir = run_common.run_dir("gait_check", args.out)
    (out_dir / "gait_check.json").write_text(
        json.dumps({"summary": summary, "sessions": sessions},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "## Gait pipeline data-quality check", "",
        "Software-pipeline characterization only; not a clinical result.", "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Sessions recorded | {summary['sessions_total']} |",
        f"| Usable sessions (>=10 frames) | {usable} |",
        f"| Insufficient-data sessions | {insufficient} |",
        f"| Depth-assisted / RGB-only | {depth_count} / {rgb_count} |",
        f"| Frame acceptance ratio range | {fmt_range(summary['frame_acceptance_ratio'])} |",
        f"| Cadence range (steps/min) | {fmt_range(summary['cadence_spm_range'], 1)} |",
        f"| Knee symmetry range (%) | {fmt_range(summary['knee_symmetry_pct_range'], 1)} |",
        f"| DB/JSON inconsistencies | {len(mismatches)} |",
        "",
        "### Per-session detail", "",
        "| ID | Frames | Steps | Cadence | Depth | Frame ratio | Label |",
        "|---:|---:|---:|---:|:---:|---:|:---|",
    ]
    for s in sessions:
        ratio = "n/a" if s["frame_ratio"] is None else s["frame_ratio"]
        lines.append(
            f"| {s['id']} | {s['frames']} | {s['steps']} | {s['cadence']} | "
            f"{'yes' if s['depth_used'] else 'no'} | {ratio} | "
            f"{s['risk_label']} |")
    (out_dir / "gait_check.md").write_text("\n".join(lines) + "\n",
                                           encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nWrote {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
