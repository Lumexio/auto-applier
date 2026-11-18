"""How well does the bot find the right line of your resumes for a question?  (Retrieval, evidence and generation quality checks.)

    python rag_eval.py                 compare keyword (BM25) and hybrid (BM25 + embeddings) search on your own answers
    python rag_eval.py --k 6           how many lines the search may return per question (default: answers.evidence_passages)
    python rag_eval.py --draft         also run the full draft-and-check on a few of the questions with your models (slower)
    python rag_eval.py --misses        list every question whose line was not found

The test set is your own history, with no labelling by hand:

  * a draft waiting in `python answers.py pending` rests on quotes; the lines holding those quotes are the right lines;
  * an answer you saved or approved (`python answers.py list`) that is more than a few words long was written from your sources; the line
    sharing most of its words (and enough of them) is the right line.

A question whose right line cannot be worked out this way is left out, not guessed. The search comparison needs no model (hybrid uses the
embedding model when it answers, otherwise it says so). Reads state.db and me/; writes nothing.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from typing import Any

import retrieval
from evidence import Passage, load_passages, norm, retrieve, tokens

MIN_ANSWER_TOKENS = 6          # a saved answer shorter than this ("Yes", "5 years") says nothing about which line it came from
MIN_SHARED_TOKENS = 4
MIN_COVERAGE = 0.4             # the best line must hold at least this share of the answer's words
LOOK_DEEP = 10                 # ranks are measured down to here whatever k is, so a miss at k=6 can still be seen at rank 8


@dataclass
class Case:
    question: str
    gold: frozenset[int]                       # indexes (into the passage list) of the lines that answer it
    origin: str                                # "pending" | "saved"


@dataclass
class Score:
    mode: str
    cases: int = 0
    hit1: int = 0
    hit3: int = 0
    hitk: int = 0
    mrr: float = 0.0
    empty: int = 0                             # the search returned nothing at all (the question would park)
    misses: list[str] = field(default_factory=list)
    ranks: list[int | None] = field(default_factory=list)


# --------------------------------------------------------------------------- building the test set

def quotes_in(basis: str) -> list[str]:
    """The quoted phrases of a proposal's basis (``resumes/a.md: "quote one" | "quote two"``)."""
    return [m.strip() for m in re.findall(r'"([^"]{8,})"', basis or "") if m.strip()]


def gold_from_quotes(quotes: list[str], passages: list[Passage]) -> frozenset[int]:
    wanted = [norm(q) for q in quotes]
    return frozenset(i for i, p in enumerate(passages) if any(w and w in norm(p.shown) for w in wanted))


def gold_from_answer(answer: str, passages: list[Passage]) -> frozenset[int]:
    """The line(s) that hold most of the answer's words; empty when none holds enough of them to say."""
    words = set(tokens(answer))
    if len(words) < MIN_ANSWER_TOKENS:
        return frozenset()
    scored = []
    for i, p in enumerate(passages):
        shared = words & set(tokens(p.shown))
        scored.append((len(shared) / len(words), len(shared), i))
    best = max(scored, default=(0.0, 0, -1))
    if best[0] < MIN_COVERAGE or best[1] < MIN_SHARED_TOKENS:
        return frozenset()
    return frozenset(i for cov, _, i in scored if cov >= best[0] * 0.8)           # (so each also holds at least MIN_SHARED_TOKENS words)


def build_cases(pending: list[dict[str, Any]], saved: list[dict[str, Any]], passages: list[Passage]) -> list[Case]:
    """Cases from drafts waiting for approval and from answers you saved; one case per distinct question."""
    cases: list[Case] = []
    seen: set[str] = set()
    for row in pending:
        text = str(row.get("question_text") or "").strip()
        gold = gold_from_quotes(quotes_in(str(row.get("proposal_basis") or "")), passages)
        if text and gold and norm(text) not in seen:
            seen.add(norm(text))
            cases.append(Case(text, gold, "pending"))
    for row in saved:
        text = str(row.get("question_text") or "").strip()
        if not text or norm(text) in seen or str(row.get("source") or "user") == "fact":
            continue
        gold = gold_from_answer(str(row.get("answer") or ""), passages)
        if gold:
            seen.add(norm(text))
            cases.append(Case(text, gold, "saved"))
    return cases


# --------------------------------------------------------------------------- measuring

def evaluate(cases: list[Case], passages: list[Passage], mode: str, k: int) -> Score:
    """Run the real search for each case in ``mode`` and see where its right line lands."""
    previous = retrieval._settings["mode"]
    retrieval._settings["mode"] = mode
    score = Score(mode)
    index = {id(p): i for i, p in enumerate(passages)}
    try:
        for case in cases:
            found = retrieve(case.question, passages, k=LOOK_DEEP)
            rank = next((r for r, p in enumerate(found, 1) if index.get(id(p)) in case.gold), None)
            score.cases += 1
            score.ranks.append(rank)
            score.empty += not found
            if rank is not None and rank <= k:
                score.hitk += 1
                score.mrr += 1.0 / rank
                score.hit1 += rank == 1
                score.hit3 += rank <= 3
            else:
                score.misses.append(case.question)
    finally:
        retrieval._settings["mode"] = previous
    return score


def percent(part: int, whole: int) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "-"


def report(scores: list[Score], k: int) -> str:
    lines = [f"{'search':10s} {'cases':>5s} {'right line first':>17s} {'in top 3':>9s} {f'in top {k}':>9s} {'nothing found':>14s} {'MRR':>6s}"]
    for s in scores:
        lines.append(
            f"{s.mode:10s} {s.cases:5d} {percent(s.hit1, s.cases):>17s} {percent(s.hit3, s.cases):>9s} {percent(s.hitk, s.cases):>9s} "
            f"{percent(s.empty, s.cases):>14s} {s.mrr / s.cases if s.cases else 0:6.2f}"
        )
    if len(scores) == 2 and scores[0].cases:
        pairs = list(zip(scores[0].ranks, scores[1].ranks))
        gained = sum(1 for a, b in pairs if (a is None or a > k) and b is not None and b <= k)
        lost = sum(1 for a, b in pairs if (b is None or b > k) and a is not None and a <= k)
        lines.append(f"\nhybrid found the right line for {gained} question(s) keyword search missed, and lost {lost}.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- the full draft (optional)

def draft_check(cases: list[Case], passages: list[Passage], cfg: dict[str, Any], limit: int) -> list[str]:
    """Run the real draft-and-check on up to ``limit`` cases with the configured models: did it answer, was it verified, did it cite the right line?"""
    import instructions
    from evidence import NoEvidence, draft_answer
    from form_questions import Question
    from llm_client import LLMError, build_llm_cfg, for_task

    llm = build_llm_cfg(cfg)
    draft_cfg, verify_cfg = for_task(llm, "essay"), for_task(llm, "verify")
    instructions.load_from(cfg)
    answers = cfg.get("answers") or {}
    out = []
    for case in cases[:limit]:
        q = Question(1, "textarea", case.question, [], True, "", "text", None)
        try:
            d = draft_answer(q, {}, "", passages, draft_cfg, verify_cfg, essay=True, k=int(answers.get("evidence_passages") or 6),
                             rewrites=int(answers.get("query_rewrites", 1)))
            right = any(i in case.gold and any(norm(x) in norm(passages[i].shown) for x in d.quotes) for i in range(len(passages)))
            out.append(f"{'verified' if d.verified else 'unverified':10s} {'cites the right line' if right else 'cites another line':22s} {case.question[:60]}")
        except NoEvidence as e:
            out.append(f"{'no answer':10s} {str(e)[:22]:22s} {case.question[:60]}")
        except LLMError as e:
            out.append(f"{'model down':10s} {str(e)[:22]:22s} {case.question[:60]}")
    return out


# --------------------------------------------------------------------------- command line

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="How well does the bot find the right line of your resumes for a question?")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--k", type=int, default=0)
    ap.add_argument("--draft", action="store_true", help="also run the full draft-and-check on a few questions")
    ap.add_argument("--limit", type=int, default=5, help="how many questions --draft tries")
    ap.add_argument("--misses", action="store_true", help="list the questions whose line was not found")
    args = ap.parse_args(argv)

    from config_loader import load_config
    from state import StateStore

    cfg = load_config(args.config)
    k = args.k or int((cfg.get("answers") or {}).get("evidence_passages") or 6)
    passages = load_passages(cfg)
    if not passages:
        print("No resumes or notes found (paths.source_dir); nothing to search.")
        return 1
    store = StateStore((cfg.get("daemon") or {}).get("state_db", "state.db"))
    cases = build_cases(store.list_pending(), store.list_answers(), passages)
    if not cases:
        print("Nothing to measure yet: no answer you saved or draft waiting for approval can be traced to a line of your sources.\n"
              "Approve a few drafts (python answers.py pending / approve) and run this again.")
        return 0
    retrieval.configure(cfg)
    scores = [evaluate(cases, passages, "keyword", k)]
    if retrieval.meaning_scores("test", [p.shown for p in passages[:1]]) is None:
        print(f"(The embedding model is not answering, so only keyword search is measured. ollama pull {retrieval._settings['embedding_model']})\n")
    else:
        scores.append(evaluate(cases, passages, "hybrid", k))
    print(f"{len(cases)} question(s) from your own history, {len(passages)} lines of your resumes and notes.\n")
    print(report(scores, k))
    if args.misses:
        for s in scores:
            print(f"\nNot found by {s.mode} search:")
            print("\n".join(f"  - {q[:100]}" for q in s.misses) or "  (none)")
    if args.draft:
        print(f"\nFull draft and check, first {args.limit}:")
        print("\n".join(draft_check(cases, passages, cfg, args.limit)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
