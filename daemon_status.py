"""What the daemon tells the dashboards about itself: ``status.json``, next to the database.

Two things only the running process knows, and that the terminal dashboard cannot see from the database:

* which models it has stopped asking (the circuit breaker in ``llm_client``: a model that did not answer twice in a row is left alone for a while), and
* which configuration it loaded when it started. The daemon reads ``config.yaml`` and ``config.local.yaml`` once, so an edit made afterwards does nothing
  until a restart. The file holds a fingerprint per section, and the dashboard compares it with the files on disk now.

Written atomically (a temporary file, then a rename), at most every few seconds, and whenever a breaker changes. A failed write is ignored: this file
is a courtesy and must never stop a cycle. The dashboards only read it, and only trust it while the daemon that wrote it (its pid) still holds the lock.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATUS_NAME = "status.json"
WRITE_EVERY_S = 5.0

_write_lock = threading.Lock()


def status_path(config: dict[str, Any] | None) -> Path:
    """Beside the state database, so the dashboard finds it wherever that is configured."""
    return Path(str(((config or {}).get("daemon") or {}).get("state_db") or "state.db")).with_name(STATUS_NAME)


def build(config_path: str | Path, sections: dict[str, str], started_at: str, pid: int | None = None) -> dict[str, Any]:
    from llm_client import breaker_report

    return {
        "pid": pid if pid is not None else os.getpid(),
        "started_at": started_at,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "config_path": str(Path(config_path).resolve()),
        "config_sections": sections,
        "breakers": breaker_report(),
    }


def write(path: str | Path, payload: dict[str, Any]) -> bool:
    """Replace the file in one step. False (never an exception) when that did not work: the next beat tries again."""
    target = Path(path)
    with _write_lock:
        tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
            for attempt in range(4):
                try:
                    os.replace(tmp, target)                    # Windows refuses while a reader has the file open at that instant
                    return True
                except PermissionError:
                    time.sleep(0.05 * (attempt + 1))
        except OSError:
            pass
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def remove(path: str | Path, pid: int | None = None) -> None:
    """On a clean exit: take the file away, but only if it is this process's own (a newer daemon may have replaced it)."""
    target = Path(path)
    try:
        if json.loads(target.read_text(encoding="utf-8")).get("pid") == (pid if pid is not None else os.getpid()):
            target.unlink()
    except (OSError, ValueError, AttributeError):
        pass
