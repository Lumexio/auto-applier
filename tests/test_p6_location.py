"""Stage 2 drops postings the applicant could not take up (wrong country, on-site where they do not live, "must reside in X")
before any model looks at them, and keeps everything it is not sure about. The strings below are real postings' locations.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from stage2_filter import location as location_module
from stage2_filter.location import Candidate, candidate_from, location_verdict
from stage2_filter.places import WORLD, country_code, fold, places_in
from stage2_filter.rank import MAX_LOCATION_EXAMPLES, stage2_run
from state import StateStore
from tests.test_p0_safety import tmp_cwd

FACTS = {
    "location": {"city": "Santiago de Querétaro", "region": "Querétaro", "country": "Mexico", "open_to_remote": True},
    "work_authorization": {"authorized_countries": ["MX", "US"]},
}
PROFILE = {"skills": ["Python"], "seniority": "senior", "location": "Santiago de Querétaro, Querétaro, Mexico"}
ME = candidate_from(FACTS, PROFILE)
ON = {"enabled": True}


def verdict(location, *, remote=None, description="", cand=ME, **cfg):
    job = {"title": "Backend Engineer", "company": "Acme", "location": location, "description": description}
    if remote is not None:
        job["remote"] = remote
    return location_verdict(job, cand, {**ON, **cfg})


# --------------------------------------------------------------------------- 1. the gazetteer

def test_places_are_read_from_the_text_the_way_a_person_would():
    assert fold("Querétaro, Qro.") == "queretaro qro" and country_code("méxico") == "MX" and country_code("us") == "US"
    assert places_in("Remote - Mexico").codes == {"MX"}
    assert places_in("Querétaro").codes == {"MX"} and places_in("Guadalajara, JAL").codes == {"MX"}
    assert places_in("New Mexico").codes == {"US"}                                   # not Mexico: the longer name wins
    assert places_in("Baja California").codes == {"MX"} and places_in("California").codes == {"US"}
    assert places_in("Remote - US").codes == {"US"} and places_in("Remote (U.S.)").codes == {"US"} and places_in("USA").codes == {"US"}
    assert places_in("Join us from home").codes == frozenset()                       # the pronoun is not a country
    assert places_in("Remote - Georgia").codes == {"US"} and places_in("Toronto, Ontario, Canada").codes == {"CA"}
    assert places_in("San Jose, Costa Rica").codes == {"CR"}                         # a named country beats a city of the same name elsewhere
    assert places_in("Amsterdam, NL").codes == {"NL"} and places_in("Springfield, TX").codes == {"US"}     # ", TX" is only a last resort
    assert places_in("Remote - TX").codes == {"US"} and places_in("Bengaluru, IN").codes == {"IN"}
    assert places_in("Home Based - Americas").codes >= {"MX", "US", "BR"} and "MX" not in places_in("Home based - EMEA").codes
    assert "MX" in places_in("LATAM").codes and "MX" in places_in("North America").codes and "US" not in places_in("LATAM").codes
    assert places_in("Anywhere").codes == {WORLD} and places_in("Home based - Worldwide").codes == {WORLD}
    assert places_in("Anywhere in the United States").codes == {"US"}                # narrower than anywhere
    assert places_in("Mexico City, Mexico").granular and not places_in("Mexico").granular
    assert places_in("Remote").codes == frozenset() and places_in("Distributed").codes == frozenset() and places_in(None).codes == frozenset()
    assert places_in("Atlantis").codes == frozenset()                                # unknown stays unknown
    assert places_in("United Kingdom (Remote), United States").countries == {"GB", "US"}


# --------------------------------------------------------------------------- 2. who the applicant is

def test_the_candidate_comes_from_facts_with_the_profile_as_the_fallback():
    assert ME == Candidate("MX", frozenset({"MX", "US"}), "santiago de queretaro", False)
    only_profile = candidate_from({}, {"location": "Toronto, Canada", "work_authorization": ["CA"]})
    assert only_profile == Candidate("CA", frozenset({"CA"}), "toronto", False)
    assert candidate_from({}, {"location": "Austin, Texas, USA"}).country == "US"
    assert candidate_from({"location": {"country": "Spain"}, "work_authorization": {"authorized_countries": ["ES", "Portugal"]}}, {}).authorized == {"ES", "PT"}
    assert candidate_from({"work_authorization": {"authorized_countries": ["MX"]}}, {}).country == "MX"      # one authorization and nothing else
    assert candidate_from({"work_authorization": {"authorized_countries": ["MX", "US"]}}, {}) is None         # two, and no home: cannot tell
    assert candidate_from({}, {}) is None and candidate_from(None, None) is None
    assert candidate_from({"location": {"country": "Atlantis"}}, {"work_authorization": {"not": "a list"}}) is None
    # only an explicit "true" is willingness to relocate; null (not stated) and false are not
    for stated, expected in ((True, True), (None, False), (False, False), ("yes", False)):
        assert candidate_from({"location": {"country": "Mexico", "willing_to_relocate": stated}}, {}).relocate is expected


# --------------------------------------------------------------------------- 3. locations, from real postings

def test_postings_open_to_someone_in_mexico_are_kept():
    for loc in (
        "Remote - Mexico", "Home based - Worldwide", "Home Based - Americas; Home based - EMEA", "Mexico City / CDMX / Mexico", "Mexico City, Mexico",
        "Mexico", "Monterrey / NLE / Mexico", "Bogota D.C. / DC / Colombia; Mexico City / CDMX / Mexico; Sao Paulo / SP / Brazil",
        "Remote, Canada; Remote, United Kingdom; Remote, United States", "Home Based - Americas; Home Based - APAC; Home based - EMEA; Office Based - London, UK",
        "Canada (Remote), United States (Remote), United Kingdom (Remote) Mexico City, Mexico (Hybrid)", "Remote - LATAM", "Remote, North America",
        "Santiago de Querétaro, Mexico (On-site)", "Queretaro (Hybrid)",
    ):
        assert verdict(loc) is None, loc


def test_what_it_cannot_tell_is_kept_for_the_model_to_judge():
    for loc in ("Remote", "Distributed", "N/A", "TBD", "LOCATION", "All Locations", "", "   ", None, "Atlantis", "Cayman Islands", "Remote (West Coast)x"):
        assert verdict(loc) is None, loc
    assert location_verdict({"title": "x"}, ME, ON) is None and location_verdict({"title": "x", "location": 42}, ME, ON) is None
    assert location_verdict({"title": "x", "location": {"a": 1}}, ME, ON) is None
    assert location_verdict({"title": "x", "location": "London"}, None, ON) is None               # nobody to compare with: do nothing


def test_postings_tied_to_countries_you_cannot_work_from_are_dropped():
    for loc in (
        "Remote Spain", "Remote - Poland", "Remote UK", "Home based - EMEA", "Remote Canada", "Canada - Remote (ON, AB, BC, or NS Only)", "Cardiff, London or Remote (UK)",
        "London, United Kingdom", "Bengaluru, India", "Singapore", "Toronto, Canada", "Sao Paulo / SP / Brazil", "Tel Aviv/ Netanya, Israel", "Remote - India",
        "Bengaluru, Karnataka, India, APAC", "Reykjavík", "Remote - Europe", "Berlin, Germany; Remote, Germany", "Poland",
    ):
        assert verdict(loc), loc
    assert "Remote Spain" in verdict("Remote Spain") and "not open to MX" in verdict("Remote Spain")        # says which posting and why


def test_on_site_work_where_you_do_not_live_is_dropped_unless_you_said_you_would_relocate():
    for loc in ("New York, NY", "San Francisco, CA • New York, NY • United States", "Hybrid - San Francisco", "Seattle", "Denver, CO - Hybrid; New York, NY - Hybrid",
                "Palo Alto, CA", "Washington, D.C.", "Austin; New York City", "Hybrid", "In-Office", "Hybrid; In-Office", "On-site"):
        assert verdict(loc), loc
    assert "on-site" in verdict("New York, NY") and "relocate" in verdict("New York, NY")
    movers = candidate_from({**FACTS, "location": {**FACTS["location"], "willing_to_relocate": True}}, PROFILE)
    assert verdict("New York, NY", cand=movers) is None and verdict("Hybrid - San Francisco", cand=movers) is None and verdict("Hybrid", cand=movers) is None
    assert verdict("London, United Kingdom", cand=movers)                            # relocating needs the right to work there: only for countries you are authorized in


def test_remote_work_tied_to_a_country_you_may_work_in_follows_remote_authorized():
    us_remote = ("Remote - US", "Remote - USA", "Remote, United States", "US Remote", "Remote - US: Select locations", "Remote - TX", "Remote - Seattle",
                 "Remote (West Coast)", "New York, NY", "San Francisco, CA, US; Remote, US")
    # a Lever-style record: the board says the job is remote even when the location is only a city
    for loc in us_remote:
        assert verdict(loc, remote=True) is None, loc
        assert verdict(loc, remote=True, remote_authorized="keep") is None, loc
    assert verdict("United States") is None                                         # a country and nothing else: remote or not, the text decides
    for loc in ("Remote - US", "Remote - TX", "New York, NY"):
        assert verdict(loc, remote=True, remote_authorized="drop"), loc
    assert verdict("United States", remote_authorized="drop")
    assert verdict("Remote - Mexico", remote=True, remote_authorized="drop") is None and verdict("Remote - Americas", remote_authorized="drop") is None


def test_the_board_saying_remote_turns_a_bare_city_into_a_remote_job_but_never_beats_hybrid():
    assert verdict("Chicago, IL")                                                    # a city alone: on-site
    assert verdict("Chicago, IL", remote=True) is None                               # the board flags it as remote
    assert verdict("Chicago, IL (Hybrid)", remote=True)                              # but hybrid is hybrid
    assert verdict("Berlin", remote=True)                                            # remote within Germany is still Germany


def test_on_site_in_your_own_country_is_only_kept_where_you_live():
    assert verdict("Mexico City") is None and verdict("Mexico") is None              # no mode stated: it might be remote
    tepic = candidate_from({"location": {"country": "Mexico", "city": "Tepic"}}, {})     # a city the gazetteer has never heard of
    assert verdict("Tepic (On-site)", cand=tepic) is None and verdict("Tepic, Nayarit (Hybrid)", cand=tepic) is None
    assert verdict("Hybrid - Mexico City", cand=tepic) and verdict("On-site", cand=tepic)
    assert verdict("Hybrid - Mexico City") and "not where you live" in verdict("Hybrid - Mexico City")
    assert verdict("Monterrey, Mexico (On-site)")
    assert verdict("Ciudad de México (Remote)") is None and verdict("Hybrid - Mexico City; Remote - Mexico") is None
    assert verdict("Santiago de Querétaro (On-site)") is None and verdict("Querétaro, Qro (Hybrid)") is None
    capital = candidate_from({**FACTS, "location": {"country": "Mexico", "city": "Mexico City"}}, {})
    assert verdict("Hybrid - Mexico City", cand=capital) is None and verdict("Hybrid - CDMX, Mexico City", cand=capital) is None
    assert verdict("Hybrid - Monterrey, Mexico", cand=capital), "the word Mexico in 'Mexico City' is not a city"


def test_the_rules_are_not_specific_to_mexico():
    american = candidate_from({"location": {"country": "United States", "city": "Austin"}, "work_authorization": {"authorized_countries": ["US"]}}, {})
    assert verdict("Austin, TX (Hybrid)", cand=american) is None and verdict("Remote - US", cand=american) is None and verdict("Chicago, IL", cand=american) is None
    assert verdict("Remote Spain", cand=american) and verdict("Remote - Mexico", cand=american) and verdict("Remote - Americas", cand=american) is None
    spanish = candidate_from({"location": {"country": "Spain"}, "work_authorization": {"authorized_countries": ["ES"]}}, {})
    assert verdict("Remote - EMEA", cand=spanish) is None and verdict("Remote - Europe", cand=spanish) is None and verdict("Home based - Americas", cand=spanish)
    assert verdict("Madrid", cand=spanish) is None and verdict("Remote - US", cand=spanish)
    # when the city is unknown nothing can be "not where you live", and the home country is always one you may work in
    nocity = candidate_from({"location": {"country": "Mexico"}}, {})
    assert nocity.city == "" and verdict("Hybrid - Mexico City", cand=nocity) is None
    assert candidate_from({"location": {"country": "Mexico"}, "work_authorization": {"authorized_countries": ["US"]}}, {}).authorized == {"MX", "US"}


def test_always_keep_overrides_the_location_rules():
    assert verdict("Remote Spain") and verdict("Remote Spain", always_keep=["spain"]) is None
    assert verdict("Hybrid - Madrid, Spain", always_keep=["MADRID"]) is None
    assert verdict("Remote - Spain; New York, NY (Hybrid)") and verdict("Hybrid - Madrid; Remote, Germany")      # every alternative is unworkable
    assert verdict("Remote Spain", always_keep=["", "  "]) , "blank words must not keep everything"
    assert verdict("Remote Spain", always_keep=["spa"]), "whole words only"


# --------------------------------------------------------------------------- 4. what the posting text says

REQUIRES_US = "We are a small team building payments software. Candidates must reside in the United States to be considered. Python, Docker and Postgres."


def test_a_posting_that_says_you_must_live_elsewhere_is_dropped_without_asking_a_model():
    for text, expected in (
        (REQUIRES_US, True),
        ("You must live in the U.S. and have your own equipment.", True),
        ("This role is open only to candidates based in Germany.", True),
        ("Applicants need to be located in California.", True),
        ("The team is limited to residents of Canada.", True),
        ("We can only hire US residents only. Great benefits.", True),
        ("Candidates are required to be based in the United Kingdom or Ireland, near our offices.", True),
        ("Our customers span the Americas and the team is busy. UK residents only please, we cannot hire elsewhere.", True),   # the place is the one just before
        ("Must be located in Germany. We serve customers across the Americas and Europe all year.", True),           # the next sentence is not part of the requirement
        ("Must be a resident of the United States. Must have 5 years of Python.", True),
        # not a requirement to live elsewhere
        ("Candidates must reside in the US, Canada or Mexico.", False),                                   # names yours
        ("Candidates must reside in Latin America.", False),
        ("Our roles are not for US residents only; we hire across the Americas and Europe.", False),                 # negated
        ("Must reside in the US. Applicants must be located in Latin America, though.", False),                      # one of two requirements is yours
        ("Candidates must be located in the Americas. Our Berlin office is separate and busy.", False),
        ("You must be based in the Americas.", False),
        ("Must live in Mexico City or Guadalajara.", False),
        ("You do not need to reside in the United States.", False),
        ("There is no need to be located in the US; we hire anywhere.", False),
        ("Doesn't matter where: you don't have to live in the US.", False),
        ("You must reside in a place with reliable internet and quiet.", False),                           # no place named
        ("We have offices in Mexico City. Candidates must be located in the United States.", False),       # names your country elsewhere: let the model read it
        ("Our clients are in the United States and Germany; you will work remotely.", False),             # no requirement
        ("must reside in Atlantis", False),                                                                 # a place we do not know
    ):
        why = verdict("Remote", description=text)
        assert bool(why) is expected, (text, why)
    why = verdict("Remote", description=REQUIRES_US)
    assert "must live in US" in why and "you live in MX" in why
    assert verdict("Remote", description=REQUIRES_US, check_description=False) is None
    assert verdict("Remote", description="Candidates must reside in the") is None and verdict("Remote", description="must reside in the United States") is None   # < 40 chars: no text to read
    assert verdict("Remote - Mexico", description=REQUIRES_US) , "the text outranks a friendly location line"
    assert verdict("Remote", description=None) is None and verdict("Remote", description=12345) is None


# --------------------------------------------------------------------------- 5. in the pipeline

RAW = [
    {"id": "1", "title": "Backend Engineer", "company": "Mex", "apply_url": "https://a.test/1", "location": "Remote - Mexico"},
    {"id": "2", "title": "Backend Engineer", "company": "Lon", "apply_url": "https://a.test/2", "location": "London, United Kingdom"},
    {"id": "3", "title": "Backend Engineer", "company": "Aus", "apply_url": "https://a.test/3", "location": "Austin, TX"},
    {"id": "4", "title": "Backend Engineer", "company": "Res", "apply_url": "https://a.test/4", "location": "Remote", "content": REQUIRES_US},
    {"id": "5", "title": "Backend Engineer", "company": "Non", "apply_url": "https://a.test/5", "location": ""},
    {"id": "6", "title": "Backend Engineer", "company": "Usa", "apply_url": "https://a.test/6", "location": "Remote - US"},
]
CONFIG = {"stage2": {"location": {"enabled": True}}}


def run_stage2(config, facts=None, raw=RAW, profile=PROFILE):
    with tmp_cwd() as cwd, tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as db_dir:
        store = StateStore(Path(db_dir) / "s.db")
        rid = store.start_run("stage2", "h", "p")
        kept, diag = stage2_run([dict(j) for j in raw], dict(profile), state=store, run_id=rid, config=config, facts=facts)
        row = store.conn.execute("SELECT removed_location FROM run_diagnostics WHERE run_id = ?", (rid,)).fetchone()
        artifact = json.loads((Path(db_dir) / "jobs.json").read_text(encoding="utf-8"))
        store.close()
        return [j["id"] for j in kept], diag, row["removed_location"], artifact


def test_stage2_drops_what_the_filter_names_and_counts_and_remembers_why():
    ids, diag, stored, artifact = run_stage2(CONFIG, facts=FACTS)
    assert sorted(ids) == ["1", "5", "6"]                                            # Remote - US stays: remote_authorized defaults to keep
    assert diag.removed_location == 3 and stored == 3
    assert len(diag.location_examples) == 3 and "Backend Engineer @ Lon [London, United Kingdom]" in diag.location_examples[0]
    assert any("Backend Engineer @ Res" in e and "must live in US" in e for e in diag.location_examples)
    assert artifact["diagnostics"]["removed_location"] == 3 and artifact["diagnostics"]["location_examples"] == diag.location_examples
    ids2, diag2, _, _ = run_stage2({"stage2": {"location": {"enabled": True, "remote_authorized": "drop"}}}, facts=FACTS)
    assert sorted(ids2) == ["1", "5"] and diag2.removed_location == 4


def test_stage2_does_nothing_unless_switched_on_and_knows_who_you_are():
    everything = ["1", "2", "3", "4", "5", "6"]
    assert sorted(run_stage2({}, facts=FACTS)[0]) == everything                      # no config: the old behaviour (API, Langflow and old setups)
    assert sorted(run_stage2({"stage2": {"location": {"enabled": False}}}, facts=FACTS)[0]) == everything
    ids, diag, stored, _ = run_stage2(CONFIG, facts={}, profile={"skills": ["Python"], "seniority": "senior"})
    assert sorted(ids) == everything and diag.removed_location == 0 and stored == 0                      # nothing says where you live


def test_stage2_remembers_only_a_few_examples_but_counts_every_drop():
    raw = [{"id": str(i), "title": "Backend Engineer", "company": f"Co{i}", "apply_url": f"https://a.test/{i}", "location": "London, United Kingdom"} for i in range(MAX_LOCATION_EXAMPLES + 7)]
    ids, diag, stored, _ = run_stage2(CONFIG, facts=FACTS, raw=raw)
    assert ids == [] and diag.removed_location == stored == MAX_LOCATION_EXAMPLES + 7 and len(diag.location_examples) == MAX_LOCATION_EXAMPLES


def test_stage2_reads_the_fact_sheet_named_in_the_config_and_survives_a_broken_one():
    config = {**CONFIG, "paths": {"facts": "facts.yaml"}}
    with tmp_cwd() as cwd:
        (cwd / "facts.yaml").write_text("location:\n  country: Mexico\n  city: Santiago de Querétaro\nwork_authorization:\n  authorized_countries: [MX, US]\n", encoding="utf-8")
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as db_dir:
            store = StateStore(Path(db_dir) / "s.db")
            rid = store.start_run("stage2", "h", "p")
            kept, diag = stage2_run([dict(j) for j in RAW], {"skills": ["Python"], "seniority": "senior"}, state=store, run_id=rid, config=config)
            assert sorted(j["id"] for j in kept) == ["1", "5", "6"] and diag.removed_location == 3
            store.close()
        (cwd / "facts.yaml").write_text("location: [this is: not: valid\n", encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as db_dir:
            store = StateStore(Path(db_dir) / "s.db")
            rid = store.start_run("stage2", "h", "p")
            profile = {**PROFILE, "work_authorization": ["MX", "US"]}                                               # the profile alone still says where you live
            kept, diag = stage2_run([dict(j) for j in RAW], profile, state=store, run_id=rid, config=config)
            store.close()
        assert sorted(j["id"] for j in kept) == ["1", "5", "6"] and "Could not read facts.yaml" in out.getvalue()


def test_a_bug_in_the_filter_keeps_the_posting_and_never_stops_a_cycle():
    with patch.object(location_module, "_by_location", side_effect=RuntimeError("boom")):
        assert verdict("London, United Kingdom") is None
        ids, diag, _, _ = run_stage2(CONFIG, facts=FACTS)
    assert sorted(ids) == ["1", "2", "3", "4", "5", "6"] and diag.removed_location == 0
