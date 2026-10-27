"""Manage what the application bot may say about you.

    python answers.py facts-init        write a starter facts.yaml from profile.json (never overwrites)
    python answers.py pending           required questions that stopped applications, most-needed first
    python answers.py set "<question>" "<answer>" [--requeue]
                                        answer one question for ALL future applications
    python answers.py list              the answers you have saved
    python answers.py forget "<question>"
    python answers.py requeue [JOB_ID]  let applications that were waiting on answers be queued again

Applications that hit a required question the fact sheet cannot answer are parked as needs_review -- the bot never
guesses. Answer here (or add the fact to facts.yaml), then requeue; a posting that is no longer listed is not
re-fetched, so requeue only helps while the job is still on the site.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from facts import init_facts_file
from form_questions import normalize_key
from state import StateStore


def _config(path: str) -> dict:
    p = Path(path)
    if not p.is_file():
        return {}
    import yaml

    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _store(cfg: dict) -> StateStore:
    return StateStore(cfg.get("daemon", {}).get("state_db", "state.db"))


def _requeue_ready(store: StateStore) -> list[str]:
    ready = store.jobs_ready_to_requeue()
    return [job_id for job_id in ready if store.requeue(job_id)]


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--profile", default="profile.json")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("facts-init")
    sub.add_parser("pending")
    sub.add_parser("list")
    p_set = sub.add_parser("set")
    p_set.add_argument("question")
    p_set.add_argument("answer")
    p_set.add_argument("--requeue", action="store_true", help="also requeue jobs that were only waiting on this answer")
    p_forget = sub.add_parser("forget")
    p_forget.add_argument("question")
    p_requeue = sub.add_parser("requeue")
    p_requeue.add_argument("job_id", nargs="?")
    args = parser.parse_args(argv)
    cfg = _config(args.config)

    if args.cmd == "facts-init":
        profile_path = Path(args.profile)
        profile = json.loads(profile_path.read_text(encoding="utf-8")) if profile_path.is_file() else {}
        target = cfg.get("paths", {}).get("facts", "facts.yaml")
        if init_facts_file(target, profile):
            print(f"Wrote {target}. Fill in the null values that are true for you; the bot never guesses them.")
        else:
            print(f"{target} already exists; not touching it.")
        return 0

    store = _store(cfg)
    try:
        if args.cmd == "pending":
            rows = store.list_pending()
            if not rows:
                print("Nothing is waiting on you.")
                return 0
            print(f"{len(rows)} question(s) are blocking applications:\n")
            for r in rows:
                print(f"  [{r['jobs']} job(s)] {r['question_text']}")
                if r["options"]:
                    print(f"      options: {r['options'].replace('|', ' | ')}")
                if r["reason"]:
                    print(f"      why it was not answered: {r['reason']}")
            print('\nAnswer one:  python answers.py set "<question>" "<answer>" --requeue\n'
                  "Or add the fact to facts.yaml (then: python answers.py requeue).")
            return 0

        if args.cmd == "list":
            rows = store.conn.execute("SELECT question_text, answer, source FROM answer_bank ORDER BY question_text").fetchall()
            for r in rows:
                print(f"  {r['question_text']}  ->  {r['answer']}   [{r['source']}]")
            print(f"{len(rows)} saved answer(s).")
            return 0

        if args.cmd == "set":
            key = normalize_key(args.question)
            if not key:
                print("That question has no readable words.")
                return 1
            store.put_answer(key, args.question, args.answer, "user")
            cleared = store.resolve_pending(key)
            print(f"Saved. Used for every future application that asks this ({cleared} waiting job(s) updated).")
            ready = store.jobs_ready_to_requeue()
            if ready and args.requeue:
                print(f"Requeued {len(_requeue_ready(store))} job(s).")
            elif ready:
                print(f"{len(ready)} job(s) now have all their answers. Run `python answers.py requeue` to queue them again.")
            return 0

        if args.cmd == "forget":
            cur = store.conn.execute("DELETE FROM answer_bank WHERE question_key = ?", (normalize_key(args.question),))
            store.conn.commit()
            print("Removed." if cur.rowcount else "No such saved answer.")
            return 0

        if args.cmd == "requeue":
            if args.job_id:
                print("Requeued." if store.requeue(args.job_id) else "That job is not parked waiting for answers.")
            else:
                done = _requeue_ready(store)
                print(f"Requeued {len(done)} job(s)." if done else "Nothing ready: every waiting job still has unanswered questions.")
            return 0
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
