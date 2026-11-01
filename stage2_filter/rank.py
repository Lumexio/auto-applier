"""Ranking, scoring, and Stage 2 pipeline orchestration."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from facts import load_facts

from .dedupe import dedupe
from .filters import is_excluded, is_expired, is_scam, salary_ok
from .location import DEFAULT_LOCATION_CFG, candidate_from, location_verdict
from .normalize import normalize_job
from .relevance import score_with_reason, title_relevance

MAX_LOCATION_EXAMPLES = 25      # how many dropped postings (and why) a run remembers, so a wrong drop can be spotted in the log


@dataclass
class Diagnostics:
    total_scraped: int = 0
    removed_expired: int = 0
    removed_location: int = 0
    removed_authorization: int = 0
    removed_salary: int = 0
    removed_exclusions: int = 0
    removed_duplicates: int = 0
    removed_scam: int = 0
    removed_relevance: int = 0
    truncated_by_cap: int = 0
    location_examples: list[str] = field(default_factory=list)


def score_job(job: dict[str, Any], profile: dict[str, Any]) -> float:
    return score_with_reason(job, profile)[0]


def _artifact_path(state: Any) -> Path:
    """jobs.json lives next to the state database (so a test or a second profile never overwrites the live one)."""
    db_path = getattr(state, "db_path", None)
    if db_path and str(db_path) != ":memory:":
        return Path(db_path).resolve().parent / "jobs.json"
    return Path("jobs.json")


def _facts_for(cfg: dict[str, Any]) -> dict[str, Any]:
    path = (cfg.get("paths") or {}).get("facts", "facts.yaml")
    try:
        return load_facts(path)
    except Exception as e:                       # malformed YAML must not take the daemon down
        print(f"[Stage 2] Could not read {path}: {e}. The location filter falls back to profile.json.")
        return {}


def stage2_run(
    raw_jobs: list[dict[str, Any]],
    profile: dict[str, Any],
    state: Any = None,
    run_id: int | None = None,
    config: dict[str, Any] | None = None,
    facts: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], Diagnostics]:
    diag = Diagnostics(total_scraped=len(raw_jobs))
    cfg = config or {}
    stage2_cfg = cfg.get("stage2", {})
    location_cfg = {**DEFAULT_LOCATION_CFG, **(stage2_cfg.get("location") or {})}
    # Where you live and may work: facts.yaml first, profile.json as the fallback. Unknown => the filter does nothing.
    candidate = candidate_from(facts if facts is not None else _facts_for(cfg), profile) if location_cfg["enabled"] else None
    safety_factor = stage2_cfg.get("safety_factor", 1.2)
    scam_heuristics = stage2_cfg.get("scam_heuristics")
    # A posting with real text must match at least this much of the profile; card-only postings (title and
    # nothing else) are judged by the title gate alone because there is nothing more to score.
    min_score = stage2_cfg.get("min_score", 0.25)
    min_description_chars = stage2_cfg.get("min_description_chars", 200)
    max_attempts = cfg.get("stage3", {}).get("max_attempts", 3)

    candidates: list[dict[str, Any]] = []

    for job in raw_jobs:
        # Validate minimal record
        if not job.get("apply_url") or not job.get("title") or not job.get("company"):
            continue
        normalize_job(job)
        if not job["title"]:
            continue

        if is_expired(job, stage2_cfg.get("stale_posting_days", 30)):
            diag.removed_expired += 1
            continue

        if is_excluded(job, profile):
            diag.removed_exclusions += 1
            continue

        if is_scam(job, scam_heuristics):
            diag.removed_scam += 1
            continue

        if not salary_ok(job, profile):
            diag.removed_salary += 1
            continue

        relevant, _why = title_relevance(job, profile)
        if not relevant:
            diag.removed_relevance += 1
            continue

        why_not_here = location_verdict(job, candidate, location_cfg)
        if why_not_here:
            diag.removed_location += 1
            if len(diag.location_examples) < MAX_LOCATION_EXAMPLES:
                diag.location_examples.append(f"{job['title']} @ {job['company']} [{job.get('location') or '-'}]: {why_not_here}")
            continue

        # Already applied to, awaiting manual review, or out of retries: never queue it again
        if state and state.is_already_submitted(job["apply_url"], max_attempts=max_attempts):
            diag.removed_duplicates += 1
            continue

        candidates.append(job)

    # De-duplication
    deduped_jobs, duplicates = dedupe(candidates)
    diag.removed_duplicates += duplicates

    # Score, then drop weak matches that we have the text to judge
    scored: list[dict[str, Any]] = []
    for job in deduped_jobs:
        job["score"], job["reason"] = score_with_reason(job, profile)
        job["status"] = "pending"
        if len(job.get("description", "")) >= min_description_chars:
            if job["score"] < min_score:
                diag.removed_relevance += 1
                continue
        else:
            job["reason"] += "; title-only (no description to score)"
        scored.append(job)
    deduped_jobs = scored

    deduped_jobs.sort(key=lambda j: j["score"], reverse=True)

    # Cap by daily limit * safety factor
    max_daily = profile.get("max_applications_per_day", 25)
    cap = int(max_daily * safety_factor)
    if len(deduped_jobs) > cap:
        diag.truncated_by_cap = len(deduped_jobs) - cap
        deduped_jobs = deduped_jobs[:cap]

    # Save to state store if provided
    if state and run_id:
        for job in deduped_jobs:
            state.save_job(job, run_id)
        # Record diagnostics table
        state.conn.execute(
            """
            INSERT OR REPLACE INTO run_diagnostics(
                run_id, total_scraped, removed_expired, removed_location,
                removed_authorization, removed_salary, removed_exclusions,
                removed_duplicates, removed_scam, removed_relevance, truncated_by_cap
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                diag.total_scraped,
                diag.removed_expired,
                diag.removed_location,
                diag.removed_authorization,
                diag.removed_salary,
                diag.removed_exclusions,
                diag.removed_duplicates,
                diag.removed_scam,
                diag.removed_relevance,
                diag.truncated_by_cap,
            ),
        )
        state.conn.commit()

    # Emit jobs.json artifact
    artifact_payload = {
        "schema_version": "2.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "diagnostics": asdict(diag),
        "jobs": deduped_jobs,
    }
    _artifact_path(state).write_text(json.dumps(artifact_payload, indent=2), encoding="utf-8")

    return deduped_jobs, diag
