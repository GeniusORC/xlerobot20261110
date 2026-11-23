#!/usr/bin/env python3
"""Editable persona and sampling settings for the companion agent.

``agent_brain`` owns the safety and tool rules; those stay in code and are not
edited here.  This module stores the layer an operator may change:

  name           what the robot calls itself
  prompt         extra speaking rules, appended after the locked rules
  temperature    sampling temperature for chat and tool decisions
  max_tokens     upper bound for a spoken reply
  history_turns  how many past messages are sent with each request
  timeout_s      HTTP timeout for one LLM call

Values live in ``data/agent/settings.json`` (gitignored, re-read when its mtime
changes so hand edits and the web UI agree).  Point ``XLEROBOT_AGENT_SETTINGS``
at another path for tests.

Usage::

    import agent_settings
    s = agent_settings.load()          # always complete and validated
    agent_settings.save({"name": "小安"})   # raises ValueError on bad input
"""
import json
import os
from pathlib import Path
import threading

ROOT = Path(__file__).resolve().parent.parent
PATH = Path(os.environ.get("XLEROBOT_AGENT_SETTINGS",
                           ROOT / "data" / "agent" / "settings.json"))

DEFAULT_PROMPT = (
    "先直接回答对方这句话，再补一句关心，不要绕圈子。\n"
    "口语化，一般 2-3 句，不超过 120 个汉字。\n"
    "用得上就使用下面记得的事实，别重复问已经知道的事。"
)

DEFAULTS = {
    "name": "小乐",
    "prompt": DEFAULT_PROMPT,
    "temperature": 0.5,
    "max_tokens": 300,
    "history_turns": 10,
    "timeout_s": 12,
}

LIMITS = {
    "name": {"min_length": 1, "max_length": 24},
    "prompt": {"max_length": 4000},
    "temperature": {"min": 0.0, "max": 1.5},
    "max_tokens": {"min": 50, "max": 1200},
    "history_turns": {"min": 0, "max": 20},
    "timeout_s": {"min": 3, "max": 60},
}

_lock = threading.RLock()
_cache = None
_cache_mtime = None


def _validate(values: dict) -> dict:
    """Return a complete, normalised copy or raise ValueError."""
    if not isinstance(values.get("name"), str) or not isinstance(values.get("prompt"), str):
        raise ValueError("名字和提示词必须是文字")
    name = values["name"].strip()
    if not LIMITS["name"]["min_length"] <= len(name) <= LIMITS["name"]["max_length"]:
        raise ValueError("名字需要 1-24 个字符")
    if any(c in name for c in "\r\n\t"):
        raise ValueError("名字不能换行")

    prompt = str(values.get("prompt", ""))
    if len(prompt) > LIMITS["prompt"]["max_length"]:
        raise ValueError("提示词最多 4000 个字符")

    def number(key, cast):
        try:
            value = cast(values.get(key, DEFAULTS[key]))
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"{key} 必须是数字") from None
        if isinstance(values.get(key), bool):
            raise ValueError(f"{key} 必须是数字")
        if cast is int:
            try:
                if float(values[key]) != value:
                    raise ValueError(f"{key} 必须是整数")
            except (TypeError, OverflowError):
                raise ValueError(f"{key} 必须是整数") from None
        lo, hi = LIMITS[key]["min"], LIMITS[key]["max"]
        if not lo <= value <= hi:
            raise ValueError(f"{key} 需要在 {lo} 到 {hi} 之间")
        return value

    return {
        "name": name,
        "prompt": prompt,
        "temperature": round(number("temperature", float), 2),
        "max_tokens": number("max_tokens", int),
        "history_turns": number("history_turns", int),
        "timeout_s": round(number("timeout_s", float), 1),
    }


def load() -> dict:
    """Current settings; never raises, falls back to defaults on bad files."""
    global _cache, _cache_mtime
    with _lock:
        try:
            stat = PATH.stat()
        except OSError:
            return dict(DEFAULTS)
        if _cache is not None and _cache_mtime == stat.st_mtime_ns:
            return dict(_cache)
        try:
            raw = json.loads(PATH.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("settings must be an object")
            merged = {key: raw.get(key, value) for key, value in DEFAULTS.items()}
            values = _validate(merged)
        except (OSError, ValueError, TypeError) as exc:
            print(f"agent_settings: {PATH} ignored ({type(exc).__name__}); "
                  "using defaults", flush=True)
            _cache, _cache_mtime = dict(DEFAULTS), stat.st_mtime_ns
            return dict(DEFAULTS)
        _cache, _cache_mtime = values, stat.st_mtime_ns
        return dict(values)


def save(patch: dict) -> dict:
    """Merge ``patch`` over the current values and write them atomically.

    Unknown keys are ignored.  Raises ValueError when a value is out of range.
    """
    global _cache, _cache_mtime
    if not isinstance(patch, dict):
        raise ValueError("设置必须是 JSON 对象")
    with _lock:
        merged = {key: patch[key] for key in DEFAULTS if key in patch}
        values = _validate({**load(), **merged})
        PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(values, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        tmp.replace(PATH)
        _cache = values
        try:
            _cache_mtime = PATH.stat().st_mtime_ns
        except OSError:
            _cache_mtime = None
        return dict(values)


def reset() -> dict:
    """Delete the saved overrides so the defaults apply again."""
    global _cache, _cache_mtime
    with _lock:
        try:
            PATH.unlink()
        except FileNotFoundError:
            pass
        _cache, _cache_mtime = None, None
        return dict(DEFAULTS)


def describe() -> dict:
    """Everything the settings UI needs in one payload (no secrets here)."""
    return {"values": load(), "defaults": dict(DEFAULTS), "limits": LIMITS}
