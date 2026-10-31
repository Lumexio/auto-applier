"""Postings the fit models vetoed, or disagreed about, and your say over them.

A "deal-breaker" is the one fit verdict that overrides a good score, and small models sometimes invent one. So a veto is
checked against your own facts, and (if a ``fit_review`` role is configured) against a second, different model. Where they
agree the posting is skipped for good; where they disagree it is parked here for you. Whatever the models said, you decide:

    python fit_review.py list             every veto and disagreement, newest first (add --all to include your own decisions)
    python fit_review.py show <job>       the models' reasoning: score, the deal-breaker quotes, the second opinion
    python fit_review.py approve <job>    apply to it after all (it is queued again and no model can veto it)
    python fit_review.py reject <job>     skip it for good (no model is asked about it again)

<job> is the job id shown by ``list``; the first few characters are enough.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(row.get("payload") or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def describe(row: dict[str, Any]) -> str:
    """One word for where this posting stands."""
    review = _payload(row).get("review") or {}
    if row.get("override") == "apply":
        return "you approved it"
    if row.get("override") == "skip":
        return "you rejected it"
    if row["status"] == "needs_review":
        return "models disagree: your call"
    if (row.get("error_message") or "").startswith("fit veto not confirmed"):
        return "waiting for a second opinion"
    if review.get("agreement") == "confirmed":
        return f"vetoed, confirmed by {review.get('model')}"
    return "vetoed by one model"


def reason(row: dict[str, Any], width: int = 90) -> str:
    payload = _payload(row)
    breakers = payload.get("deal_breakers") or []
    text = f"{breakers[0].get('issue', '')}: “{breakers[0].get('quote', '')}”" if breakers else (payload.get("summary") or row.get("error_message") or "")
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _find(store: Any, prefix: str) -> dict[str, Any] | None:
    rows = store.list_fit_reviews(prefix)
    exact = [r for r in rows if r["job_id"] == prefix]
    rows = exact or rows
    if not rows:
        print(f"No vetoed or disputed posting matches '{prefix}'. See: python fit_review.py list")
        return None
    if len(rows) > 1:
        print(f"'{prefix}' matches {len(rows)} postings; use more of the id:")
        for r in rows[:8]:
            print(f"  {r['job_id']}  {r['title']} @ {r['company']}")
        return None
    return rows[0]


def main(argv: list[str] | None = None) -> int:
    from state import StateStore  # imported here so `import fit_review` stays light

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="config.yaml")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").add_argument("--all", action="store_true", help="include the postings you rejected (an approved one is queued again and leaves this list)")
    for name in ("show", "approve", "reject"):
        sub.add_parser(name).add_argument("job")
    args = parser.parse_args(argv)

    cfg: dict = {}
    if Path(args.config).is_file():
        import yaml

        cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    store = StateStore(cfg.get("daemon", {}).get("state_db", "state.db"))
    try:
        if args.cmd == "list":
            rows = [r for r in store.list_fit_reviews() if args.all or not r.get("override")]
            if not rows:
                print("No vetoed or disputed postings waiting for you.")
                return 0
            print(f"{'job id':22s} {'score':>5s}  {'where it stands':30s} {'posting':44s} why")
            for r in rows:
                score = f"{float(r['score']):.0%}" if r.get("score") is not None else "-"
                posting = f"{r['title']} @ {r['company']}"
                print(f"{r['job_id'][:22]:22s} {score:>5s}  {describe(r):30s} {posting[:44]:44s} {reason(r)}")
            print("\nLook at one:  python fit_review.py show <job>      Decide:  python fit_review.py approve|reject <job>")
            return 0

        row = _find(store, args.job)
        if row is None:
            return 1
        job_id = row["job_id"]
        if args.cmd == "show":
            payload = _payload(row)
            print(f"{row['title']} @ {row['company']}\n{row['apply_url']}\njob id: {job_id}   {describe(row)}")
            print(f"\nJudge ({row.get('model') or '?'}): fit {float(row['score']):.0%}  {payload.get('summary', '')}" if row.get("score") is not None else "\nNo model assessment stored.")
            for d in payload.get("deal_breakers") or []:
                print(f"  deal-breaker: {d.get('issue')}: “{d.get('quote')}”")
            review = payload.get("review")
            if review:
                print(f"\nSecond opinion ({review.get('model')}): {review.get('verdict')} at {float(review.get('score') or 0):.0%}  {review.get('summary', '')}")
                for d in review.get("deal_breakers") or []:
                    print(f"  deal-breaker: {d.get('issue')}: “{d.get('quote')}”")
            folder = Path("applications") / "".join(c if c.isalnum() or c in "._-" else "_" for c in job_id).strip("._")[:80]
            if (folder / "fit.json").is_file():
                print(f"\nFull record: {folder / 'fit.json'}")
            return 0

        if args.cmd == "approve":
            store.set_fit_override(job_id, "apply", "approved with fit_review.py")
            store.requeue_after_fit_decision(job_id)
            print(f"Approved: {row['title']} @ {row['company']}. It is queued again; no model can veto it now.")
            print("If the posting is still listed, the next cycle applies to it (sites on probation are still only prepared).")
            return 0

        store.set_fit_override(job_id, "skip", "rejected with fit_review.py")
        store.finalize_fit_skip(job_id, "you rejected this posting (fit_review.py)")
        print(f"Rejected: {row['title']} @ {row['company']}. It is skipped for good.")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
