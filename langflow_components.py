"""Custom visual components for Langflow integration with Auto Applier."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

# Ensure Python 3.14 dataclass patch is applied if running directly
import run_langflow

run_langflow._apply_python314_dataclass_patch()

try:
    from langflow.custom import Component
    from langflow.io import BoolInput, DataInput, DropdownInput, FloatInput, Output, StrInput
    from langflow.schema import Data
except ImportError:
    # Minimal fallback stubs if langflow is not imported
    class Component:  # type: ignore[no-redef]
        display_name = ""
        description = ""
        name = ""
        inputs = []
        outputs = []

    class Output:  # type: ignore[no-redef]
        def __init__(self, **kwargs):
            pass

    class StrInput:  # type: ignore[no-redef]
        def __init__(self, **kwargs):
            pass

    class BoolInput:  # type: ignore[no-redef]
        def __init__(self, **kwargs):
            pass

    class DataInput:  # type: ignore[no-redef]
        def __init__(self, **kwargs):
            pass

    class FloatInput:  # type: ignore[no-redef]
        def __init__(self, **kwargs):
            pass

    class DropdownInput:  # type: ignore[no-redef]
        def __init__(self, **kwargs):
            pass

    class Data:  # type: ignore[no-redef]
        def __init__(self, data=None):
            self.data = data or {}


from profile_gen import parse_resume_to_profile
from scraper import fetch_browser_jobs, fetch_greenhouse_jobs, fetch_lever_jobs
from stage2_filter.rank import stage2_run
from stage3_apply.run import stage3_run
from state import StateStore

COMMON_HEADER = """from __future__ import annotations
import json
import os
import sys
from pathlib import Path
from typing import Any

# Ensure auto-applier directory is present in sys.path when executed in Langflow sandbox
for _p in [Path.cwd(), Path(os.environ.get("AUTO_APPLIER_HOME", "."))]:
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from langflow.custom import Component
from langflow.io import Output, StrInput, BoolInput, DataInput, FloatInput, DropdownInput
from langflow.schema import Data

from profile_gen import parse_resume_to_profile
from scraper import fetch_browser_jobs, fetch_greenhouse_jobs, fetch_lever_jobs
from stage2_filter.rank import stage2_run
from stage3_apply.run import stage3_run
from state import StateStore
from outreach import craft_outreach_message, extract_recruiter_from_job_page, send_recruiter_outreach

"""


# -----------------------------------------------------------------------------
# Langflow Component 1: Profile Manager
# -----------------------------------------------------------------------------
class CandidateProfileComponent(Component):
    """Langflow Component to load, inspect, and update the applicant's profile."""

    display_name = "Candidate Profile"
    description = "Loads and manages applicant profile from resume source of truth."
    name = "CandidateProfileComponent"

    inputs = [
        BoolInput(name="reload_from_resume", display_name="Reload from Resume", value=False)
    ]
    outputs = [
        Output(name="profile", display_name="Profile Data", method="build_profile")
    ]

    def build_profile(self) -> Data:
        prof_file = Path("profile.json")
        if getattr(self, "reload_from_resume", False) or not prof_file.is_file():
            prof = parse_resume_to_profile()
            prof_file.write_text(json.dumps(prof, indent=2), encoding="utf-8")
        else:
            prof = json.loads(prof_file.read_text(encoding="utf-8"))
        return Data(data=prof)

    def build(self, reload_from_resume: bool = False) -> dict[str, Any]:
        self.reload_from_resume = reload_from_resume
        res = self.build_profile()
        return res.data if hasattr(res, "data") else res


# Alias for backward compatibility
ProfileComponent = CandidateProfileComponent


# -----------------------------------------------------------------------------
# Langflow Component 2: Job Scraper
# -----------------------------------------------------------------------------
class JobScraperComponent(Component):
    """Langflow Component to scrape job listings from ATS endpoints."""

    display_name = "Job Scraper"
    description = "Ingests job postings from Greenhouse or Lever ATS boards."
    name = "JobScraperComponent"

    inputs = [
        StrInput(name="board_token", display_name="Board Token or URL", value="stripe"),
        DropdownInput(
            name="ats_type",
            display_name="ATS Type",
            options=["greenhouse", "lever", "browser"],
            value="greenhouse",
        ),
    ]
    outputs = [
        Output(name="jobs", display_name="Scraped Jobs", method="scrape_jobs")
    ]

    def scrape_jobs(self) -> Data:
        board = getattr(self, "board_token", "stripe")
        ats = (getattr(self, "ats_type", None) or "greenhouse").lower()
        if "greenhouse" in ats and not board.startswith("http"):
            jobs = fetch_greenhouse_jobs(board)
        elif "lever" in ats and not board.startswith("http"):
            jobs = fetch_lever_jobs(board)
        else:
            jobs = fetch_browser_jobs(board)
        print(f"[Langflow] Scraped {len(jobs)} postings from {board} ({ats}).")
        return Data(data={"jobs": jobs, "count": len(jobs)})

    def build(self, board_token: str = "stripe", ats_type: str = "greenhouse") -> list[dict[str, Any]]:
        self.board_token = board_token
        self.ats_type = ats_type
        res = self.scrape_jobs()
        data = res.data if hasattr(res, "data") else res
        return data.get("jobs", [])


# -----------------------------------------------------------------------------
# Langflow Component 3: Job Filter & Ranker
# -----------------------------------------------------------------------------
class JobFilterComponent(Component):
    """Langflow Component to filter, deduplicate, and score job listings."""

    display_name = "Job Filter & Ranker"
    description = "Filters jobs by salary, exclusions, and ranks them by candidate skills overlap."
    name = "JobFilterComponent"

    inputs = [
        DataInput(name="jobs_data", display_name="Scraped Jobs Data"),
        DataInput(name="profile_data", display_name="Candidate Profile Data"),
        FloatInput(name="min_score", display_name="Min Match Score", value=0.5),
    ]
    outputs = [
        Output(name="filtered_jobs", display_name="Filtered Jobs", method="filter_jobs")
    ]

    def filter_jobs(self) -> Data:
        jobs_in = getattr(self, "jobs_data", None)
        if hasattr(jobs_in, "data"):
            jobs = jobs_in.data.get("jobs", [])
        elif isinstance(jobs_in, dict):
            jobs = jobs_in.get("jobs", [])
        elif isinstance(jobs_in, list):
            jobs = jobs_in
        else:
            jobs = []

        prof_in = getattr(self, "profile_data", None)
        if hasattr(prof_in, "data"):
            prof = prof_in.data
        elif isinstance(prof_in, dict):
            prof = prof_in
        else:
            prof = CandidateProfileComponent().build()

        store = StateStore("state.db")
        try:
            run_id = store.start_run("stage2", "langflow_run", "langflow_prof")
            filtered, diag = stage2_run(jobs, prof, state=store, run_id=run_id)
            store.finish_run(run_id, "completed")
            print(f"[Langflow] Filtered down to {len(filtered)} qualified postings.")
            return Data(data={"jobs": filtered, "count": len(filtered)})
        finally:
            store.close()

    def build(self, jobs: list[dict[str, Any]], profile: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.jobs_data = Data(data={"jobs": jobs})
        self.profile_data = Data(data=profile) if profile else None
        res = self.filter_jobs()
        data = res.data if hasattr(res, "data") else res
        return data.get("jobs", [])


# -----------------------------------------------------------------------------
# Langflow Component 4: Auto Applier
# -----------------------------------------------------------------------------
class AutoApplierComponent(Component):
    """Langflow Component to run autonomous or dry-run applications in browser."""

    display_name = "Auto Applier Engine"
    description = "Executes multi-ATS form filling and submissions with pre-authenticated sessions."
    name = "AutoApplierComponent"

    inputs = [
        DataInput(name="filtered_jobs_data", display_name="Filtered Jobs Data"),
        DataInput(name="profile_data", display_name="Candidate Profile Data"),
        BoolInput(name="dry_run", display_name="Dry Run (No Actual Submit)", value=True),
        BoolInput(name="enable_outreach", display_name="Recruiter Outreach", value=False),
        DropdownInput(
            name="browser",
            display_name="Browser Engine",
            options=["firefox", "chromium"],
            value="firefox",
        ),
    ]
    outputs = [
        Output(name="application_results", display_name="Application Results", method="run_apply")
    ]

    def run_apply(self) -> Data:
        jobs_in = getattr(self, "filtered_jobs_data", None)
        if hasattr(jobs_in, "data"):
            jobs = jobs_in.data.get("jobs", [])
        elif isinstance(jobs_in, dict):
            jobs = jobs_in.get("jobs", [])
        elif isinstance(jobs_in, list):
            jobs = jobs_in
        else:
            jobs = []

        prof_in = getattr(self, "profile_data", None)
        if hasattr(prof_in, "data"):
            prof = prof_in.data
        elif isinstance(prof_in, dict):
            prof = prof_in
        else:
            prof_file = Path("profile.json")
            if prof_file.is_file():
                prof = json.loads(prof_file.read_text(encoding="utf-8"))
            else:
                prof = parse_resume_to_profile()

        dry_run = getattr(self, "dry_run", True)
        browser = getattr(self, "browser", "firefox")
        enable_outreach = getattr(self, "enable_outreach", False)

        store = StateStore("state.db")
        try:
            run_id = store.start_run("stage3", "langflow_apply", "langflow_prof")
            cfg = {
                "stage3": {"mode": "dry_run" if dry_run else "autonomous"},
                "browser": {"type": browser},
                "outreach": {"enabled": enable_outreach},
            }
            stage3_run(jobs, prof, state=store, run_id=run_id, config=cfg)
            store.finish_run(run_id, "completed")
            return Data(
                data={
                    "status": "success",
                    "jobs_processed": len(jobs),
                    "mode": cfg["stage3"]["mode"],
                    "browser": browser,
                }
            )
        finally:
            store.close()

    def build(self, jobs: list[dict[str, Any]], dry_run: bool = True) -> dict[str, Any]:
        self.filtered_jobs_data = Data(data={"jobs": jobs})
        self.dry_run = dry_run
        res = self.run_apply()
        return res.data if hasattr(res, "data") else res


# -----------------------------------------------------------------------------
# Langflow Component 5: State & Metrics Monitor
# -----------------------------------------------------------------------------
class StateMonitorComponent(Component):
    """Langflow Component to query SQLite database metrics and recent runs."""

    display_name = "Application Metrics"
    description = "Queries state.db for application counts, runs, and success metrics."
    name = "StateMonitorComponent"

    inputs = []
    outputs = [
        Output(name="metrics", display_name="Metrics Data", method="get_metrics")
    ]

    def get_metrics(self) -> Data:
        store = StateStore("state.db")
        try:
            cur = store.conn.cursor()
            cur.execute("SELECT COUNT(*) FROM jobs")
            total_jobs = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM applications WHERE status = 'submitted'")
            submitted = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM applications WHERE status = 'pending'")
            pending = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM applications WHERE status = 'skipped'")
            skipped = cur.fetchone()[0]
            try:
                cur.execute("SELECT COUNT(*) FROM recruiter_outreach WHERE status IN ('sent', 'drafted')")
                outreaches = cur.fetchone()[0]
            except Exception:
                outreaches = 0
            return Data(
                data={
                    "total_jobs": total_jobs,
                    "submitted": submitted,
                    "pending": pending,
                    "skipped": skipped,
                    "recruiter_outreaches": outreaches,
                }
            )
        finally:
            store.close()

    def build(self) -> dict[str, Any]:
        res = self.get_metrics()
        return res.data if hasattr(res, "data") else res


def _make_node(
    cls: type[Component],
    node_id: str,
    pos_x: int,
    pos_y: int,
    icon: str = "Component",
    field_order: list[str] | None = None,
) -> dict[str, Any]:
    comp = cls()
    comp._id = node_id
    comp._code = COMMON_HEADER + inspect.getsource(cls)
    fn = comp.to_frontend_node()
    fn["id"] = node_id
    fn["type"] = "genericNode"
    fn["position"] = {"x": pos_x, "y": pos_y}
    fn["data"]["id"] = node_id
    fn["data"]["display_name"] = cls.display_name
    fn["data"]["description"] = cls.description
    fn["data"]["node"]["display_name"] = cls.display_name
    fn["data"]["node"]["description"] = cls.description
    fn["data"]["node"]["icon"] = icon

    if field_order is not None:
        fn["data"]["node"]["field_order"] = field_order
    else:
        tmpl = fn["data"]["node"].get("template", {})
        fn["data"]["node"]["field_order"] = [k for k in tmpl.keys() if k not in ["_type", "code"]]

    return fn


def _make_edge(
    edge_id: str, source: str, target: str, src_handle: dict[str, Any], tgt_handle: dict[str, Any]
) -> dict[str, Any]:
    return {
        "id": edge_id,
        "source": source,
        "target": target,
        "sourceHandle": json.dumps(src_handle),
        "targetHandle": json.dumps(tgt_handle),
        "data": {
            "sourceHandle": src_handle,
            "targetHandle": tgt_handle,
        },
    }


def generate_flow_json(output_path: str = "auto_applier_flow.json") -> dict[str, Any]:
    """Generates a 100% compliant, importable Langflow 1.12 flow schema."""
    fn_prof = _make_node(
        CandidateProfileComponent,
        "CandidateProfile_1",
        100,
        200,
        icon="User",
        field_order=["reload_from_resume"],
    )
    fn_scrap = _make_node(
        JobScraperComponent,
        "JobScraper_1",
        450,
        100,
        icon="Globe",
        field_order=["board_token", "ats_type"],
    )
    fn_filt = _make_node(
        JobFilterComponent,
        "JobFilter_1",
        800,
        150,
        icon="Filter",
        field_order=["jobs_data", "profile_data", "min_score"],
    )
    fn_app = _make_node(
        AutoApplierComponent,
        "AutoApplier_1",
        1180,
        150,
        icon="Play",
        field_order=["filtered_jobs_data", "profile_data", "dry_run", "enable_outreach", "browser"],
    )
    fn_mon = _make_node(
        StateMonitorComponent,
        "StateMonitor_1",
        800,
        450,
        icon="Activity",
        field_order=[],
    )

    edges = [
        _make_edge(
            "reactflow__edge-JobScraper_1-JobFilter_1",
            "JobScraper_1",
            "JobFilter_1",
            {"dataType": "JSON", "id": "JobScraper_1", "name": "jobs", "output_types": ["JSON"]},
            {"fieldName": "jobs_data", "id": "JobFilter_1", "inputTypes": ["JSON"], "type": "other"},
        ),
        _make_edge(
            "reactflow__edge-CandidateProfile_1-JobFilter_1",
            "CandidateProfile_1",
            "JobFilter_1",
            {"dataType": "JSON", "id": "CandidateProfile_1", "name": "profile", "output_types": ["JSON"]},
            {"fieldName": "profile_data", "id": "JobFilter_1", "inputTypes": ["JSON"], "type": "other"},
        ),
        _make_edge(
            "reactflow__edge-JobFilter_1-AutoApplier_1",
            "JobFilter_1",
            "AutoApplier_1",
            {"dataType": "JSON", "id": "JobFilter_1", "name": "filtered_jobs", "output_types": ["JSON"]},
            {"fieldName": "filtered_jobs_data", "id": "AutoApplier_1", "inputTypes": ["JSON"], "type": "other"},
        ),
        _make_edge(
            "reactflow__edge-CandidateProfile_1-AutoApplier_1",
            "CandidateProfile_1",
            "AutoApplier_1",
            {"dataType": "JSON", "id": "CandidateProfile_1", "name": "profile", "output_types": ["JSON"]},
            {"fieldName": "profile_data", "id": "AutoApplier_1", "inputTypes": ["JSON"], "type": "other"},
        ),
    ]

    payload = {
        "name": "Auto Job Applier Flow",
        "description": "Visual pipeline for scraping, filtering, and applying to jobs with pre-authenticated Firefox sessions.",
        "data": {
            "nodes": [fn_prof, fn_scrap, fn_filt, fn_app, fn_mon],
            "edges": edges,
            "viewport": {"x": 0, "y": 0, "zoom": 1},
        },
    }

    if output_path:
        out_file = Path(output_path)
        out_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"[Langflow] Generated valid flow JSON at: {out_file.resolve()}")

    return payload


if __name__ == "__main__":
    generate_flow_json()
