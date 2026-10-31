"""Client for the local OpenAI-compatible LLM server (Ollama, llama.cpp server, LM Studio, ...).

Two things matter with small local models and are handled here rather than in every caller:

* **Context window.** Ollama serves a 2048-token window by default and silently drops the *start* of a longer
  prompt (the instructions!). Every request is budgeted up front (``estimate_tokens``), the server's reported
  ``prompt_tokens`` is checked afterwards, and ``detect_context_tokens`` measures the real window at startup.
* **Failure is routine.** Anything that talks to the model must degrade to a safe fallback. Every failure
  (unreachable, timeout, bad JSON, truncated prompt) surfaces as the one exception type ``LLMError``.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import urllib.request
from typing import Any

DEFAULT_ENDPOINT = "http://127.0.0.1:11434/v1"  # Ollama's OpenAI-compatible API
DEFAULT_MODEL = "qwen2.5:7b"
DEFAULT_CONTEXT_TOKENS = 2048                   # Ollama's default window


class LLMError(Exception):
    """The model could not give a usable answer (unreachable, timed out, invalid JSON, prompt too big/truncated)."""


def estimate_tokens(text: str) -> int:
    """Deliberately pessimistic (3.5 chars/token): we would rather chunk too early than be truncated."""
    return math.ceil(len(text) / 3.5)


def context_budget(llm_cfg: dict[str, Any] | None) -> int:
    """Tokens available for a prompt plus its reply: the measured window if known, else the configured one."""
    cfg = llm_cfg or {}
    return int(cfg.get("effective_context_tokens") or cfg.get("context_tokens") or DEFAULT_CONTEXT_TOKENS)


def list_models(endpoint: str, timeout: float = 5.0) -> list[str]:
    req = urllib.request.Request(f"{endpoint.rstrip('/')}/models", headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return [m.get("id", "") for m in data.get("data", [])]


def check_health(llm_cfg: dict[str, Any] | None, timeout: float = 5.0) -> tuple[bool, str]:
    """(usable, human-readable status). Never raises."""
    cfg = llm_cfg or {}
    endpoint = cfg.get("endpoint", DEFAULT_ENDPOINT)
    wanted = cfg.get("model", DEFAULT_MODEL)
    try:
        models = list_models(endpoint, timeout=timeout)
    except Exception as e:
        return False, (
            f"endpoint {endpoint} is not reachable ({type(e).__name__}). "
            "Anything that needs the model will use its non-LLM fallback until it is running."
        )
    # Ollama lists 'name:tag'; accept a configured name without the tag.
    if wanted and not any(m == wanted or m.startswith(f"{wanted}:") for m in models):
        return False, f"{endpoint} is up but does not serve model '{wanted}' (available: {', '.join(models) or 'none'})."
    return True, f"{endpoint} is up, model '{wanted}' available."


# --------------------------------------------------------------------------- when a model is not answering
#
# A model that is down (Ollama restarting, out of memory, swapped out behind another program's request) should cost one
# slow failure or two, not one per job: after ``failures`` requests in a row that got no answer at all, the model is left
# alone for ``cooldown_seconds`` and callers fail at once (a role then falls back to the plain ``llm`` model immediately).
# After the cool-down ONE trial request goes through: success closes the breaker, failure reopens it. Bad JSON, an oversized
# prompt or a truncated reply say nothing about availability and never count. Opt-in per config: ``build_llm_cfg`` turns it
# on with the defaults below (``breaker: {failures: 0}`` or ``breaker: false`` turns it off); a bare dict has none.

DEFAULT_BREAKER = {"failures": 2, "cooldown_seconds": 120}
_now = time.monotonic                                   # replaced in tests
_breaker_lock = threading.Lock()
_breakers: dict[tuple[str, str], dict[str, Any]] = {}  # (endpoint, model) -> failures, open_until, announced


def _breaker_key(cfg: dict[str, Any]) -> tuple[str, str]:
    return str(cfg.get("endpoint", DEFAULT_ENDPOINT)).rstrip("/"), str(cfg.get("model", DEFAULT_MODEL))


def _breaker_limits(cfg: dict[str, Any]) -> tuple[int, float]:
    spec = cfg.get("breaker")
    if not isinstance(spec, dict):
        return 0, 0.0
    return int(spec.get("failures", DEFAULT_BREAKER["failures"]) or 0), float(spec.get("cooldown_seconds", DEFAULT_BREAKER["cooldown_seconds"]))


def reset_breakers() -> None:
    with _breaker_lock:
        _breakers.clear()


def breaker_status(cfg: dict[str, Any]) -> dict[str, float]:
    """For displays and tests: consecutive failures and seconds left in the cool-down (0 = closed)."""
    with _breaker_lock:
        st = _breakers.get(_breaker_key(cfg))
        return {"failures": st["failures"] if st else 0, "open_for": max(0.0, st["open_until"] - _now()) if st else 0.0}


def _breaker_gate(cfg: dict[str, Any]) -> None:
    if not _breaker_limits(cfg)[0]:
        return
    with _breaker_lock:
        st = _breakers.get(_breaker_key(cfg))
        if st and st["open_until"] > _now():
            raise LLMError(
                f"model '{_breaker_key(cfg)[1]}' did not answer {st['failures']} requests in a row; not asking it again for {int(st['open_until'] - _now())}s"
            )


def _breaker_failed(cfg: dict[str, Any]) -> None:
    limit, cooldown = _breaker_limits(cfg)
    if not limit:
        return
    note = ""
    with _breaker_lock:
        st = _breakers.setdefault(_breaker_key(cfg), {"failures": 0, "open_until": 0.0, "announced": False})
        st["failures"] += 1
        if st["failures"] >= limit:
            st["open_until"] = _now() + cooldown
            if not st["announced"]:                       # once per outage, not every cool-down
                st["announced"] = True
                note = f"[LLM] model '{_breaker_key(cfg)[1]}' did not answer {st['failures']} requests in a row; leaving it alone for {int(cooldown)}s and retrying after that"
    if note:
        print(note)


def _breaker_ok(cfg: dict[str, Any]) -> None:
    limit, _ = _breaker_limits(cfg)
    if not limit:
        return
    note = ""
    with _breaker_lock:
        st = _breakers.get(_breaker_key(cfg))
        if st:
            if st["announced"]:
                note = f"[LLM] model '{_breaker_key(cfg)[1]}' is answering again"
            st.update(failures=0, open_until=0.0, announced=False)
    if note:
        print(note)


# --------------------------------------------------------------------------- memory

def free_ram_gb() -> float | None:
    """RAM programs could use right now (free plus reclaimable cache), in GiB; None when it cannot be read."""
    try:
        if os.name == "nt":
            import ctypes

            class _MemStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            status = _MemStatus()
            status.dwLength = ctypes.sizeof(status)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):    # type: ignore[attr-defined]
                return None
            return status.ullAvailPhys / 1024**3
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024 / 1024
    except Exception:
        return None
    return None


_loaded_cache: dict[str, tuple[float, set[str] | None]] = {}
_ram_notes: dict[str, float] = {}


def loaded_models(endpoint: str, ttl: float = 5.0) -> set[str] | None:
    """Names of the models Ollama has in memory right now (None: not Ollama, or not answering). Cached for a few seconds."""
    base = endpoint.rstrip("/")
    base = base[:-3] if base.endswith("/v1") else base
    hit = _loaded_cache.get(base)
    if hit and _now() - hit[0] < ttl:
        return hit[1]
    try:
        with urllib.request.urlopen(f"{base}/api/ps", timeout=1.0) as resp:
            names: set[str] | None = {str(m.get("name") or m.get("model")) for m in (json.loads(resp.read().decode("utf-8")).get("models") or [])}
    except Exception:
        names = None
    _loaded_cache[base] = (_now(), names)
    return names


def _short_of_ram(name: str, role: dict[str, Any], base: dict[str, Any]) -> bool:
    """True when ``role`` wants more free RAM (``min_free_ram_gb``) than there is AND its model is not already loaded.

    A model that is already in memory costs nothing more to use; sending the request elsewhere would only make Ollama
    evict it and load it again a moment later.
    """
    need = role.get("min_free_ram_gb")
    if not need:
        return False
    model = str(role.get("model", base.get("model", DEFAULT_MODEL)))
    hot = loaded_models(str(role.get("endpoint", base.get("endpoint", DEFAULT_ENDPOINT))))
    if hot and (model in hot or f"{model}:latest" in hot):
        return False
    free = free_ram_gb()
    if free is None or free >= float(need):
        return False
    if _now() - _ram_notes.get(name, -1e9) > 300:         # say so now and then, not on every request
        _ram_notes[name] = _now()
        print(f"[LLM] role '{name}' not used right now: {free:.1f} GB of RAM available, it wants {float(need):g} GB; its work goes to the main model")
    return True


# --------------------------------------------------------------------------- several models (optional)
#
# ``models:`` in config.yaml names roles (a model, plus any overrides of the ``llm`` block) and says which role does which
# task. Without that block nothing below changes anything: ``for_task`` just returns the plain ``llm`` block.

_ROUTING_KEYS = ("roles", "tasks", "fallback")        # not request settings: how a task finds its model
_ROLE_BOOKKEEPING = ("available", "label", "min_free_ram_gb")   # on a role, but not part of the request either


def build_llm_cfg(config: dict[str, Any] | None) -> dict[str, Any]:
    """The ``llm`` block plus the ``models:`` roles and tasks (if any): the one dict every component is handed."""
    config = config or {}
    cfg = dict(config.get("llm") or {})
    cfg.setdefault("breaker", dict(DEFAULT_BREAKER))   # see "when a model is not answering"
    models = config.get("models") or {}
    if isinstance(models.get("roles"), dict) and models["roles"]:
        cfg["roles"] = models["roles"]                # shared on purpose: the startup probe marks roles (un)available in place
        cfg["tasks"] = models.get("tasks") or {}
    return cfg


def role_names(llm_cfg: dict[str, Any] | None, task: str) -> list[str]:
    """The roles configured for ``task`` (one name, or a list whose members take turns); empty when unconfigured."""
    spec = ((llm_cfg or {}).get("tasks") or {}).get(task)
    if not spec:
        return []
    return [str(s) for s in spec] if isinstance(spec, (list, tuple)) else [str(spec)]


def for_task(llm_cfg: dict[str, Any] | None, task: str, vote: int = 0) -> dict[str, Any]:
    """The flat settings for one kind of job: the ``llm`` block with the task's role laid over it.

    ``vote`` picks which role of a list answers (the agent asks one model per vote). A task without a role, a role that
    does not exist, and a role that failed its startup probe all give the plain ``llm`` block, so a missing model never
    stops anything. A role with its own model also carries ``fallback``: the plain block, which ``chat_json`` tries once
    if the role's model cannot answer.
    """
    cfg = llm_cfg or {}
    base = {k: v for k, v in cfg.items() if k not in _ROUTING_KEYS}
    names = role_names(cfg, task)
    if not names:
        return base
    name = names[vote % len(names)]
    role = (cfg.get("roles") or {}).get(name)
    if not isinstance(role, dict) or role.get("available") is False or _short_of_ram(name, role, base):
        return base
    merged = dict(base)
    if any(k in role and role[k] != base.get(k) for k in ("model", "endpoint")):
        merged.pop("effective_context_tokens", None)  # that window was measured for the base model, not for this one
    merged.update({k: v for k, v in role.items() if k not in _ROLE_BOOKKEEPING})
    merged["role"] = name
    if (merged.get("model"), merged.get("endpoint")) != (base.get("model"), base.get("endpoint")):
        merged["fallback"] = base
    return merged


def probe_roles(config: dict[str, Any] | None) -> list[str]:
    """Once, at startup: does every role that a task uses have its model? Marks each ``available`` and describes it.

    An unavailable role is simply not used (its tasks fall back to the ``llm`` block). Roles no task uses are not probed.
    """
    config = config or {}
    models = config.get("models") or {}
    roles = models.get("roles")
    if not isinstance(roles, dict) or not roles:
        return []
    tasks = models.get("tasks") or {}
    used = {name for task in tasks for name in role_names({"tasks": tasks}, task)}
    base = {k: v for k, v in (config.get("llm") or {}).items() if k not in _ROUTING_KEYS}
    lines = []
    for name, role in roles.items():
        if name not in used or not isinstance(role, dict):
            continue
        probe = {**base, **{k: v for k, v in role.items() if k not in _ROLE_BOOKKEEPING}}
        ok, status = check_health(probe)
        role["available"] = ok
        model = probe.get("model", DEFAULT_MODEL)
        if ok:
            lines.append(f"role '{name}' uses {model}")
        else:
            lines.append(f"role '{name}' ({model}) is NOT available, falling back to '{base.get('model', DEFAULT_MODEL)}': {status}")
    return lines


def _post_chat(cfg: dict[str, Any], payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    endpoint = cfg.get("endpoint", DEFAULT_ENDPOINT).rstrip("/")
    req = urllib.request.Request(
        f"{endpoint}/chat/completions", data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # URLError, timeout, bad HTTP status, bad JSON envelope
        raise LLMError(f"{type(e).__name__}: {e}") from e


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_json_object(text: str) -> dict[str, Any]:
    """The first JSON object in ``text``, tolerating code fences and chatter around it."""
    cleaned = _FENCE.sub("", (text or "").strip())
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise LLMError("reply contained no JSON object") from None
        try:
            value = json.loads(cleaned[start: end + 1])
        except json.JSONDecodeError as e:
            raise LLMError(f"reply was not valid JSON: {e}") from e
    if not isinstance(value, dict):
        raise LLMError("reply JSON was not an object")
    return value


def chat_json(
    llm_cfg: dict[str, Any] | None,
    system: str,
    user: str,
    *,
    max_tokens: int = 400,
    temperature: float = 0.0,
    timeout: float | None = None,
    retries: int = 1,
) -> dict[str, Any]:
    """One JSON-mode chat completion. Raises ``LLMError`` on any failure, never returns partial data.

    When ``llm_cfg`` came from ``for_task`` with a role of its own, a failure of that role's model (not loaded, out of
    memory, prompt too big for its window) is retried once on the plain ``llm`` block, so adding a model can never
    leave a task worse off than it was without it.
    """
    cfg = llm_cfg or {}
    fallback = cfg.get("fallback")
    try:
        return _chat_json_once(cfg, system, user, max_tokens, temperature, timeout, retries)
    except LLMError as first:
        if not fallback:
            raise
        try:
            return _chat_json_once(fallback, system, user, max_tokens, temperature, timeout, retries)
        except LLMError:
            raise first from None                      # the role's error is the one worth reading


def _chat_json_once(
    cfg: dict[str, Any], system: str, user: str, max_tokens: int, temperature: float, timeout: float | None, retries: int
) -> dict[str, Any]:
    budget = context_budget(cfg)
    needed = estimate_tokens(system) + estimate_tokens(user) + max_tokens
    if needed > budget:
        raise LLMError(f"prompt does not fit the {budget}-token context window (needs ~{needed}); callers must chunk or trim")

    payload = {
        "model": cfg.get("model", DEFAULT_MODEL),
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    wait = float(timeout if timeout is not None else cfg.get("timeout_seconds", 60))

    last: Exception | None = None
    for _attempt in range(retries + 1):
        _breaker_gate(cfg)                                 # a model that just failed twice is not asked again at once
        try:
            data = _post_chat(cfg, payload, wait)
        except LLMError:                                   # no answer at all: that is what counts toward the breaker
            _breaker_failed(cfg)
            raise
        _breaker_ok(cfg)
        used = (data.get("usage") or {}).get("prompt_tokens")
        if isinstance(used, int) and used >= budget - 16:
            # The server filled its whole window: whatever it dropped came from the front of the prompt.
            raise LLMError(f"prompt reached the {budget}-token context limit and was probably truncated by the server")
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            last = LLMError(f"unexpected response shape: {e}")
            continue
        try:
            return parse_json_object(content)
        except LLMError as e:
            last = e
    raise last or LLMError("no reply")


def detect_context_tokens(llm_cfg: dict[str, Any] | None, probe_tokens: int = 3000) -> int | None:
    """Measure the server's real context window with one oversized prompt.

    Returns the window size when the server clipped the probe (``prompt_tokens`` saturates at the window),
    or None when the window is at least ``probe_tokens`` (or the server could not be asked).
    """
    cfg = llm_cfg or {}
    sentence = "The quick brown fox jumps over the lazy dog. "      # ~10 tokens
    probe = sentence * (probe_tokens // 10)
    payload = {
        "model": cfg.get("model", DEFAULT_MODEL),
        "messages": [{"role": "user", "content": probe + "\nReply with: ok"}],
        "temperature": 0,
        "max_tokens": 1,
    }
    try:
        data = _post_chat(cfg, payload, float(cfg.get("timeout_seconds", 60)))
    except LLMError:
        return None
    used = (data.get("usage") or {}).get("prompt_tokens")
    if isinstance(used, int) and used < probe_tokens * 0.85:
        return used
    return None
