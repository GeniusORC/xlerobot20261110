#!/usr/bin/env python3
"""Shared run-directory handling for the Route A experiment harnesses.

A single ``XLEROBOT_RUN_ID`` groups every harness of one invocation into the
same folder under the experiment root, so ``paper_tables.py`` can collect all
sections from one coherent run instead of matching timestamps.

Resolution order:

  1. an explicit ``XLEROBOT_RUN_ID`` environment variable (shared by the whole
     invocation, e.g. set by ``run_all.sh``);
  2. a fresh ``YYYYmmdd_HHMMSS`` id generated once and exported back into the
     environment so child processes and later imports reuse it.

The experiment root defaults to ``data/experiments`` and can be overridden with
``XLEROBOT_EXPERIMENT_ROOT`` (used by tests to keep state out of real data).
"""
import os
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXPERIMENT_ROOT = ROOT / "data" / "experiments"


def experiment_root() -> Path:
    return Path(os.environ.get("XLEROBOT_EXPERIMENT_ROOT",
                               DEFAULT_EXPERIMENT_ROOT))


def run_id() -> str:
    rid = os.environ.get("XLEROBOT_RUN_ID")
    if not rid:
        rid = datetime.now().strftime("%Y%m%d_%H%M%S")
        os.environ["XLEROBOT_RUN_ID"] = rid
    return rid


def run_dir(kind: str, out: str | os.PathLike | None = None) -> Path:
    """Output directory for one harness, created before it is returned.

    ``--out`` still wins when a caller passes an explicit path, so individual
    harnesses remain runnable on their own with a custom destination.
    """
    if out:
        path = Path(out)
    else:
        path = experiment_root() / run_id() / kind
    path.mkdir(parents=True, exist_ok=True)
    return path


def latest_run() -> Path | None:
    root = experiment_root()
    runs = sorted(p for p in root.glob("*")
                  if p.is_dir() and p.name[:1].isdigit())
    return runs[-1] if runs else None
