# AI Auto Job Applier

A job-application assistant that runs **entirely on your own computer**. It finds postings, decides which ones
suit you, fills in the application forms, and (once you trust it) submits them. It runs as a background daemon
until you stop it or shut the PC down.

There is no cloud AI behind it. The language model is a small open model (default: `qwen2.5:7b`) served locally
by [Ollama](https://ollama.com), and everything that matters for safety is decided by ordinary code, not by the model.

> **Status:** MVP. The whole loop works end to end: find, filter, check fit, answer, fill in, submit, verify, and
> hand you whatever needs a person. The pipeline, the answer engine, the agent and the safety rails are covered by
> 490 automated tests, but a brand-new install has no application history, so **every kind of site starts on
> probation** (filled in and reviewed, not submitted) until you approve it. See [What the MVP includes](#what-the-mvp-includes)
> for exactly what is in and what is not, and the [Safety model](#safety-model).
> Setup instructions are in **[SETUP.md](SETUP.md)**.

---

## Why this exists

Most "AI job apply" tools are a fixed script per job board, wrapped around a paid cloud model. They break the
moment a company uses a different application system, and they send your résumé and personal details to a
third party.

This project takes the opposite approach:

- **Local first.** One 7B-class model on an ordinary gaming GPU (8 GB VRAM is enough) or a CPU. Your résumé, facts
  and answers never leave the machine.
- **Dynamic, not scripted.** Dedicated handlers exist for the big platforms, but for *any other* site an
  application agent reads the page, works out what it is looking at, and clicks through the steps.
- **Boringly safe.** The model may only choose between options that code has already vetted. It never invents
  answers, never writes selectors, and never gets to press the final button on its own authority.
- **Unattended.** It is built to be left running: crash recovery, graceful shutdown, rate limits, duplicate
  protection and a full audit trail.

## How it works

```
 ┌──────────────┐    ┌──────────────────┐    ┌──────────────────────────────────────┐
 │  Stage 1     │    │  Stage 2         │    │  Stage 3                             │
 │  Find jobs   │ →  │  Filter & rank   │ →  │  Apply                               │
 │              │    │                  │    │                                      │
 │ Greenhouse   │    │ dedupe, scams,   │    │ fit check (local model) → open form  │
 │ Lever        │    │ exclusions,      │    │ → answer questions from your facts   │
 │ LinkedIn     │    │ salary floor     │    │ → upload résumé → probation / dry    │
 │ Handshake    │    │ title relevance, │    │ run gate → submit → verify the page  │
 │ (browser)    │    │ skill score      │    │ really confirmed it                  │
 └──────────────┘    └──────────────────┘    └──────────────────────────────────────┘
          every cycle (default: hourly)  ·  state kept in SQLite (state.db)
```

1. **Stage 1: ingestion.** Public Greenhouse and Lever boards are read through their APIs; LinkedIn and Handshake
   are read through a real browser that reuses your logged-in session.
2. **Stage 2: filtering.** Cheap, explainable rules: duplicates, stale postings, scam phrases, your excluded
   companies and keywords, minimum salary, title relevance (field and seniority), a skill-match score and a
   **location filter** that drops postings you could not take up (remote only in other countries, on-site where you
   do not live, "must reside in X"). No model is needed here; what it is unsure about it keeps for the fit check.
3. **Stage 3: applying.** For the few jobs that survive (at most the daily cap), a local-model **fit check** reads
   the real posting text, the form is opened and filled, and the result is verified against what the page says.

## What the MVP includes

The loop below is built, tested and run by the daemon today. Each row points at the section that explains it.

### Features

| Area | What you get | Details |
|---|---|---|
| **Job discovery** | Public Greenhouse and Lever boards through their APIs (no login). LinkedIn, Handshake and any other URL through a browser that keeps its own logged-in profile. Optional cookie import from your Firefox. | [Job discovery](#job-discovery) |
| **Filtering** | Duplicates across runs, jobs already applied to, expired postings, scam phrases, excluded companies and keywords, salary floor, title relevance (field and seniority) and a skill-match score. Plain rules, no model, every drop explained in the log. | [Filtering and fit](#filtering-and-fit) |
| **Location filter** | Drops postings you could not take up (remote only elsewhere, on-site where you do not live, "must reside in X"), reads a work mode written in the title ("... - Hybrid"), and keeps whatever it cannot read for the fit check. | [Filtering and fit](#filtering-and-fit) |
| **Fit check** | A local model reads the real posting. A deal-breaker only counts if it can be quoted word for word, your own facts waive vetoes they already answer, a second model can confirm the rest, and you can overrule any veto. | [Filtering and fit](#filtering-and-fit) |
| **Source of truth** | Your resume versions in `me/` become years per technology, highest finished degree, GPA, first-degree year and employer counts, each with where it came from. Contradictions between versions are reported; `facts.yaml` always wins. | [Your source of truth](#your-source-of-truth) |
| **Answering questions** | Only from `facts.yaml`, your saved answers and your own resumes and notes (quoted, checked, then confirmed by a second model call). Consent boxes are ticked, marketing opt-ins never; a few questions are never answered for you; a required question nobody can answer parks the job and is listed once. | [Answering application questions](#answering-application-questions) |
| **Resume per job** | If you keep `resume-frontend.pdf` beside `resume-base.pdf`, the version that fits the job title is attached; anything unclear gets your default. | [Your source of truth](#your-source-of-truth) |
| **Application engines** | Dedicated handlers for Greenhouse, Lever, Workday, LinkedIn Easy Apply and Handshake Quick Apply. For every other site an observe, decide, act, verify agent, which also takes over when a handler finds nothing it knows. A trace with a screenshot per step, and a failure picture when something goes wrong. | [Handling any site](#handling-any-site-the-application-agent) |
| **Several local models** | Each job (fit, form questions, essays, verification, buttons) can go to the installed model that suits it. A missing or failing model falls back to the main one, and a model that keeps failing is left alone for a while. | [Several local models](#several-local-models-optional) |
| **Safety rails** | Probation per kind of site, dry run that always wins, a daily cap and per-site limits, duplicate protection, submission only on page evidence, a second look at applications that were never confirmed, crash recovery and an audit trail. | [Safety and reliability](#safety-and-reliability) |
| **Dashboards** | A terminal dashboard, a web page for everything that needs you (answer, decide, trust a site, finish by hand), a one-screen pending list, and a desk that tiles them with the daemon log. Both dashboards warn when a model has been stopped or when you edited the config after the daemon loaded it. | [Interfaces](#interfaces) |
| **Control** | Commands for the daemon, answers, site trust, fit vetoes and your source of truth; a local REST API; an optional Langflow pipeline. | [Everyday commands](#everyday-commands) |
| **Running it** | `setup.bat -All` starts everything and opens it; `-All -Background` starts it silently; `-EnableDesk` does it at every Windows login. Your settings live in `config.local.yaml`, so updating the project never touches them. | [Quick start](#quick-start) |

### Properties

| Property | What it means |
|---|---|
| **Local only** | No cloud AI, no accounts, no API keys. The model runs in Ollama on this computer; the only outbound traffic is to the job sites. |
| **Private by default** | Your profile, facts, resumes, database, sessions and token are git-ignored (a test fails if one is tracked). The REST API, the web interface and the dashboards answer on this computer only; the API needs a token and the web page a per-run one. |
| **Grounded** | Nothing is guessed. Every answer comes from a source you own, and the application records which one (fact sheet, saved answer, computed from a dated resume, or the file and line a draft was quoted from). |
| **The model never decides alone** | It chooses only among options code already judged safe, a veto needs a quote that exists in the posting, a button choice must survive several menu orders, and code, not the model, classifies buttons and decides whether an application went through. |
| **Fails safe** | Unknown means parked, never guessed. A site starts on probation, a dry run beats every other setting, a model outage skips a job for now without using up an attempt, and an application that was clicked but not confirmed is never retried. |
| **Never twice** | Duplicates are caught across cycles and restarts; `unconfirmed` is never sent again. LinkedIn and Handshake are asked a second time whether you applied; elsewhere it stays until you decide. |
| **Auditable** | A folder per job (screenshots, answers with sources, fit verdict, agent trace), a SQLite history of runs, attempts and events, and a log line with the reason for every drop. |
| **Unattended** | Single-instance lock, graceful shutdown, interrupted work parked for review, an Ollama wait at login, circuit breaker for failing models, and a status file the dashboards read to tell you when it needs a restart. |
| **Modest hardware** | Works with one 7B model on 8 GB of VRAM (about 3 seconds per short call) or on a CPU, inside Ollama's default 2048-token window. |
| **Yours to configure** | Defaults in `config.yaml`, your own changes in `config.local.yaml`; per-site, per-model, per-filter switches; MIT licensed. |
| **Tested** | 490 tests, including real browsers driven against a fake application server. They need neither Ollama nor an internet connection. |
| **Platform** | Windows 11 and Python 3.14. The Python core is portable; the launcher, desk and login entry are Windows-only. |

### Not in the MVP yet

| Left out | Why, and what to do meanwhile |
|---|---|
| Recruiter outreach | Built but **off** (`outreach.enabled: false`). A draft-and-approve queue, so that nothing is ever sent without your OK, comes first. |
| CAPTCHAs, new accounts, email codes | Never, by design: those jobs are skipped or parked for you. |
| Writing your resume or cover letters | The cover letter is a template in `profile.json`. |
| A launcher outside Windows | Run `python daemon.py` and the commands in [Everyday commands](#everyday-commands) directly. |

## Features

### Job discovery
- Greenhouse and Lever boards (no login required), LinkedIn and Handshake searches (browser, persistent session).
- Optional cookie import from your own Firefox profile so the bot starts out logged in.

### Filtering and fit
- Hard filters: duplicates (across runs), jobs already applied to, expired postings, scam heuristics,
  company/keyword exclusions, salary floor, title relevance (your field and seniority) and a skill-match score.
- **Location filter** (`stage2.location`), driven by your `facts.yaml`: where you live, which countries you may work in
  and whether you would relocate. It drops "Remote - Spain", "London" or "Austin, TX (Hybrid)" and postings whose text
  says "must reside in the United States" before any model reads them, and keeps anything it cannot read (a bare
  "Remote", an unknown city) for the fit check. A work mode written at the end of the title ("Backend Engineer - Hybrid",
  "Engineer (Remote)") counts like one in the location field. Every cycle's log lists a few drops so a wrong one is easy to spot.
- **Model-based fit scoring** on the actual posting text, cached per posting/profile/model. It judges what the
  cheap rules cannot, such as location, remote policy and work authorization, against your facts. A "deal-breaker" only
  vetoes a job if the model can quote it **verbatim** from the posting, so a small model cannot invent a reason
  to skip a job.
- **Vetoes are double-checked before they are final.** A deal-breaker is the one verdict that overrides a good score,
  and small models get it wrong (for example vetoing "US work authorization required" for someone who has it). So code
  first waives any veto your own facts already answer; then, if a `fit_review` model is configured, a *different* model
  judges the posting independently: both skip, the skip is final; it would apply, the job is parked for you rather
  than skipped or applied to. `python fit_review.py list` (or the dashboard's `v` key) shows every veto and lets you
  overturn it.

### Your source of truth
- Put your resume versions (and notes about your work) in `me/` (never committed; `python source.py sync` copies them from your
  resume-template folder). Markdown, HTML, text, Word (`.docx`) and PDF are all read; a PDF or Word file that sits beside a text version
  of the same name is that version's printout and is not read twice. A scan, a locked PDF or a file with no text is reported, never guessed at. **Years of experience are computed from the dates next to each job**, a framework implies its language
  (Laravel -> PHP, Vue -> JavaScript), and every number records where it came from. `python source.py show` prints them,
  `python source.py check` looks for contradictions between your versions.
- **Education and employers come from the same dates**: your highest *finished* degree (one in progress is not it), its school, field and
  GPA, when your first bachelor's degree ended, and how many different companies you worked for in the last ten years / since that degree.
  Where your versions disagree (a different field of study, another GPA) nothing is claimed and `check` says so: state it in `facts.yaml`.
- If you keep several PDF versions of your resume (`resume-frontend.pdf` beside `resume-base.pdf`), the one that fits the job is attached: the title decides
  ("Frontend Engineer"), the posting text only when it clearly leans one way, and anything unclear ("Full Stack", no PDF for that kind) gets the default.
  `stage3.resume_per_job: false` turns it off; `attachments.resumes` / `attachments.resume_rules` in `profile.json` list your own versions and words.
- A technology your resumes never show is never claimed, and the career total is never offered for a question about a specific tool.
  What you write in `facts.yaml` always wins over what is computed.
- Questions that were parked for a missing fact are re-checked every run: add the fact (or fix a resume) and those jobs are queued again.

### Answering application questions
- Answers come only from **`facts.yaml`** (a file you own and edit), your contact details in `profile.json`, and
  answers you saved with `answers.py`. Yes/no and numeric questions (work authorization, sponsorship, years of
  experience with a skill, salary, notice period…) are answered from those facts; nothing is guessed.
- If a *required* question cannot be answered, the job is **parked as `needs_review`** and listed by
  `python answers.py pending`. Answer it once (`answers.py set`) and it is used for all future applications.
- Consent boxes (privacy policy, terms, "my information is accurate") can be ticked automatically; marketing
  opt-ins never are.
- **When the fact sheet does not say, your own words do.** A question no rule covers ("what do you use to store tracking data?",
  "tell us about something you built") is tried against the lines of your resumes and notes (`me/`): a local model drafts an
  answer **quoting** the lines it relied on, code checks that every quote is word for word in your files and that the answer
  adds no link, number, tool or name they do not contain, and a second model call confirms every claim. A short answer that
  passes is filled in (the audit trail names the file and the line). An essay is **proposed, not filled in**, unless you set
  `answers.open_ended: generate`; `python answers.py pending` shows each draft with its sources, and
  `python answers.py approve "<question>"` accepts it (an answer that names the company is saved for that company only).
- Some questions are never answered for you, whatever your files say: whether you used AI for the application, citizenship and
  identity numbers, signatures, dismissals and legal disclosures, security clearances, criminal history.

### Handling any site: the application agent
- Dedicated handlers: **Greenhouse, Lever, Workday, LinkedIn Easy Apply, Handshake Quick Apply**.
- For everything else (and for "apply on the company site" links from LinkedIn/Handshake) an
  **observe → decide → act → verify** agent takes over. It works in new tabs, iframes and shadow DOM, handles
  multi-step wizards, declines cookie banners, uploads your résumé (into a file field, or through an "Upload resume" button or drop zone that opens a file picker),
  and notices when a site rejected a step.
- The same agent **takes over from a dedicated handler** that finds nothing it knows how to do on a page (LinkedIn or Handshake
  with no apply button it recognises). It reads the page: a closed posting is recorded as closed, not as a failure; a link the
  script had no selector for is followed; a sign-in wall or a CAPTCHA is named. `agent.takeover: false` turns this off.
- Plain rules decide almost every step. The model is asked only about an unusual button (for example
  "Let's go!"), only chooses from a menu of buttons the code already judged safe, and is asked in several menu
  orders; if its answer changes with the order it is guessing and the agent stops instead of acting.
- Every run leaves a **step-by-step trace with a screenshot per step** in `applications/<job>/trace/`.

### Several local models (optional)
- One model by default; with a `models:` block in `config.yaml` each job (judging fit, mapping form questions,
  drafting essays, choosing buttons) can go to the installed model that is best at it.
- The agent's votes can come from different models, and they must all agree, so one model's blind spot cannot decide
  alone.
- A missing or failing model never leaves a job worse off: its work falls back to the main model.

### Safety and reliability
- **Probation per site type**: a new ATS platform or company domain is filled in and reviewed, never submitted,
  until you approve it, it completes a few clean runs, or one application there is confirmed.
- **Dry run is authoritative**: in dry-run mode nothing is ever submitted, whatever else is configured.
- **Duplicate protection**: a job is never applied to twice. An application that was clicked but not confirmed is
  recorded as `unconfirmed` and is **never** retried automatically.
- **A second look at `unconfirmed` applications**: LinkedIn and Handshake remember what you applied to, so after each cycle
  the bot opens the oldest ones and, when the posting now says you applied ("Applied 2 days ago", "See application
  status"), records them as `submitted`. It only reads the page (no click, no form, no CAPTCHA), believes nothing from a
  sign-in page or another posting, looks a few times a day apart and then leaves it to you. See `python reverify.py status`.
- **Rate limits**: a daily cap (default 25), a per-domain hourly cap and randomised delays.
- **Verification**: "submitted" requires evidence from the page (a confirmation message or URL), not just a click.
- **Crash-safe**: single-instance lock, graceful shutdown on Ctrl+C / system shutdown, interrupted applications
  are parked for review rather than guessed at.
- **Audit trail**: SQLite database of runs, attempts and events, plus a per-job folder of screenshots and answers.

### Interfaces
- **Terminal dashboard** (`tui.bat`): at-a-glance numbers (submitted today and against the daily cap, outcomes, the
  latest cycle's funnel, which models are loaded), the job being worked on right now with its step-by-step agent flow,
  recent applications you can open to see the fit verdict, answers and every step with who decided it, plus questions
  waiting for you and the trust state of each site. Read-only: it only watches what the daemon writes.
- **Web interface** (`gui.bat`, http://127.0.0.1:8765/): everything that needs a person on one page. Answer the questions the
  forms asked (a draft from your own resumes is already in the box, with the lines it rests on), settle fit decisions the models
  disagreed about, look at the filled-in forms of a site on probation and trust it, and finish by hand the jobs that send you to
  the company's own site. Every button does what the matching command does; nothing is submitted from the page. Listens on this
  computer only.
- **Pending list** (`python pending.py`): one screen of everything only you can do, each item with the command or link for it.
- **The desk** (`setup.bat -Desk`, or `setup.bat -All`): the dashboard, the pending list and the daemon log tiled in one Windows
  Terminal window, with the web interface and Langflow opening in your browser. `-EnableDesk` does it at every Windows login.
- **Health warnings** in both dashboards: the daemon writes a small `status.json` (its process id, the config's path, model names and a fingerprint per config section; no personal data), and the dashboards use it to
  say when a model has been stopped for repeated failures (its jobs use the main model meanwhile) and when `config.yaml` or
  `config.local.yaml` was edited after the daemon loaded it, which needs a restart to take effect.
- **CLI** for the daemon, answers, site trust, fit vetoes and your source of truth.
- **Local REST API** (loopback only, bearer-token protected) for profile, status, scrape/filter/apply.
- **Langflow** visual pipeline (optional) with ready-made components.
- **Windows helper** (`setup.ps1`) to start/stop services, open the desk and auto-start it at login.

## Safety model

The model is an assistant, not a decision-maker. Responsibility is split like this:

| Decision | Who makes it |
|---|---|
| Is this posting in my field and seniority? | Rules (`stage2_filter`) |
| Does this posting suit me? Any deal-breakers? | Local model, but a veto needs a verbatim quote that exists in the text |
| What do I answer to this question? | Your `facts.yaml` and saved answers. No fact, no answer, job parked |
| Which button is "Next" / "Submit" / dangerous? | Code (`page_state.classify_action`) |
| Which of several *safe* buttons on an odd page? | Model, only if it agrees with itself across menu orders |
| May this site be submitted to unattended? | You (probation → `sites.py approve`) |
| Did the application actually go through? | Code, from page evidence |

Things the agent will **never** do: click delete / sign-up / share / download / sign-out style buttons, follow
links to unrelated sites, create accounts, sign in with credentials it was not given, solve CAPTCHAs, or act on
instructions found in page text.

## What it is not

- It does not pass CAPTCHAs, bypass bot detection, create accounts or read email verification codes. Those jobs
  are skipped or parked for you. It stops for a CAPTCHA only when a person would have to act on one (a checkbox or
  puzzle on screen), not for the invisible reCAPTCHA that many forms load silently, and a skip records what it saw
  (for example `recaptcha checkbox 304x78`) so a wrong one can be audited.
- It does not write your résumé or cover letters (the cover letter is a template in `profile.json`).
- It is not a guarantee of interviews. It removes the repetitive part so you can spend time on the applications
  that matter.

## Requirements

- Windows 11 (the Python core is portable, the `setup.ps1` helpers are Windows-only)
- Python 3.14 (developed and tested on 3.14.1)
- [Ollama](https://ollama.com) with a small instruct model (default `qwen2.5:7b`, about 4.7 GB)
- 8 GB VRAM or CPU-only with patience; 16 GB RAM recommended; a few GB of disk for browsers and the model
- Playwright browsers (Firefox and/or Chromium), installed by the setup steps

## Quick start

Full instructions, including the files you must create first, are in **[SETUP.md](SETUP.md)**. In short:

```powershell
python -m pip install -r requirements.txt
python -m playwright install firefox chromium
ollama pull qwen2.5:7b
python profile_gen.py --resume my-resume.md --location "City, Country" --authorized XX --min-salary 60000 --seniority mid --remote yes
python answers.py facts-init       # then fill in facts.yaml (see SETUP.md)
python daemon.py --once --dry-run
```

Look in `applications/<job>/` for what it would have submitted. When you are happy with a site:

```powershell
python sites.py list
python sites.py approve greenhouse
```

Then run it for real: `setup.bat -All` starts everything and opens the dashboard, the web interface and Langflow; `setup.bat -All -Background` starts the same services silently; `python daemon.py` runs only the daemon.

(In PowerShell write `./setup.bat`, `./gui.bat` and `./tui.bat`: PowerShell does not run a program from the current folder by its bare name.)

## Everyday commands

| Command | What it does |
|---|---|
| `python daemon.py` | Run the daemon (one cycle per `daemon.interval_seconds`) |
| `python daemon.py --once --dry-run` | One cycle, nothing submitted |
| `python daemon.py --login <url>` | Open a visible browser to log in to a site once |
| `python daemon.py --sync-cookies` | Import cookies from your personal Firefox profile |
| `python answers.py pending` | Required questions that blocked applications |
| `python answers.py set "<question>" "<answer>" --requeue` | Answer once for all future applications |
| `python answers.py approve "<question>" [--text "..."] [--all-verified] --requeue` | Accept the draft the bot wrote from your resumes (or your own wording) |
| `python answers.py facts-init` | Write a starter `facts.yaml` from your profile (never overwrites) |
| `python reverify.py status` / `run` | Which `unconfirmed` applications are looked at again and what the last look found; `run` looks now (daemon stopped) |
| `python sites.py list` / `approve <site>` / `revoke <site>` | Manage which sites may be submitted to |
| `python fit_review.py list` / `show` / `approve <job>` / `reject <job>` | See postings the models vetoed or disagreed about, and overrule them |
| `python source.py init` / `sync` / `show` / `check` / `derive` | Create `me/`, copy your resumes into it, see the years, degrees and employer counts the bot computes from them and why, find contradictions, recompute now |
| `python pending.py [--watch 60]` | One screen of everything waiting for you, with the command or link for each |
| `gui.bat` | The web interface for what needs you: answer, decide, finish by hand (`--demo` for made-up data, `--port N`) |
| `setup.bat -All` | Start the daemon, web interface, REST API and Langflow, and open the desk (the same as `-Desk`; `-NoGui` skips the browser) |
| `setup.bat -All -Background` | The same services, silently, with nothing opened |
| `setup.bat -Desk` / `-EnableDesk` | Open dashboard + pending list + daemon log in one terminal window, and the web interface and Langflow in your browser, now / at every Windows login (`-NoGui` skips the browser) |
| `tui.bat` | Live terminal dashboard (`--demo` for made-up data, `--once` to print a text snapshot) |
| `python api.py --port 8000` | Local REST API |
| `setup.bat -Status` / `-StopAll` | Show daemon / web interface / API / Langflow state, or stop them all |
| `python -m pytest tests -q` | Run the test suite |

## Project layout

| Path | Purpose |
|---|---|
| `daemon.py`, `daemon_status.py` | The loop (scheduling, locking, signals, stage 1→2→3 per cycle), and the `status.json` it writes for the dashboards |
| `scraper.py`, `job_text.py` | Greenhouse / Lever / browser ingestion and text cleanup |
| `stage2_filter/` | Normalise, dedupe, filter, relevance, rank |
| `stage3_apply/` | Application runner, rate limiter, confirmation checks |
| `stage3_apply/ats_handlers/` | Greenhouse, Lever, Workday, LinkedIn, Handshake and the agent handler |
| `agent.py`, `page_state.py` | The application agent and the page/button classifier |
| `form_questions.py`, `answerer.py`, `facts.py`, `answers.py` | Reading form fields and answering from your facts |
| `evidence.py` | Answers drafted from your own resumes and notes: quoted, checked in code, confirmed by a second model call |
| `resume_pick.py` | Which resume version goes with which job |
| `reverify.py` | The second look at applications that were sent but never confirmed |
| `llm_client.py` | The local-model client: roles, fallbacks, the circuit breaker for failing models |
| `state.py`, `schema.sql`, `sites.py` | SQLite state, migrations, site trust (probation) |
| `fit.py`, `fit_review.py` | Model fit scoring (facts check, second opinion) and the command to overrule it |
| `api.py` | Local REST API |
| `gui/`, `gui.bat` | The web interface for what needs a person (standard library only: `service.py` is what the buttons do, `server.py` the local HTTP side, `static/index.html` the page) |
| `tui/`, `tui.bat` | Read-only terminal dashboard (data and rendering use only the standard library and `rich`; the Textual app runs in its own `.venv-tui`) |
| `profile_gen.py` | Builds `profile.json` from your resume; leaves empty whatever the resume does not state |
| `config.yaml`, `config_loader.py` | The shared defaults, and the loader that merges your own `config.local.yaml` (never committed) over them |
| `resume_facts.py`, `source.py` | Reads the resumes in `me/` and computes years per technology, degrees, GPA and employer counts with provenance; the command to sync / show / check them |
| `pending.py`, `desk.ps1` | The "what is waiting for you" screen, and the Windows Terminal layout that shows it with the dashboard and the daemon log |
| `langflow_components.py`, `run_langflow.py`, `auto_applier_flow.json` | Optional visual pipeline |
| `outreach.py` | Recruiter outreach (disabled by default) |
| `import_profile.py`, `backfill_history.py` | Cookie import from your Firefox profile; a one-off repair of application history, kept for reference |
| `stage1_generate/` | Experimental sandboxed scraper-code generator (not part of the daemon loop) |
| `applier.py`, `store.py` | The original standalone prototype, kept for reference |
| `tests/` | Test suite, including a fake application server driven by real browsers |
| `setup.ps1`, `setup.bat` | Windows launcher: install, start/stop services, auto-start |

## Privacy and responsible use

- All processing is local. The only network traffic is to the job sites themselves (and a one-time model download).
- These files contain personal data or live sessions; keep them private and out of version control:
  `profile.json`, `facts.yaml`, `config.local.yaml`, `me/`, `state.db*`, `applications/`, `.browser_profile/`, `.api_token`, `daemon.log`.
  The included `.gitignore` already excludes them (plus `jobs.json`, `*.pid` and other runtime output). If you
  ever staged them before adding it, unstage with `git rm -r --cached <path>`; if you already committed or pushed
  them, treat the browser session and API token as leaked and rotate them.
- Automated applications can breach the terms of some platforms (LinkedIn in particular). You are responsible for
  how you use this tool; start with dry runs, keep the caps low and review what it sends.

## Documentation

- **[SETUP.md](SETUP.md)** for installation, configuration, first run, running as a service, the dashboards, tuning and troubleshooting.
- **[What the MVP includes](#what-the-mvp-includes)** above is the list of what is built, the properties it keeps, and what is left out.
- `config.yaml` is commented inline; every option is explained next to its value. Change settings in `config.local.yaml`, not in `config.yaml`.

## License

MIT, see [LICENSE](LICENSE). Use it, change it, share it; it comes with no warranty, and you are responsible for what it sends in your name.
