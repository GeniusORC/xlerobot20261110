#!/usr/bin/env python3
"""Load config.yaml from the project root.

Usage in any script::

    from config_loader import CFG
    radius = CFG["robot"]["wheel_radius_m"]

The file is read once per process and cached in the module.
"""
import os
import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_ROOT, "config.yaml")


def load() -> dict:
    with open(_PATH) as f:
        return yaml.safe_load(f)


CFG: dict = load()
