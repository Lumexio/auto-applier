"""Reading config.yaml: the shipped defaults, with YOUR changes from config.local.yaml merged on top.

``config.yaml`` is part of the project and the same for everyone. Anything you want different (your models, the company boards
you watch, how strict the location filter is ...) goes in ``config.local.yaml`` beside it. That file is never committed, so a
``git pull`` can update the defaults without touching your settings, and the repository never holds one person's setup.

Merging: a section you write replaces only the keys you write in it (``models: {roles: ...}`` leaves the rest of ``models`` alone);
a list or a single value replaces the default as a whole (so your ``target_urls`` is the list, not an addition to it).

Standard library plus PyYAML only, so the terminal dashboard's small environment can use it too.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any


def local_path(path: str | Path) -> Path:
    """config.yaml -> config.local.yaml (same folder)."""
    p = Path(path)
    return p.with_name(f"{p.stem}.local{p.suffix}")


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """``override`` on top of ``base``: dicts merge key by key, anything else (lists, numbers, null) replaces. Neither argument is modified."""
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _read(path: Path) -> dict[str, Any]:
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping at the top level")
    return data


def load_config(path: str | Path = "config.yaml") -> dict[str, Any]:
    """The defaults in ``path`` with ``<name>.local.yaml`` merged over them. {} when ``path`` does not exist."""
    p = Path(path)
    if not p.is_file():
        return {}
    config = _read(p)
    local = local_path(p)
    if local.is_file():
        config = deep_merge(config, _read(local))
    return config
