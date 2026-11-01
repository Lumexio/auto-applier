"""A deal-breaker veto overrides a good score, and small models get it wrong. So it is checked against the candidate's own facts,
against a second (different) model, and the person has the last word. These tests pin each of those, including what stage 3
records for each outcome and the `fit_review.py` command.
"""

from __future__ import annotations

import contextlib
import io
import json
from unittest.mock import patch

import fit_review
import llm_client
from facts import merged_view
from fit import FitScorer, build_fit_gate, met_by_candidate
from llm_client import LLMError, build_llm_cfg
from stage3_apply.ats_handlers.base import FitDisputed, NotAFit
from stage3_apply.run import stage3_run
from state import StateStore
from tests.test_p0_safety import PROFILE as RUN_PROFILE, app_row, cfg_for, fake_playwright, save_job, tmp_cwd
from tests.test_p2_fit import FACTS, JOB, POSTING, PROFILE, model_says
from tests.test_p2_flow import DynPage

ENDPOINT = "http://127.0.0.1:11434/v1"
VETO = {"issue": "clearance", "quote": "must be a US citizen with an active TS/SCI clearance"}          # really in POSTING
US_AUTH = {"issue": "residence", "quote": "must be authorized to work in the United States; we do not sponsor visas"}


def llm(second: bool = True, role_model: str = "review-m", **role):
    cfg = {"endpoint": ENDPOINT, "model": "judge-m", "context_tokens": 4096, "effective_context_tokens": 4096}
    models = {"roles": {"rev": {"model": role_model, **role}}, "tasks": {"fit_review": "rev"}} if second else None
    return build_llm_cfg({"llm": cfg, **({"models": models} if models else {})})


def post_with(extra: str) -> str:
    return POSTING + " " + extra


class Models:
    """Stands in for fit.chat_json: a reply (dict) or exception per model name; records who was asked what."""

    def __init__(self, **replies):
        self.replies, self.calls, self.prompts, self.cfgs = replies, [], {}, {}

    def __call__(self, cfg, system, user, **kw):
        name = cfg["model"]
        self.calls.append(name)
        self.prompts[name] = user
        self.cfgs[name] = cfg
        reply = self.replies[name]
        if isinstance(reply, Exception):
            raise reply
        return reply


def decisive(**over):
    return model_says(80, deal_breakers=[VETO], **over)


# --------------------------------------------------------------------------- 1. vetoes the facts already answer

def test_a_veto_the_candidates_own_facts_already_satisfy_is_not_a_veto():
    view = merged_view(FACTS, PROFILE)                                               # authorized in MX and US, needs no sponsorship, lives in Toronto
    met = lambda quote, v=view, p=PROFILE: met_by_candidate(quote, v, p)             # noqa: E731
    assert met("Applicants must be authorized to work in the United States; we do not sponsor visas.") == "you are authorized to work in US"
    assert met("must be authorized to work in the US without sponsorship") == "you are authorized to work in US"
    assert met("must be authorized to work in the United States or Canada") == "you are authorized to work in US"
    assert met("based in our Toronto office, two days a week on site") == "you live in Toronto"
    # the wording real postings use (these two were real vetoes, at 78% and 85% fit, for someone authorized in the US)
    assert met("US work authorization required") == "you are authorized to work in US"
    assert met("Candidates must hold valid U.S. work authorization.") == "you are authorized to work in US"
    assert met("Eligible to work in the USA") == "you are authorized to work in US"
    assert met("Applicants must be authorized to work in the US. Join us!") == "you are authorized to work in US"     # the pronoun "us" is not a country

    for hard in (
        "must be a US citizen with an active TS/SCI clearance", "authorized to work in the United States and a US citizen", "On-site only.",
        "On-site in Singapore, five days a week", "authorized to work in Germany", "you must reside in Toronto", "relocation to Toronto is required",
        "You must already reside in Germany and hold a valid EU work permit", "we cannot sponsor visas or support relocation", "",
        "Canadian work authorization required", "authorized to work in the US and Canada", "right to work in Australia", "work authorization required",
        "join us and help us grow", "authorized to work in Canada or the United Kingdom",                       # "or", but none of them is a country you are authorized in
        "work authorization required. Join us!",                                                                # the pronoun is not a country
        "Our US offices offer free lunch and a learning budget",                                                 # a country, but no work-authorization requirement at all
    ):
        assert met(hard) is None, hard                                               # citizenship, clearance, residence, relocation, other places: not ours to waive

    only_mx = {**PROFILE, "work_authorization": ["MX"]}
    facts_mx = {**FACTS, "work_authorization": {"authorized_countries": ["MX"], "requires_sponsorship": False}}
    assert met_by_candidate(US_AUTH["quote"], merged_view(facts_mx, only_mx), only_mx) is None            # not authorized in the US: a real veto
    needs = {**FACTS, "work_authorization": {"authorized_countries": ["MX", "US"], "requires_sponsorship": True}}
    assert met_by_candidate(US_AUTH["quote"], merged_view(needs, PROFILE), PROFILE) is None               # needs sponsorship and the posting refuses it
    unstated = {**FACTS, "work_authorization": {"authorized_countries": ["MX", "US"], "requires_sponsorship": None}}
    assert met_by_candidate(US_AUTH["quote"], merged_view(unstated, PROFILE), PROFILE) is None            # sponsorship left unstated is not "no sponsorship needed"
    assert met_by_candidate("US work authorization required", merged_view(unstated, PROFILE), PROFILE) == "you are authorized to work in US"   # but no mention of it is fine
    assert met_by_candidate("Toronto office", merged_view(FACTS, {**PROFILE, "location": "Montreal, Canada"}), {**PROFILE, "location": "Montreal, Canada"}) is None


def test_the_scorer_drops_met_vetoes_and_keeps_real_ones_even_in_cached_assessments():
    judge = Models(**{"judge-m": model_says(78, deal_breakers=[US_AUTH])})
    with patch("fit.chat_json", judge):
        r = FitScorer(PROFILE, FACTS, llm(False), {}, None).score(JOB, post_with(US_AUTH["quote"] + "."))
    assert r.verdict == "apply" and r.deal_breakers == [] and r.vetoes_met[0]["why"] == "you are authorized to work in US"

    with patch("fit.chat_json", Models(**{"judge-m": decisive()})):
        assert FitScorer(PROFILE, FACTS, llm(False), {}, None).score(JOB, POSTING).verdict == "skip"      # a clearance is a real deal-breaker

    with tmp_cwd() as tmp:
        store = StateStore(tmp / "s.db")
        scorer = FitScorer(PROFILE, FACTS, llm(False), {}, store)
        old = {"fit": 0.78, "seniority_match": "match", "missing_must_haves": [], "deal_breakers": [US_AUTH], "summary": "ok"}   # stored before this rule existed
        store.put_fit("j1", scorer.cache_key, 0.78, "skip", json.dumps(old), "judge-m")
        with patch("fit.chat_json", side_effect=AssertionError("cached: no model call")):
            again = scorer.score(JOB, POSTING)
        assert again.cached and again.verdict == "apply" and again.vetoes_met                                  # the better rule fixes the old assessment
        store.close()


# --------------------------------------------------------------------------- 2. the second opinion

def score_with(models: Models, scorer: FitScorer, text: str = POSTING):
    with patch("fit.chat_json", models):
        return scorer.score(JOB, text)


def test_a_decisive_veto_is_final_only_when_a_different_model_agrees():
    scorer = lambda **kw: FitScorer(PROFILE, FACTS, llm(**kw), {}, None)               # noqa: E731

    models = Models(**{"judge-m": decisive(), "review-m": model_says(20)})
    r = score_with(models, scorer())
    assert (r.verdict, r.review["agreement"], r.review["model"]) == ("skip", "confirmed", "review-m") and models.calls == ["judge-m", "review-m"]

    confirmed = Models(**{"judge-m": decisive(), "review-m": model_says(10, deal_breakers=[VETO])})
    with patch("fit.chat_json", confirmed):
        try:
            build_fit_gate(scorer())(object(), {**JOB, "description": POSTING})
            raise AssertionError("a confirmed veto must stop the job")
        except NotAFit as e:
            assert "confirmed by review-m" in e.summary and e.details["review"]["agreement"] == "confirmed" and e.details.get("error") == ""

    disputed = Models(**{"judge-m": decisive(), "review-m": model_says(85)})            # the second model would apply
    with patch("fit.chat_json", disputed):
        try:
            build_fit_gate(scorer())(object(), {**JOB, "description": POSTING})
            raise AssertionError("a disagreement must stop the job (and park it)")
        except FitDisputed as e:
            assert "judge-m vetoed it" in e.summary and "review-m would apply (85%)" in e.summary and e.details["review"]["agreement"] == "disputed"

    for broken, why in ((LLMError("ConnectionError: refused"), "refused"), (model_says(150), "unusable")):    # unreachable / nonsense
        down = Models(**{"judge-m": decisive(), "review-m": broken})
        with patch("fit.chat_json", down):
            try:
                build_fit_gate(scorer())(object(), {**JOB, "description": POSTING})
                raise AssertionError("an unconfirmed veto must not pass")
            except NotAFit as e:
                assert e.details["error"] == "review_unavailable" and "fit veto not confirmed" in e.summary and why in e.summary


def test_the_reviewer_is_blind_independent_and_asked_only_when_it_matters():
    judge_reply, scorer = decisive(), lambda **kw: FitScorer(PROFILE, FACTS, llm(**kw), {}, None)   # noqa: E731
    models = Models(**{"judge-m": judge_reply, "review-m": model_says(20)})
    score_with(models, scorer())
    assert models.prompts["judge-m"] == models.prompts["review-m"], "same posting, same question: the reviewer is not told the judge's answer"
    assert "clearance" not in models.prompts["review-m"].split("POSTING")[0], "nothing of the judge's reasoning reaches the reviewer"
    assert "fallback" not in models.cfgs["review-m"], "the reviewer must never fall back to the judge: that would be the same model twice"

    def asked(replies, **kw):
        m = Models(**replies)
        r = score_with(m, scorer(**kw))
        return m.calls, r

    calls, _ = asked({"judge-m": model_says(30)})                                       # low score, no veto: the model's overall judgement stands
    assert calls == ["judge-m"]
    calls, _ = asked({"judge-m": model_says(40, deal_breakers=[VETO])})                 # a veto, but the score alone would have skipped: not decisive
    assert calls == ["judge-m"]
    calls, r = asked({"judge-m": model_says(80)})                                       # apply: nothing to confirm
    assert calls == ["judge-m"] and r.verdict == "apply"
    calls, _ = asked({"judge-m": decisive()}, second=False)                              # no fit_review role configured
    assert calls == ["judge-m"]
    calls, _ = asked({"judge-m": decisive()}, role_model="judge-m")                      # a second opinion from the same model is none at all
    assert calls == ["judge-m"]
    cfg_off = FitScorer(PROFILE, FACTS, llm(), {"second_opinion": False}, None)
    m = Models(**{"judge-m": decisive()})
    assert score_with(m, cfg_off).verdict == "skip" and m.calls == ["judge-m"]           # switched off in config

    # the reviewer's own vetoes go through the same facts check
    m = Models(**{"judge-m": decisive(), "review-m": model_says(80, deal_breakers=[US_AUTH])})
    r = score_with(m, scorer(), post_with(US_AUTH["quote"] + "."))
    assert r.review["verdict"] == "apply" and r.review["agreement"] == "disputed"          # its "US authorization" veto is waived by the facts: it would apply


def test_a_reviewer_that_cannot_run_right_now_postpones_the_verdict_instead_of_confirming_it():
    with patch.object(llm_client, "free_ram_gb", return_value=1.0), patch.object(llm_client, "loaded_models", return_value=set()), contextlib.redirect_stdout(io.StringIO()):
        scorer = FitScorer(PROFILE, FACTS, llm(min_free_ram_gb=5.5), {}, None)        # the reviewer's role wants RAM the machine does not have
        models = Models(**{"judge-m": decisive(), "review-m": model_says(10)})
        r = score_with(models, scorer)
    assert models.calls == ["judge-m"], "no second opinion was possible, and the judge must not be asked to agree with itself"
    assert r.verdict == "skip" and r.review["agreement"] == "unavailable" and r.error == "review_unavailable"


# --------------------------------------------------------------------------- 3. caching

def test_the_second_opinion_is_cached_and_a_postponed_one_is_asked_again_later():
    with tmp_cwd() as tmp:
        store = StateStore(tmp / "s.db")
        scorer = FitScorer(PROFILE, FACTS, llm(), {}, store)
        first = Models(**{"judge-m": decisive(), "review-m": model_says(85)})
        r1 = score_with(first, scorer)
        assert first.calls == ["judge-m", "review-m"] and r1.review["agreement"] == "disputed"
        r2 = score_with(Models(), scorer)                                                  # any model call would raise KeyError
        assert r2.cached and r2.review["agreement"] == "disputed" and r2.verdict == "skip"

        scorer2 = FitScorer(PROFILE, FACTS, llm(), {}, store)
        job2 = {**JOB, "id": "j2"}
        down = Models(**{"judge-m": decisive(), "review-m": LLMError("refused")})
        with patch("fit.chat_json", down):
            assert scorer2.score(job2, POSTING).review["agreement"] == "unavailable"
        up = Models(**{"review-m": model_says(5, deal_breakers=[VETO])})                   # the judge is NOT asked again: only the reviewer
        with patch("fit.chat_json", up):
            r = scorer2.score(job2, POSTING)
        assert up.calls == ["review-m"] and r.review["agreement"] == "confirmed" and r.cached
        assert json.loads(store.get_fit("j2", scorer2.cache_key)["payload"])["review"]["agreement"] == "confirmed"

        # an assessment cached before a reviewer existed gets its second opinion when it is next read
        scorer3 = FitScorer(PROFILE, FACTS, llm(), {}, store)
        old = {"fit": 0.8, "seniority_match": "match", "missing_must_haves": [], "deal_breakers": [VETO], "summary": "old"}
        store.put_fit("j3", scorer3.cache_key, 0.8, "skip", json.dumps(old), "judge-m")
        late = Models(**{"review-m": model_says(90)})
        with patch("fit.chat_json", late):
            assert scorer3.score({**JOB, "id": "j3"}, POSTING).review["agreement"] == "disputed"
        assert late.calls == ["review-m"]
        store.close()

    same = FitScorer(PROFILE, FACTS, llm(), {"second_opinion": False}, None).cache_key
    assert same == FitScorer(PROFILE, FACTS, llm(False), {}, None).cache_key              # reviewer off: the key is what it always was
    assert FitScorer(PROFILE, FACTS, llm(), {}, None).cache_key != same                   # reviewer on: judged anew


# --------------------------------------------------------------------------- 4. your decision

def test_your_decision_beats_every_model_and_the_cache():
    with tmp_cwd() as tmp:
        store = StateStore(tmp / "s.db")
        scorer = FitScorer(PROFILE, FACTS, llm(), {}, store)
        store.put_fit("j1", scorer.cache_key, 0.8, "skip", json.dumps({"fit": 0.8, "seniority_match": "match", "missing_must_haves": [], "deal_breakers": [VETO], "summary": "x"}), "judge-m")
        with patch("fit.chat_json", Models(**{"review-m": model_says(5, deal_breakers=[VETO])})):
            assert scorer.score(JOB, POSTING).verdict == "skip"                              # the cached veto, confirmed, before you decide
        with patch("fit.chat_json", side_effect=AssertionError("your decision needs no model")):
            store.set_fit_override("j1", "apply")
            r = scorer.score(JOB, POSTING)
            assert (r.verdict, r.model) == ("apply", "you") and not r.cached
            assert scorer.score(JOB, "too short").verdict == "apply"                          # even a posting too thin to judge
            store.set_fit_override("j1", "skip")
            assert scorer.score(JOB, POSTING).verdict == "skip"
        assert store.get_fit_override("j1") == "skip" and store.get_fit_override("nope") is None
        try:
            store.set_fit_override("j1", "maybe")
            raise AssertionError
        except ValueError:
            pass
        store.close()


# --------------------------------------------------------------------------- 5. what stage 3 records

def run_outcome(models: Models, tmp, store=None, job_id="g1"):
    store = store or StateStore(tmp / "s.db")
    rid = store.start_run("stage3", "h", "p")
    job = {**save_job(store, rid, job_id, "https://boards.greenhouse.io/acme/jobs/" + job_id), "description": POSTING}
    cfg = {**cfg_for(tmp, "autonomous"), "llm": {"endpoint": ENDPOINT, "model": "judge-m", "context_tokens": 4096},
           "models": {"roles": {"rev": {"model": "review-m"}}, "tasks": {"fit_review": "rev"}}}
    with fake_playwright(DynPage()), patch("answerer.chat_json", side_effect=LLMError("down")), patch("fit.chat_json", models), \
            patch("stage3_apply.run.wait_for_confirmation", return_value=False), contextlib.redirect_stdout(io.StringIO()) as out:
        stage3_run([job], dict(RUN_PROFILE, mode="autonomous"), state=store, run_id=rid, config=cfg)
    return store, out.getvalue()


def test_stage_3_records_each_outcome_of_a_veto_differently():
    with tmp_cwd() as tmp:
        store, out = run_outcome(Models(**{"judge-m": decisive(), "review-m": model_says(10, deal_breakers=[VETO])}), tmp)
        row = app_row(store, "g1")
        assert (row["status"], row["error_code"]) == ("skipped", "not_a_fit") and "confirmed by review-m" in row["error_message"]
        assert store.is_already_submitted("https://boards.greenhouse.io/acme/jobs/g1") is True                 # final
        store.close()
    with tmp_cwd() as tmp:
        store, out = run_outcome(Models(**{"judge-m": decisive(), "review-m": model_says(90)}), tmp)
        row = app_row(store, "g1")
        assert (row["status"], row["error_code"]) == ("needs_review", "fit_disagreement") and "would apply (90%)" in row["error_message"]
        assert store.is_already_submitted("https://boards.greenhouse.io/acme/jobs/g1") is True                 # parked: nothing retries it until you decide
        assert "python fit_review.py list" in out
        fit = json.loads((tmp / "applications" / "g1" / "fit.json").read_text(encoding="utf-8"))
        assert fit["review"]["agreement"] == "disputed" and fit["review"]["model"] == "review-m"                # the audit trail has both opinions
        store.close()
    with tmp_cwd() as tmp:
        store, out = run_outcome(Models(**{"judge-m": decisive(), "review-m": LLMError("refused")}), tmp)
        row = app_row(store, "g1")
        assert (row["status"], row["error_code"], row["attempts"]) == ("skipped", None, 0)                      # postponed: retryable and free
        assert store.is_already_submitted("https://boards.greenhouse.io/acme/jobs/g1") is False
        store.close()


# --------------------------------------------------------------------------- 6. fit_review.py

def seed(tmp):
    """A database with one posting in each state `fit_review list` must show, and one it must not."""
    store = StateStore(tmp / "state.db")
    (tmp / "config.yaml").write_text(f'daemon:\n  state_db: "{(tmp / "state.db").as_posix()}"\n', encoding="utf-8")
    rid = store.start_run("stage3", "h", "p")

    def add(job_id, title, status, code, message, payload=None):
        save_job(store, rid, job_id, f"https://boards.greenhouse.io/acme/jobs/{job_id}", title=title, company="Acme")
        store.conn.execute(
            "INSERT INTO applications(job_id, run_id, status, mode, ats, started_at, finished_at, error_code, error_message, attempts) VALUES (?,?,?,?,?,?,?,?,?,1)",
            (job_id, rid, status, "autonomous", "greenhouse", "2026-10-30T10:00:00+00:00", "2026-10-30T10:01:00+00:00", code, message),
        )
        if payload:
            store.put_fit(job_id, "k", payload["fit"], "skip", json.dumps(payload), "judge-m")
        store.conn.commit()

    base = {"seniority_match": "match", "missing_must_haves": [], "summary": "good match"}
    add("conf1234", "Confirmed Role", "skipped", "not_a_fit", "not a fit (60%): deal-breaker", {**base, "fit": 0.6, "deal_breakers": [VETO], "review": {"agreement": "confirmed", "model": "review-m", "verdict": "skip", "score": 0.2}})
    add("disp1234", "Disputed Role", "needs_review", "fit_disagreement", "the fit models disagree", {**base, "fit": 0.8, "deal_breakers": [VETO], "review": {"agreement": "disputed", "model": "review-m", "verdict": "apply", "score": 0.85, "summary": "fine"}})
    add("wait1234", "Waiting Role", "skipped", None, "fit veto not confirmed (no second opinion yet: x); will try again", {**base, "fit": 0.8, "deal_breakers": [VETO]})
    add("lone1234", "Lone Role", "skipped", "not_a_fit", "not a fit (30%)", {**base, "fit": 0.3, "deal_breakers": []})
    add("done1234", "Submitted Role", "submitted", None, None)
    add("capt1234", "Captcha Role", "skipped", None, "CAPTCHA encountered")                       # a skip that is not about fit
    return store


def cli(tmp, *args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = fit_review.main(["--config", str(tmp / "config.yaml"), *args])
    return code, out.getvalue()


def test_fit_review_lists_shows_approves_and_rejects():
    with tmp_cwd() as tmp:
        seed(tmp).close()
        code, text = cli(tmp, "list")
        assert code == 0
        for expected in ("Confirmed Role", "vetoed, confirmed by review-m", "Disputed Role", "models disagree: your call", "Waiting Role", "waiting for a second opinion", "Lone Role", "vetoed by one model"):
            assert expected in text, expected
        assert "Submitted Role" not in text and "Captcha Role" not in text                     # only fit decisions
        assert "clearance" in text and "“must be a US citizen" in text                      # the quoted reason is shown

        code, text = cli(tmp, "show", "disp")                                                     # a prefix is enough
        assert code == 0 and "Judge (judge-m): fit 80%" in text and "Second opinion (review-m): apply at 85%" in text and "deal-breaker: clearance" in text

        assert cli(tmp, "show", "zzz")[0] == 1
        code, text = cli(tmp, "approve", "d")                                                      # 'd' matches disp1234 and done...: only fit decisions are searched
        assert code == 0 and "Approved" in text

        store = StateStore(tmp / "state.db")
        assert store.get_fit_override("disp1234") == "apply"
        assert app_row(store, "disp1234") is None and store.is_already_submitted("https://boards.greenhouse.io/acme/jobs/disp1234") is False   # queued again
        assert app_row(store, "done1234")["status"] == "submitted"                                  # nothing else was touched
        store.close()

        code, text = cli(tmp, "reject", "wait")
        assert code == 0 and "Rejected" in text
        store = StateStore(tmp / "state.db")
        row = app_row(store, "wait1234")
        assert (row["status"], row["error_code"]) == ("skipped", "not_a_fit") and "you rejected" in row["error_message"] and store.get_fit_override("wait1234") == "skip"
        store.close()
        _, text = cli(tmp, "list")
        assert "Waiting Role" not in text and "Disputed Role" not in text                           # decided ones leave the to-do list
        assert "you rejected it" in cli(tmp, "list", "--all")[1]


def test_an_ambiguous_prefix_is_refused_rather_than_guessed():
    with tmp_cwd() as tmp:
        store = seed(tmp)
        save_job(store, store.start_run("stage3", "h", "p"), "conf9999", "https://boards.greenhouse.io/acme/jobs/conf9999", title="Another Confirmed")
        store.conn.execute("INSERT INTO applications(job_id, run_id, status, mode, started_at, error_code) VALUES ('conf9999', 1, 'skipped', 'autonomous', '2026-10-30T11:00:00+00:00', 'not_a_fit')")
        store.conn.commit()
        store.close()
        code, text = cli(tmp, "approve", "conf")
        assert code == 1 and "matches 2 postings" in text and "conf1234" in text and "conf9999" in text
        store = StateStore(tmp / "state.db")
        assert store.get_fit_override("conf1234") is None and app_row(store, "conf1234") is not None          # nothing was decided
        store.close()


def test_the_state_queries_touch_only_fit_decisions():
    with tmp_cwd() as tmp:
        store = seed(tmp)
        assert {r["job_id"] for r in store.list_fit_reviews()} == {"conf1234", "disp1234", "wait1234", "lone1234"}
        assert store.requeue_after_fit_decision("done1234") is False and app_row(store, "done1234") is not None   # a submission is never forgotten
        assert store.requeue_after_fit_decision("capt1234") is False                                                 # nor a skip that is not about fit
        assert store.requeue_after_fit_decision("lone1234") is True and app_row(store, "lone1234") is None
        assert store.finalize_fit_skip("disp1234", "you rejected") is True and app_row(store, "disp1234")["status"] == "skipped"
        store.close()
