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


# --------------------------------------------------------------------------- has the config changed since it was loaded?
#
# The daemon reads its config once, when it starts. Editing config.local.yaml afterwards changes nothing until it is restarted, which is easy
# to forget. The daemon therefore records a fingerprint of every top-level section as it loaded them (daemon_status.py), and the dashboards
# compare that with the files on disk now.

def section_fingerprints(config: dict[str, Any]) -> dict[str, str]:
    """One short hash per top-level section, so a warning can say WHICH part changed. Key order and YAML formatting do not matter."""
    import hashlib
    import json

    return {str(key): hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12] for key, value in config.items()}


def changed_sections(before: dict[str, str], config: dict[str, Any]) -> list[str]:
    """The sections that were edited, added or removed since ``before`` (from ``section_fingerprints``), sorted."""
    now = section_fingerprints(config)
    return sorted(key for key in set(before) | set(now) if before.get(key) != now.get(key))
