#!/usr/bin/env python3
"""Load config.yaml from the project root.

Usage in any script::

    from config_loader import CFG
    radius = CFG["robot"]["wheel_radius_m"]

The file is read once per process and cached in the module.
"""
import os
from pathlib import Path
import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_ROOT, "config.yaml")


def load() -> dict:
    with open(os.environ.get("XLEROBOT_CONFIG", _PATH), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict):
        raise ValueError("robot configuration must be a mapping")
    for section in ("robot", "motors", "cameras", "services", "logs", "arms"):
        if not isinstance(cfg.get(section), dict):
            raise ValueError(f"missing configuration section: {section}")
    for key in ("wheel_radius_m", "wheelbase_m"):
        if not 0 < float(cfg["robot"][key]) < 2:
            raise ValueError(f"invalid robot.{key}")
    return cfg


CFG: dict = load()


def project_path(value):
    path = Path(value).expanduser()
    return path if path.is_absolute() else Path(_ROOT) / path


def service_url(name):
    return (os.environ.get(name.upper()) or CFG["services"][name]).rstrip("/")
