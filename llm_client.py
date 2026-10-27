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
import re
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
    """One JSON-mode chat completion. Raises ``LLMError`` on any failure, never returns partial data."""
    cfg = llm_cfg or {}
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
        data = _post_chat(cfg, payload, wait)
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
