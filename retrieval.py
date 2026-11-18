"""Finding the passages of your resumes and notes that a question is about: keyword search (BM25), meaning search (embeddings), or both.

  * keyword  BM25 ranks passages by the distinctive words they share with the question, with saturating counts and length
             normalisation. Exact terms ("PostgreSQL", "OpenSSH 8.2") are found; "led a team" does not find "managed engineers".
  * meaning  a small local embedding model (Ollama, ``nomic-embed-text``) turns each passage and the question into a vector; passages
             pointing the same way as the question rank high whatever words they use.
  * hybrid   both rankings are merged with reciprocal-rank fusion (a passage near the top of either list rises), so an exact term and a
             paraphrase are both found. This is the default when the embedding model answers.

Retrieval only decides WHICH of your own passages the model is shown. Every check that stops an invented fact (the quotes must appear word for
word in the passages, no number or link that is not in them, a second call to confirm) runs afterwards, so a wrong ranking can cost an
answer, never produce a false one.

If the embedding model is missing or Ollama is busy, search silently stays on BM25 for a while (``retry_seconds``) and says so once.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Sequence

DEFAULTS: dict[str, Any] = {
    "mode": "hybrid",                       # keyword | hybrid
    "embedding_model": "nomic-embed-text",  # ollama pull nomic-embed-text
    "endpoint": "",                         # empty: the llm endpoint
    "timeout_seconds": 20,
    "retry_seconds": 300,                   # after a failed embedding call, stay on keyword search this long
    "batch": 32,
    "candidates": 20,                       # how many passages each ranking contributes to the fusion
    "min_similarity": 0.55,                 # a passage must be at least this similar (cosine) to count as a meaning match: weaker ones are noise
    "query_prefix": "search_query: ",       # nomic-embed-text was trained with these task prefixes and separates much better with them
    "document_prefix": "search_document: ",
}
K1, B = 1.5, 0.75                           # BM25 constants
RRF_K = 60                                  # reciprocal-rank-fusion constant

_settings: dict[str, Any] = dict(DEFAULTS, mode="keyword")      # until a cycle configures it, nothing is embedded
_cache_path: Path | None = None
_vectors: dict[str, list[float]] = {}
_down_until = 0.0
_warned = False
embed_fn: Callable[[list[str]], list[list[float]]] | None = None   # tests (and other back ends) replace the HTTP call


def configure(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """Read ``retrieval:`` from the config (and where to keep the index); called at the start of every cycle."""
    global _settings, _cache_path, _vectors, _down_until, _warned
    section = (cfg or {}).get("retrieval") or {}
    _settings = {**DEFAULTS, **{k: v for k, v in section.items() if v is not None}}
    if not _settings["endpoint"]:
        _settings["endpoint"] = str(((cfg or {}).get("llm") or {}).get("endpoint") or "http://127.0.0.1:11434/v1")
    scratch = Path(str(((cfg or {}).get("paths") or {}).get("scratch_dir") or ".scratch"))
    path = scratch / "embeddings.json"
    if path != _cache_path:
        _cache_path, _vectors = path, {}
    _down_until, _warned = 0.0, False
    return _settings


def reset() -> None:
    """For tests: forget everything and go back to keyword search."""
    global _settings, _cache_path, _vectors, _down_until, _warned, embed_fn
    _settings, _cache_path, _vectors, _down_until, _warned, embed_fn = dict(DEFAULTS, mode="keyword"), None, {}, 0.0, False, None


def mode() -> str:
    return str(_settings["mode"])


# --------------------------------------------------------------------------- keyword: BM25

def bm25(query: Sequence[str], docs: Sequence[Sequence[str]], heads: Sequence[Sequence[str]] | None = None, head_bonus: float = 0.5) -> list[float]:
    """BM25 score of each document (a list of tokens) for the ``query`` tokens. ``heads`` are the tokens of each document's heading:
    a query word that appears there adds ``head_bonus`` (a bullet under "Northwind, Backend Engineer" is about that job)."""
    n = len(docs)
    if not n or not query:
        return [0.0] * n
    terms = set(query)
    lengths = [len(d) for d in docs]
    avg = (sum(lengths) / n) or 1.0
    df: dict[str, int] = {}
    counts: list[dict[str, int]] = []
    for d in docs:
        c: dict[str, int] = {}
        for t in d:
            if t in terms:
                c[t] = c.get(t, 0) + 1
        counts.append(c)
        for t in c:
            df[t] = df.get(t, 0) + 1
    scores: list[float] = []
    for i in range(n):
        s = 0.0
        for t, f in counts[i].items():
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            s += idf * f * (K1 + 1) / (f + K1 * (1 - B + B * lengths[i] / avg))
        if heads is not None:
            s += head_bonus * sum(1 for t in terms if t in heads[i])
        scores.append(s)
    return scores


def ranked(scores: Sequence[float], limit: int) -> list[int]:
    """Indexes of the best ``limit`` positive scores, best first (ties: the earlier passage)."""
    order = sorted((i for i, s in enumerate(scores) if s > 0), key=lambda i: (-scores[i], i))
    return order[:limit]


# --------------------------------------------------------------------------- meaning: embeddings

def _key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _http_embed(texts: list[str]) -> list[list[float]]:
    url = str(_settings["endpoint"]).rstrip("/") + "/embeddings"
    body = json.dumps({"model": _settings["embedding_model"], "input": texts}).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=float(_settings["timeout_seconds"])) as resp:
        data = json.loads(resp.read().decode("utf-8"))["data"]
    vectors = [list(map(float, item["embedding"])) for item in sorted(data, key=lambda d: d.get("index", 0))]
    if len(vectors) != len(texts) or not all(vectors):
        raise ValueError("the embedding service returned the wrong number of vectors")
    return vectors


def _load_cache() -> None:
    if _vectors or not _cache_path:
        return
    try:
        stored = json.loads(_cache_path.read_text(encoding="utf-8"))
        if stored.get("model") == _settings["embedding_model"]:
            _vectors.update({k: v for k, v in stored.get("vectors", {}).items() if isinstance(v, list)})
    except (OSError, ValueError, AttributeError):
        pass


def _save_cache() -> None:
    if not _cache_path:
        return
    try:
        _cache_path.parent.mkdir(parents=True, exist_ok=True)
        slim = {k: [round(x, 5) for x in v] for k, v in _vectors.items()}
        _cache_path.write_text(json.dumps({"model": _settings["embedding_model"], "vectors": slim}), encoding="utf-8")
    except OSError:
        pass


def vectors_for(texts: Sequence[str]) -> list[list[float]]:
    """One vector per text (cached by the text's hash; only unseen texts are sent to the model). Raises on any failure."""
    _load_cache()
    embed = embed_fn or _http_embed
    missing = [t for t in dict.fromkeys(texts) if _key(t) not in _vectors]
    step = max(1, int(_settings["batch"]))
    for start in range(0, len(missing), step):
        chunk = missing[start:start + step]
        for text, vec in zip(chunk, embed(chunk)):
            _vectors[_key(text)] = vec
    if missing:
        _save_cache()
    return [_vectors[_key(t)] for t in texts]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def meaning_scores(query: str, texts: Sequence[str]) -> list[float] | None:
    """Cosine similarity of ``query`` to each text, or None when the embedding model is not available (stays off for ``retry_seconds``)."""
    global _down_until, _warned
    if time.monotonic() < _down_until:
        return None
    try:
        docs = [str(_settings["document_prefix"]) + t for t in texts]
        vecs = vectors_for([*docs, str(_settings["query_prefix"]) + query])
    except Exception as e:                                           # the service is down, the model is not pulled, the reply is odd ...
        _down_until = time.monotonic() + float(_settings["retry_seconds"])
        if not _warned:
            _warned = True
            print(f"[Retrieval] The embedding model is not answering ({type(e).__name__}); using keyword search only for now. "
                  f"Fix: ollama pull {_settings['embedding_model']}")
        return None
    return [cosine(vecs[-1], v) for v in vecs[:-1]]


# --------------------------------------------------------------------------- merging

def fuse(*rankings: Sequence[int], limit: int) -> list[int]:
    """Reciprocal-rank fusion: each ranking gives its i-th item 1/(RRF_K + i + 1); items are ordered by the sum (ties: the lower index)."""
    total: dict[int, float] = {}
    for ranking in rankings:
        for pos, idx in enumerate(ranking):
            total[idx] = total.get(idx, 0.0) + 1.0 / (RRF_K + pos + 1)
    return sorted(total, key=lambda i: (-total[i], i))[:limit]
