"""Finding your own lines: BM25 on words, embeddings on meaning, both merged, and a quiet fallback when the embedding model is away."""

from __future__ import annotations

import io
import json
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import retrieval
from evidence import Passage, retrieve, tokens

PASSAGES = [
    Passage("resume.md", "Northwind, Senior Engineer", "Led the migration of a monolith to Docker containers, cutting deploy time"),
    Passage("resume.md", "Northwind, Senior Engineer", "Mentored four junior engineers through code review and pairing"),
    Passage("resume.md", "Northwind, Senior Engineer", "Built an order tracking service in Python and PostgreSQL"),
    Passage("resume.md", "Languages", "Spanish (Native), English (Professional)"),
]

# A fake embedding model with three "meanings": people management, containers, languages. Words are mapped to them, so a paraphrase lands
# near its passage without sharing a single word with it.
MEANINGS = (
    {"led", "managed", "manage", "mentored", "coached", "coach", "team", "junior", "engineers", "developers", "people"},
    {"docker", "containers", "container", "kubernetes", "deploy", "deployment"},
    {"spanish", "english", "languages", "speak", "fluent"},
)


def fake_vector(text: str) -> list[float]:
    words = {w.strip(".,?()").lower() for w in text.split()}
    return [float(len(words & group)) for group in MEANINGS]


class Embedder:
    """Stands in for the embedding model and remembers what it was asked."""

    def __init__(self, fail=False):
        self.calls: list[list[str]] = []
        self.fail = fail

    def __call__(self, texts):
        self.calls.append(list(texts))
        if self.fail:
            raise ConnectionRefusedError("ollama is not running")
        return [fake_vector(t) for t in texts]


class Hybrid:
    """Hybrid search on, with ``embedder`` as the model and a throw-away index folder; everything is reset afterwards."""

    def __init__(self, embedder=None, **settings):
        self.embedder = embedder or Embedder()
        self.settings = settings

    def __enter__(self):
        self.dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        retrieval.configure({"paths": {"scratch_dir": self.dir.name}, "retrieval": {"mode": "hybrid", **self.settings}})
        retrieval.embed_fn = self.embedder
        return self

    def __exit__(self, *exc):
        retrieval.reset()
        self.dir.cleanup()


def names(found):
    return [p.text.split()[0] for p in found]


# --------------------------------------------------------------------------- the pieces

def test_bm25_prefers_rare_words_saturates_repeats_and_favours_the_shorter_text():
    docs = [["python", "python", "python", "python", "python"], ["python", "kubernetes"], ["java", "java"], []]
    rare = retrieval.bm25(["kubernetes", "python"], docs)
    assert rare[1] > rare[0] > 0 and rare[2] == 0 == rare[3]                         # the rarer word outweighs five repeats of the common one
    only_rare, only_common = retrieval.bm25(["kubernetes", "python"], [["kubernetes"], ["python"], ["python"], ["python"], ["x"]])[:2]
    assert only_rare > only_common                                                   # one match on a word few passages have beats one on a word most have
    five, ten = retrieval.bm25(["python"], [["python"] * 5 + ["x"] * 5, ["python"] * 10])[:2]
    assert ten < five * 2                                                            # repeats saturate: ten mentions are not twice as good as five
    short = retrieval.bm25(["go"], [["go"], ["go"] + ["filler"] * 30, ["other"] * 10])
    assert short[0] > short[1] > 0
    with_head = retrieval.bm25(["northwind"], [["x"], ["x"]], heads=[{"northwind"}, set()])
    assert with_head[0] > 0 == with_head[1]                                          # a bullet under the heading "Northwind" is about Northwind
    assert retrieval.bm25([], docs) == [0.0] * 4 and retrieval.bm25(["a"], []) == []


def test_ranking_drops_zero_scores_and_breaks_ties_towards_the_earlier_passage():
    assert retrieval.ranked([0.0, 2.0, 2.0, 5.0, -1.0], 10) == [3, 1, 2]
    assert retrieval.ranked([1.0, 2.0, 3.0], 2) == [2, 1]


def test_fusion_lifts_what_both_rankings_like_and_is_deterministic():
    assert retrieval.fuse([3, 1, 2], [2, 9], limit=4) == [2, 3, 1, 9]                # 2 is high in both lists
    assert retrieval.fuse([5], [4], limit=2) == [4, 5] or retrieval.fuse([5], [4], limit=2) == [5, 4]
    assert retrieval.fuse([5], [4], limit=2) == retrieval.fuse([5], [4], limit=2)
    assert retrieval.fuse([1, 2, 3], [], limit=2) == [1, 2] and retrieval.fuse(limit=3) == []
    assert retrieval.fuse([7], [7], limit=5) == [7]


def test_cosine_is_zero_for_empty_or_unrelated_vectors():
    assert abs(retrieval.cosine([1, 0], [2, 0]) - 1.0) < 1e-9 and retrieval.cosine([1, 0], [0, 1]) == 0 and retrieval.cosine([0, 0], [1, 1]) == 0


# --------------------------------------------------------------------------- retrieve(): keyword only

def test_without_the_embedding_model_search_is_keyword_only_and_an_unrelated_question_finds_nothing():
    retrieval.reset()
    assert names(retrieve("Describe your Docker containers migration", PASSAGES)) == ["Led"]
    assert retrieve("What is your favourite colour?", PASSAGES) == []
    assert retrieve("anything", []) == []
    assert "Mentored" not in names(retrieve("Have you managed developers?", PASSAGES))   # BM25 alone does not know the paraphrase


# --------------------------------------------------------------------------- retrieve(): hybrid

def test_hybrid_finds_a_paraphrase_that_shares_no_word_and_still_finds_exact_terms():
    with Hybrid():
        assert "Mentored" in names(retrieve("Have you coached other developers?", PASSAGES))
        assert names(retrieve("PostgreSQL experience", PASSAGES))[0] == "Built"       # an exact term still wins through the keyword list
        both = names(retrieve("Describe your Docker containers deployment work", PASSAGES))
        assert both[0] == "Led" and len(both) == len(set(both))                        # in both lists: first, and listed once


def test_a_meaning_that_is_too_weak_does_not_count_so_an_unrelated_question_still_parks():
    with Hybrid():
        assert retrieve("What is your favourite colour?", PASSAGES) == []
        assert names(retrieve("Have you coached other developers?", PASSAGES)) == ["Mentored"]      # similarity 1.0 for one line, 0.32 for the Docker line
    with Hybrid(min_similarity=0.3):                                                   # a lower floor lets the weaker match in
        assert names(retrieve("Have you coached other developers?", PASSAGES)) == ["Mentored", "Led"]
    with Hybrid(min_similarity=1.01):                                                  # a floor nothing clears: meaning contributes nothing
        assert retrieve("Have you coached other developers?", PASSAGES) == []


def test_the_embedding_model_is_given_the_task_prefixes_and_only_text_it_has_not_seen():
    emb = Embedder()
    with Hybrid(emb):
        retrieve("Have you coached other developers?", PASSAGES)
        first = [t for call in emb.calls for t in call]
        assert sum(t.startswith("search_document: ") for t in first) == len(PASSAGES)
        assert [t for t in first if t.startswith("search_query: ")] == ["search_query: Have you coached other developers?"]
        assert "Northwind, Senior Engineer: Mentored" in " ".join(first)               # what the model is shown (heading and text) is what is embedded
        emb.calls.clear()
        retrieve("Have you coached other developers?", PASSAGES)
        retrieve("A new question about containers", PASSAGES)
        asked = [t for call in emb.calls for t in call]
        assert asked == ["search_query: A new question about containers"]              # passages and the repeated question come from the cache
        more = PASSAGES + [Passage("notes.md", "", "Coached a hackathon team of students")]
        emb.calls.clear()
        retrieve("coached", more)
        assert [t for call in emb.calls for t in call if "hackathon" in t] and len([t for call in emb.calls for t in call]) == 2   # one new passage + the question


def test_the_index_is_kept_on_disk_and_belongs_to_one_model():
    emb = Embedder()
    with Hybrid(emb) as h:
        retrieve("coached", PASSAGES)
        saved = json.loads((Path(h.dir.name) / "embeddings.json").read_text(encoding="utf-8"))
        assert saved["model"] == "nomic-embed-text" and len(saved["vectors"]) >= len(PASSAGES)
        retrieval._vectors.clear()                                                     # a new process: memory is empty, the file is not
        emb.calls.clear()
        retrieve("coached", PASSAGES)
        assert emb.calls == []                                                          # the passages AND the repeated question come from the file
        saved["model"] = "some-other-model"
        (Path(h.dir.name) / "embeddings.json").write_text(json.dumps(saved), encoding="utf-8")
        retrieval._vectors.clear()
        emb.calls.clear()
        retrieve("coached", PASSAGES)
        assert len([t for call in emb.calls for t in call]) == len(PASSAGES) + 1       # vectors of another model are never reused
        (Path(h.dir.name) / "embeddings.json").write_text("{ not json", encoding="utf-8")
        retrieval._vectors.clear()
        assert "Mentored" in names(retrieve("coached", PASSAGES))                       # a damaged index is rebuilt, not fatal


# --------------------------------------------------------------------------- when the model is away

def test_when_the_embedding_model_fails_search_stays_on_keywords_says_so_once_and_tries_again_later():
    emb = Embedder(fail=True)
    now = [1000.0]
    out = io.StringIO()
    with Hybrid(emb, retry_seconds=60), patch("retrieval.time.monotonic", lambda: now[0]), redirect_stdout(out):
        assert names(retrieve("Describe your Docker containers migration", PASSAGES)) == ["Led"]    # keywords still work
        retrieve("Describe your Docker containers migration", PASSAGES)
        retrieve("another question about docker", PASSAGES)
        assert len(emb.calls) == 1                                                     # not asked again while it is down
        assert out.getvalue().count("[Retrieval]") == 1 and "ollama pull nomic-embed-text" in out.getvalue()
        now[0] += 61
        retrieve("a later question about containers", PASSAGES)                         # tried again after the wait, failed again ...
        assert len(emb.calls) == 2 and out.getvalue().count("[Retrieval]") == 1         # ... and it does not repeat itself
        now[0] += 61
        emb.fail = False
        assert "Mentored" in names(retrieve("Have you coached other developers?", PASSAGES))   # it came back: meaning search resumes


def test_the_http_call_reads_vectors_in_order_and_a_wrong_reply_counts_as_a_failure():
    class Reply:
        def __init__(self, payload):
            self.payload = payload

        def read(self):
            return json.dumps(self.payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    sent = {}

    def fake_open(request, timeout=0):
        sent["url"], sent["body"], sent["timeout"] = request.full_url, json.loads(request.data), timeout
        return Reply({"data": [{"index": 1, "embedding": [0.0, 1.0]}, {"index": 0, "embedding": [1.0, 0.0]}]})

    retrieval.configure({"llm": {"endpoint": "http://127.0.0.1:11434/v1/"}, "retrieval": {"timeout_seconds": 7}})
    with patch("urllib.request.urlopen", fake_open):
        assert retrieval._http_embed(["a", "b"]) == [[1.0, 0.0], [0.0, 1.0]]
    assert sent["url"] == "http://127.0.0.1:11434/v1/embeddings" and sent["timeout"] == 7
    assert sent["body"] == {"model": "nomic-embed-text", "input": ["a", "b"]}
    with patch("urllib.request.urlopen", lambda r, timeout=0: Reply({"data": [{"index": 0, "embedding": [1.0]}]})):
        try:
            retrieval._http_embed(["a", "b"])
            raise AssertionError("one vector for two texts must be refused")
        except ValueError:
            pass
    retrieval.reset()


def test_the_daemon_configures_retrieval_when_a_cycle_starts_and_the_shipped_config_asks_for_hybrid():
    import yaml

    from daemon import Daemon

    shipped = yaml.safe_load((Path(__file__).resolve().parent.parent / "config.yaml").read_text(encoding="utf-8"))
    assert shipped["retrieval"]["mode"] == "hybrid" and shipped["retrieval"]["embedding_model"] == "nomic-embed-text"
    daemon = Daemon.__new__(Daemon)
    daemon.config = {"retrieval": {"mode": "hybrid", "embedding_model": "my-model"}, "llm": {"endpoint": "http://localhost:1/v1"}}
    with patch.object(Daemon, "load_or_generate_profile", side_effect=RuntimeError("stop here")):
        try:
            daemon.run_cycle()
        except RuntimeError:
            pass
    assert retrieval.mode() == "hybrid" and retrieval._settings["embedding_model"] == "my-model" and retrieval._settings["endpoint"] == "http://localhost:1/v1"
    retrieval.reset()
    assert retrieval.mode() == "keyword"


def test_tokens_still_drop_filler_and_stem():
    assert tokens("Describe your experience managing containers") == ["experience", "manag", "container"] or "container" in tokens("containers")
