# Setup Guide

How to install, configure and run the AI Auto Job Applier. For what the project is and why it behaves the way it
does, read the [README](README.md) first.

**Contents**
1. [Requirements](#1-requirements)
2. [Install](#2-install)
3. [Create your profile and fact sheet](#3-create-your-profile-and-fact-sheet)
4. [Configure](#4-configure)
5. [Log in to the sites you use](#5-log-in-to-the-sites-you-use)
6. [First run: a dry run](#6-first-run-a-dry-run)
7. [Review, approve, go live](#7-review-approve-go-live)
8. [Running it as a service](#8-running-it-as-a-service)
9. [REST API and Langflow](#9-rest-api-and-langflow-optional)
10. [Tuning for your hardware](#10-tuning-for-your-hardware)
11. [Files it creates](#11-files-it-creates)
12. [Testing](#12-testing)
13. [Troubleshooting](#13-troubleshooting)

---

## 1. Requirements

| Need | Details |
|---|---|
| OS | Windows 11. The Python code is portable; `setup.ps1` / `setup.bat` and the auto-start helper are Windows-only |
| Python | 3.14 (developed and tested on 3.14.1; other versions are untested) |
| Ollama | <https://ollama.com>, serving a small instruct model. Default: `qwen2.5:7b` (about 4.7 GB) |
| GPU / CPU | 8 GB VRAM fits the default model fully on the GPU (about 3 s per short call). CPU-only works but is slower |
| RAM / disk | 16 GB RAM recommended; a few GB for the model and the Playwright browsers |
| Browsers | Playwright's Firefox and/or Chromium (installed below) |

Everything runs locally. No account or API key is needed for the AI part.

## 2. Install

Open PowerShell in the project folder.

**Python packages**

```powershell
python -m pip install -r requirements.txt
```

`requirements.txt` includes `langflow`, which is large and only needed for the optional visual pipeline
([section 9](#9-rest-api-and-langflow-optional)). If you do not want it, install just the core:

```powershell
python -m pip install playwright pyyaml pytest
```

**Browsers**

```powershell
python -m playwright install firefox chromium
```

**Local model**

```powershell
ollama pull qwen2.5:7b
```

Check that Ollama is serving it:

```powershell
curl http://127.0.0.1:11434/api/tags
```

Ollama's default context window is only 2048 tokens. The project works inside that, but longer postings and forms
get trimmed. For better results raise it on **both** sides ([section 10](#10-tuning-for-your-hardware)).

## 3. Create your profile and fact sheet

You write two files. They are the *only* things the bot is allowed to say about you.

### 3.1 `profile.json`: who you are and what you want

> **Note:** `profile_gen.py` (which `setup.ps1` runs when `profile.json` is missing) builds a profile from a résumé
> folder laid out as `resume-versions/resume-backend.md` (an HTML-flavoured Markdown résumé) plus
> `out/resume-base.pdf`. It looks for that folder in `./resume-template`, or wherever the `AUTO_APPLIER_RESUME_DIR`
> environment variable points. Anything the résumé does not state (location, work authorization, excluded
> companies, GitHub link) is left empty for you to fill in. Unless your résumé uses that exact layout, it is simpler
> to create `profile.json` by hand as below.

Create `profile.json` in the project folder:

```json
{
  "schema_version": "2.0",
  "name": "Ada Lovelace",
  "first_name": "Ada",
  "last_name": "Lovelace",
  "email": "ada@example.com",
  "phone": "+15551234567",
  "location": "London, UK / Remote",
  "remote_ok": true,
  "work_authorization": ["GB", "US"],
  "min_salary": 90000,
  "currency": "USD",
  "seniority": "senior",
  "skills": ["Python", "PostgreSQL", "Docker", "API Design", "TypeScript"],
  "linkedin": "https://www.linkedin.com/in/your-handle/",
  "portfolio": "https://example.com",
  "github": "https://github.com/your-handle",
  "exclusions": {
    "companies": ["Some Company I Won't Work For"],
    "keywords": ["unpaid", "commission-only", "clearance required"],
    "domains": []
  },
  "attachments": {
    "resume_path": "C:\\Users\\you\\Documents\\resume.pdf",
    "cover_template": "Dear Hiring Team,\n\nI am excited to apply...\n\nBest regards,\nAda Lovelace",
    "portfolio_url": "https://example.com"
  },
  "mode": "autonomous"
}
```

| Field | Meaning |
|---|---|
| `name`, `first_name`, `last_name`, `email`, `phone`, `linkedin`, `portfolio`, `github` | Contact details typed into application forms |
| `location`, `remote_ok`, `work_authorization` | Used by the model fit check and as answers to related questions (list of country codes) |
| `min_salary`, `currency` | Postings that state a pay range clearly below this are dropped |
| `seniority` | `junior`, `mid`, `senior`, `staff`, `lead`, ... Senior profiles automatically skip junior/intern roles |
| `skills` | Drives the skill-match score in stage 2 |
| `exclusions.companies / keywords / domains` | Never apply to these |
| `attachments.resume_path` | Absolute path to the résumé PDF that gets uploaded. **Required**: a form that needs a file and has none configured is parked, never submitted without it |
| `attachments.cover_template` | Cover-letter text for forms that have a cover-letter box |
| `mode` | `autonomous`, `assisted` or `dry_run` (see below). Optional; falls back to `stage3.mode` in `config.yaml` |
| `max_applications_per_day`, `max_applications_per_domain_per_hour` | Optional overrides of the caps in `config.yaml` |

Optional keys for the relevance gate: `target_titles` (title words you want), `title_exclusions` (title words to
drop) and `exclude_junior_roles` (`true`/`false`, default depends on `seniority`).

**Modes.** `dry_run`: fill and review, never submit. `autonomous`: submit unattended. `assisted`: pause and ask
`Submit? [y/N/s]` in the terminal for each job (needs a foreground terminal, so it does not work as a hidden
background service). If **either** `profile.json` or `config.yaml` says `dry_run`, dry run wins; a missing or
unrecognised mode also means dry run.

### 3.2 `facts.yaml`: what the bot may say about you

Generate a starter file from your profile (it never overwrites an existing one):

```powershell
python answers.py facts-init
```

Then open `facts.yaml` and fill it in. Sections: `work_authorization`, `availability`, `compensation`,
`location`, `employment`, `experience` (including `years_by_skill`), `education`, `languages`, `demographics`,
`preferences` and `narrative`.

Rules the bot follows:

- It answers form questions **only** from this file, your contact details in `profile.json`, and answers you saved
  with `answers.py`.
- `null` means **not stated**. The bot does not pre-fill it and does not guess: a *required* question it cannot
  answer parks that job as `needs_review` and shows up in `python answers.py pending`.
- Nothing here is checked against reality. Only write what is true. Be especially careful with sponsorship,
  years of experience, salary and the `demographics` block (leave those `null` to never answer them).

## 4. Configure

`config.yaml` is commented inline. The settings you will most likely touch:

### Job sources: `stage1.target_urls`

```yaml
stage1:
  target_urls:
    - "https://boards.greenhouse.io/<company>"      # public board, read via API, no login
    - "https://jobs.lever.co/<company>"             # public board, read via API, no login
    - "https://www.linkedin.com/jobs/search/?keywords=backend%20engineer&f_WT=2"   # needs a logged-in browser
    - "https://app.joinhandshake.com/job-search/"   # needs a logged-in browser
  max_pages: 10
```

Greenhouse and Lever boards are the most reliable sources. A URL on those domains that does not name a company is
skipped. Any other URL is opened in the browser and scraped.

Two things to know about company boards:

* A posting's apply link can point at the company's own careers site (Dropbox and CircleCI do) instead of Greenhouse. The
  agent still handles it, but that site is a new kind of site for [probation](#72-approve-sites-probation): prepared, not
  submitted, until you approve it or it has been prepared cleanly a few times.
* `stage2.stale_posting_days` counts from the date a posting was **first published**, and some companies keep evergreen
  listings open for years (Canonical's median is over two years). The built-in default is 30 days, which dropped 83% of
  the postings on seven boards tried, so `config.yaml` uses 90. If a board you added still yields almost nothing, check
  the stage 2 line of the log for how many postings were dropped as expired.

### Where you can work: `stage2.location`

```yaml
stage2:
  location:
    enabled: true
    remote_authorized: keep   # "Remote - US" when you may work there but live elsewhere: keep | drop
    check_description: true   # read "must reside in the United States" style sentences
    always_keep: []           # words that keep a posting whatever its location says, e.g. ["lisbon"]
```

The filter removes postings you could not take up, before any model spends time on them. It reads **where you live**
(`location.country`, `location.city`), **where you may work** (`work_authorization.authorized_countries`) and **whether
you would relocate** (`location.willing_to_relocate`) from `facts.yaml`, with `profile.json` as the fallback. If it cannot
tell where you live it does nothing.

| Posting says | Result |
|---|---|
| Remote or based only in countries you cannot work from (`Remote Spain`, `London`, `Home based - EMEA`) | dropped |
| Anywhere, your region or country (`Home based - Worldwide`, `Remote - Americas`, `Remote - Mexico`) | kept |
| On-site or hybrid in a country you may work in but do not live in (`New York, NY`) | dropped, unless `willing_to_relocate: true` |
| Remote, tied to a country you may work in (`Remote - US`) | kept, or dropped with `remote_authorized: drop` |
| On-site or hybrid in another city of your own country (`Hybrid - Mexico City`) | dropped unless it is your city; a bare `Mexico City` (no mode stated) is kept |
| `Hybrid` / `In-Office` with no place named | dropped (you would have to be there, and nothing says where) |
| A bare `Remote`, `Distributed`, an unknown city or empty | kept: the fit check judges it |
| The text says you must live in the United States / Germany / ... and names no place you live | dropped, unless the text also names your country |

It only drops what it is sure about, and a bug in it keeps the posting rather than stopping a cycle. The daemon prints the
number dropped and a few examples each cycle (`[Stage 2]   not for you: ...`), and the dashboard shows the count. `willing_to_relocate`
left empty counts as "not stated", which is treated like no. If it wrongly drops a place you do want, add a word from the
location to `always_keep`. Postings the filter keeps still go through the fit check, which has the last say.

> `stage1.blocklist`, `stage1.robots_respect` and `stage1.sandbox` belong to the experimental
> `stage1_generate/` scraper generator. The daemon's own ingestion does not read them.

### How often and how much

```yaml
daemon:
  interval_seconds: 3600        # one cycle per hour

stage3:
  mode: "autonomous"            # autonomous | assisted | dry_run
  max_attempts: 3               # failed postings are retried at most this often; unconfirmed ones never
  rate_limit:
    global_per_day: 25          # rolling 24 h, counts real submissions only
    per_domain_per_hour: 5
    min_delay_seconds: 5        # random pause between applications
    max_delay_seconds: 15
```

### The model

```yaml
llm:
  endpoint: "http://127.0.0.1:11434/v1"
  model: "qwen2.5:7b"
  context_tokens: 2048          # prompt budget; see "Tuning for your hardware"
  timeout_seconds: 60
```

### Using more than one model (optional)

By default one model does every job. If you have several models installed you can give each job to the one that is
best at it. This is a `models:` block in `config.yaml`, commented out in the shipped file:

```yaml
models:
  roles:                                  # a role = a model, plus any overrides of the llm block above
    judge: {model: "qwen2.5:7b"}
    pilot: {model: "qwen2.5-coder:7b", context_tokens: 4096}
  tasks:                                  # which role does which job
    fit: judge
    questions: judge
    agent: [judge, pilot]                 # one vote per entry, in turn
```

| Task | What the model does | Notes |
|---|---|---|
| `fit` | judges a posting against you | needs reading and judgement |
| `questions` | maps a form question to your facts | strict JSON; abstaining is the right answer when the facts do not say |
| `essay` | drafts a free-text answer (`answers.open_ended: generate`) | only used when you turn that on |
| `agent` | chooses between buttons on an unfamiliar page | a list of roles: vote *k* is cast by role *k* in menu order *k*, and the votes must still all agree |

A task you do not list uses the `llm` block, so you can adopt this one job at a time.

What happens when something is missing:

- At startup the daemon checks every role that a task uses and prints a `[Daemon] Models:` line for each. A role
  whose model is not installed is not used; its tasks use the `llm` model instead.
- If a role's model fails while running (not loaded, out of memory, prompt too big for its window), that one request
  is retried on the `llm` model. Adding a model therefore cannot leave a job worse off than before it existed.
- Fit verdicts are cached per model, so changing the `fit` model re-judges postings instead of reusing old verdicts.
- **A model that keeps failing is left alone.** After two requests in a row that get no answer at all (Ollama
  restarting, out of memory, swapped out), that model is not asked again for two minutes: callers fail at once instead
  of waiting out a timeout per job, and a role goes straight to the `llm` model. After the cool-down one trial request
  goes through; success resumes normal use. A reply that is merely bad (broken JSON) does not count. The daemon log says
  `[LLM] model '…' did not answer 2 requests in a row` once per outage, and `is answering again` when it recovers.
  Tune with `llm.breaker: {failures: 2, cooldown_seconds: 120}`; `breaker: false` turns it off.
- **A role can ask for free RAM.** `min_free_ram_gb: 5.5` on a role (for a big model that spills into system RAM) means
  it is used only while that much RAM is available, or if it is already loaded; otherwise its work goes to the `llm`
  model and the log says so now and then. An unreadable RAM figure never blocks a role.
- **The `fit_review` task** (`tasks: fit_review: heavy`) names the model that gives a second opinion on a deal-breaker
  veto ([section 7.4](#74-fit-vetoes-check-what-the-models-skipped)). It must be a different model from `fit`'s; with no
  `fit_review` role, or the same model, there is no second opinion. If that role cannot be used right now (model down,
  or short of the RAM `min_free_ram_gb` asks for) the veto is postponed, never confirmed by the judge alone.
- **An outage never costs a job its attempts.** With `fit.on_llm_error: skip`, a job whose fit could not be checked is
  skipped for now and tried again next cycle without using up one of its `max_attempts`, however long the outage lasts.

Things to know when you add models:

- **Context size.** Ollama's OpenAI endpoint cannot set the window per request. Bake it into the model with a
  Modelfile (`PARAMETER num_ctx 4096`) and put the same number in the role's `context_tokens`.
- **Importing a model file into Ollama** (`ollama create name -f Modelfile` with `FROM path\to\model.gguf`) does not
  always pick up the chat format. Check `ollama show name --modelfile`: if it says `TEMPLATE {{ .Prompt }}`, the model
  would receive your system and user text without role markers and answer badly. Copy the `TEMPLATE` of a model of
  the same family (`ollama show qwen2.5:7b --modelfile`) into your Modelfile.
- **Swapping costs time.** Ollama loads and unloads models on demand. Two models that do not fit in VRAM together
  take 10 to 30 seconds to swap, so give a single model as many of the jobs as you can.
- **Measure before you trust.** General-purpose and code-tuned models fail differently: one may refuse to act, another
  may answer confidently and wrongly, a third may break the JSON format on long answers. Try a role on your own
  postings and forms before giving it a job where a wrong answer costs something.

### Fit check, answers and the agent

```yaml
fit:
  enabled: true
  min_score: 0.5                # below this the job is skipped for good
  deal_breakers_veto: true
  on_llm_error: "skip"          # "skip": never apply unchecked; the job is retried next cycle | "allow": apply anyway if the model is down

answers:
  enabled: true
  accept_consents: true         # tick privacy/terms/accuracy boxes (never marketing opt-ins)
  open_ended: "review"          # "review": park essay questions | "generate": draft from facts.yaml narrative
  answer_optional: false

agent:
  enabled: true                 # false: sites without a dedicated handler are skipped
  max_steps: 30
  max_seconds: 360
  consensus: 2
  allowed_domains: []           # extra sites the agent may follow to
  probation:
    enabled: true
    clean_runs_to_trust: 3      # 0 = only your approval (or a confirmed submission) can trust a site
```

Leave `outreach.enabled: false`. Recruiter outreach is disabled until its detection is fixed.

## 5. Log in to the sites you use

LinkedIn and Handshake need a logged-in session. The bot uses its own persistent browser profile
(`.browser_profile/`). Two ways to give it a session:

**Log in once in the bot's browser** (recommended). A visible browser opens; log in, then press Enter in the
terminal to save the session:

```powershell
python daemon.py --login https://www.linkedin.com/login
```

Repeat for each site (for example `https://app.joinhandshake.com/login`). Use `--browser chromium` if you
configured Chromium.

**Or import cookies from your everyday Firefox profile:**

```powershell
python daemon.py --sync-cookies
```

With `browser.auto_sync_system_cookies: true` (the default) this also happens automatically each time the
daemon starts. Be aware that this copies live session cookies of your personal Firefox into `.browser_profile/`.

The bot never types passwords. If a site asks it to sign in mid-application, that job is parked.

## 6. First run: a dry run

Always start here. A dry run does everything except the final submit and **never counts against the daily cap**.

```powershell
python daemon.py --once --dry-run
```

At startup you should see a line like `[Daemon] LLM OK: ...`. If it says `WARNING`, fix Ollama first
([troubleshooting](#13-troubleshooting)). Then watch the three stages report: how many listings were scraped, how
many qualified, and what happened to each application.

Each job gets a folder, `applications/<job>/`:

| File | What it is |
|---|---|
| `form_snapshot.png` | Screenshot of the filled-in form, ready to submit |
| `answers.json` | Every question and the answer the bot chose, with its source |
| `fit.json` | The fit assessment: score, verdict, deal-breakers (with the quote) |
| `trace/` | For jobs the agent handled: `steps.jsonl` and one screenshot per step |
| `probation.json` | Written when a site is on probation: why it was held and how to release it |

Check that names, contact details, the résumé upload and the answers are right. Then look at the overall state:

```powershell
python answers.py pending
python sites.py list
python -c "import sqlite3; print(sqlite3.connect('state.db').execute('select status, count(*) from applications group by status').fetchall())"
```

## 7. Review, approve, go live

### 7.1 Answer anything it was missing

`python answers.py pending` lists the *required* questions that stopped applications, most-needed first. Answer one
and it is reused everywhere:

```powershell
python answers.py set "Are you willing to relocate?" "No" --requeue
```

`--requeue` puts the jobs that were only waiting on that answer back in the queue (only while the posting is still
listed). Other commands: `answers.py list`, `answers.py forget "<question>"`, `answers.py requeue [JOB_ID]`.
You can also add the fact to `facts.yaml` and requeue.

### 7.2 Approve sites (probation)

A kind of site (an ATS platform such as `greenhouse`, or a company domain) is **on probation** until it earns trust.
While on probation the application is filled in and reviewed but **not submitted**. It becomes trusted when you
approve it, when it has been filled in cleanly for `clean_runs_to_trust` different jobs (dry runs count), or when
one application there is confirmed. Because a new install has no confirmed submissions, expect to approve each
site type yourself the first time.

```powershell
python sites.py list                 # every site type seen, trust state, what is waiting on it
python sites.py approve greenhouse   # trust it and release the jobs parked on probation
python sites.py revoke greenhouse    # put it back on probation
```

Review a couple of `applications/<job>/` folders from a site before approving it. Use the name `sites.py list`
shows.

### 7.3 Go live

Make sure no `dry_run` is set in `profile.json` or `config.yaml` (`mode: autonomous`), then run the daemon:

```powershell
python daemon.py
```

Stop it with Ctrl+C. The current application finishes or is parked, then the daemon exits; a second Ctrl+C exits
immediately.

How outcomes are recorded:

| Status | Meaning |
|---|---|
| `submitted` | The site confirmed it (confirmation text or URL seen) |
| `unconfirmed` | Submit was clicked but no confirmation appeared. **Never retried automatically**; check the site |
| `needs_review` | Parked: needs an answer, the form rejected the submit, probation, or an interrupted run |
| `failed` | Error; retried up to `stage3.max_attempts` |
| `skipped` | CAPTCHA, login wall, closed posting, or a poor fit. Closed and not-a-fit are permanent |
| `duplicate` | The site says it was already applied to |

### 7.4 Fit vetoes: check what the models skipped

A "deal-breaker" the model quotes from a posting is the one fit verdict that overrides a good score, and small models
sometimes get it wrong (a posting says "US work authorization required" and the model decides you lack it, although
your facts say you have it). A veto is therefore checked three ways before it is final:

1. **Your own facts.** If the quote is already answered by `facts.yaml` (it asks for work authorization in a country you
   are authorized in, or names the city you live in) the veto is dropped by code. Citizenship, clearances, residence
   and relocation are never waived this way. If `requires_sponsorship` is unstated (`null`), a quote that mentions
   sponsorship is not waived: whether you need sponsorship is your decision, not an assumption.
2. **A second model.** With a `fit_review` role configured (see "Using more than one model") and a veto that
   overrides a good score (the fit score alone would have applied), a *different* model judges the posting on its own,
   without being told what the first said. If it also skips, the skip is final. If it would apply, **nothing is skipped
   or applied**: the job is parked (`needs_review`, code `fit_disagreement`). If it cannot be asked right now, the skip
   is postponed and tried again next cycle without using up one of the job's attempts.
3. **You.** Whatever the models said:

```powershell
python fit_review.py list             # every veto and disagreement, newest first
python fit_review.py show <job>       # the models' reasoning: score, the quoted deal-breakers, the second opinion
python fit_review.py approve <job>    # apply to it after all (queued again; no model can veto it)
python fit_review.py reject <job>     # skip it for good (no model is asked again)
```

The first few characters of the job id are enough. The dashboard's `v` key lists the same postings, disagreements first.
Switch the second opinion off with `fit.second_opinion: false`.

## 8. Running it as a service

`setup.bat` is the Windows launcher (it runs `setup.ps1`). With no arguments it shows an interactive menu;
the flags below skip the menu.

| Command | What it does |
|---|---|
| `setup.bat -All -Background` | Start the daemon, the REST API (port 8000) and Langflow (port 7860) in the background |
| `setup.bat -Daemon -Background` | Start only the daemon, detached |
| `setup.bat -Status` | Show what is running |
| `setup.bat -Logs` | Stream the live daemon log |
| `setup.bat -StopAll` | Stop the daemon, API and Langflow |
| `setup.bat -StopDaemon` | Stop only the daemon |
| `setup.bat -DryRun` | One dry-run cycle |
| `setup.bat -EnableAutoStart` / `-DisableAutoStart` | Start everything at Windows login (adds or removes a script in your Startup folder) |
| `setup.bat -Login "<url>"` | Open the login browser for a site |
| `setup.bat -SyncCookies` | Import cookies from your personal Firefox |
| `setup.bat -Test` | Run the test suite |
| `-Browser "chromium"` | Add to any command to choose the browser engine |

The daemon writes `daemon.log` and holds a single-instance lock in `state.db`, so a second copy refuses to start.
If the machine lost power and the lock is stale, the launcher clears it when it can confirm the process is gone.
After a crash, anything that was mid-application is parked as `needs_review` rather than retried blindly.

> After changing code or `config.yaml`, restart the daemon: `setup.bat -StopAll`, then `setup.bat -All -Background`.

## 9. REST API and Langflow (optional)

### REST API

```powershell
python api.py --port 8000
```

It listens on loopback only and every request needs a bearer token, created on first start in `.api_token`
(or set `AUTO_APPLIER_API_TOKEN`).

```powershell
$token = (Get-Content .api_token -Raw).Trim()
Invoke-RestMethod http://127.0.0.1:8000/api/status -Headers @{ Authorization = "Bearer $token" }
```

| Route | Method | Purpose |
|---|---|---|
| `/api/profile` | GET / POST | Read `profile.json`, or merge the posted fields into it |
| `/api/status` | GET | The five latest runs plus counts of known, submitted and pending jobs |
| `/api/jobs` | GET | The contents of `jobs.json` (the last exported job list) |
| `/api/scrape` | POST | Fetch a Greenhouse/Lever board (`{"source": "greenhouse", "board": "acme"}`) |
| `/api/filter` | POST | Run stage 2 on a list of jobs |
| `/api/apply` | POST | Run stage 3 on a list of jobs. **A dry run unless the body has `"dry_run": false`** (the literal JSON `false`) |

### Langflow

```powershell
python run_langflow.py run --port 7860
```

Open <http://localhost:7860> and import `auto_applier_flow.json` for a visual Profile → Scraper → Filter → Applier →
Metrics pipeline built from this project's components. The Applier component defaults to dry run.

The components import this project's modules, so Langflow must be able to find the folder. Start it from the project
folder (the default), or set `AUTO_APPLIER_HOME` to the project's path first.

### Terminal dashboard

```powershell
tui.bat
```

The first run creates a small environment (`.venv-tui`, a few MB: Textual and PyYAML) and then opens the dashboard.
Textual lives in its own environment on purpose: installing it into the main one would upgrade `rich` and could break
Langflow. Open it in a second terminal window; it can stay open next to the daemon.

It is **read-only**. It reads `state.db` (opened read-only), `daemon.log`, the per-job folders under `applications/`
and asks Ollama which models are loaded; it never writes and never touches the daemon.

| Panel | Shows |
|---|---|
| Top line | whether the daemon is running (checked from its lock and process, never by signalling it), which models Ollama has loaded and how much GPU memory they use, the mode, and when the next cycle starts |
| Today | submissions in the last 24 hours against `stage3.rate_limit.global_per_day`, today's outcomes, all-time totals and a 7-day sparkline |
| Latest cycle | the funnel: scraped, what each filter removed, what reached stage 3, and what happened to it |
| Models | what is loaded now, and each configured role (`models:`) with whether its model is loaded and which tasks use it |
| Now | the job being worked on: `open ▸ fit 84% ✓ ▸ click "Apply" ▸ fill form ▸ click "Next" …`, a **◆** marking a step chosen by a model, and the reason for the latest step |
| Recent applications | newest first; **↑↓ and Enter** open a job: outcome, fit verdict and summary, answers by source, and a table of every agent step with who decided it and why |
| Needs you | questions waiting for an answer (`python answers.py pending`) and each site's trust state (`✓` trusted, `n/m` clean runs toward trust) |

Keys: `Enter` open the selected job, `Esc` back, `o` open that step's screenshot, `f` open the job's folder, `l` the
daemon log, `v` the fit decisions, `r` refresh now, `q` quit. It refreshes every two seconds.

`v` lists every posting skipped for fit and every one parked because the models disagree, the ones that need you first,
with the quoted deal-breaker and where it stands (`models disagree: your call`, `waiting for a 2nd opinion`,
`veto confirmed`, `vetoed by one model`, `low fit score`). `a` / `x` copy the `fit_review.py approve|reject <job>`
command for the selected row. A disagreement also appears in the "Needs you" panel on the main screen.

Other ways to run it: `tui.bat --demo` shows made-up data (handy to see what it looks like before the daemon has run),
`tui.bat --once` prints one text snapshot and exits (needs only `rich`, so `python -m tui --once` works in the main
environment too), and `tui.bat --selftest` drives the dashboard headlessly and prints PASS/FAIL.

If colours are missing, your shell has `NO_COLOR` set; unset it. If the dashboard says "daemon stopped" while you
know it runs, it may be running as another user or in WSL (the process check only sees this Windows session).

## 10. Tuning for your hardware

**Give the model a bigger context window.** Ollama serves a small window by default (2048 tokens on the machine this was
built on), and the daemon prints the real figure at startup (`The model server's context window is only N tokens`;
prompts are then sized to the smaller of that and `llm.context_tokens`). Do not read it from `ollama ps`: its CONTEXT
column shows what the *loaded instance* was started with, which can come from a request option rather than the server
default. If you have the VRAM (a 7B model at 8k context adds only about 0.5 GB), raise both:

```powershell
setx OLLAMA_CONTEXT_LENGTH 8192
```

then restart Ollama, and in `config.yaml` set `llm.context_tokens: 8192`. At startup the daemon measures the
server's real window and sizes prompts to the smaller of the two numbers.

**Smaller machines.** Pick a smaller model in `llm.model` (for example a 3B instruct model) and `ollama pull` it.
Quality drops, and the guards stay the same: the model can only choose from vetted options and every answer must
be grounded in `facts.yaml`. You can also lower `agent.consensus` to `1` (fewer model calls on ambiguous pages;
less safe) or set `fit.enabled: false` to skip the fit check.

**No model at all.** Features that need it degrade rather than guess: with `fit.on_llm_error: skip` a job whose fit
cannot be checked is not applied to and is tried again on a later cycle (with `allow` it is applied to unchecked, which
is only sensible if you run without a model on purpose), the agent stops on ambiguous pages, and questions it cannot
answer from the facts park the job.

**Bigger machines.** A larger model improves the fit check; avoid models too big for your VRAM, because spilling
into system RAM slows each call dramatically.

## 11. Files it creates

| Path | Contents | Keep private? |
|---|---|---|
| `state.db` (+ `-wal`, `-shm`) | All state: jobs, applications, attempts, events, saved answers, fit scores, site trust | Yes |
| `applications/<job>/` | Screenshots, answers, fit assessment, agent traces | Yes |
| `daemon.log` | Log of every cycle | Yes |
| `.browser_profile/` | The bot's browser profile **with logged-in sessions** | **Yes, never share or commit** |
| `.api_token` | REST API bearer token | Yes |
| `.daemon.pid`, `.api.pid`, `.langflow.pid` | Process ids for the launcher | No |
| `profile.json`, `facts.yaml` | Your personal data | Yes |

If you put the project under version control, ignore all of the above.

To start completely fresh, stop the daemon and delete `state.db*`. This also forgets what was already applied to,
so the bot could apply to the same postings again. Upgrades migrate `state.db` in place automatically; stop the
daemon first.

## 12. Testing

```powershell
python -m pytest tests -q
python run_tests.py
```

The suite (248 tests with pytest; `run_tests.py` runs 247, skipping the one that needs the dashboard environment) includes real-browser tests against a fake application server. It takes a few minutes.
Tests that need a Playwright browser are skipped if none is installed. The suite is built around local fake
servers and stubbed models, so it does not need Ollama or an internet connection.

## 13. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `[Daemon] LLM WARNING` at startup | Ollama is not running or the model is missing. Start Ollama, run `ollama pull qwen2.5:7b`, check `llm.endpoint` and `llm.model` |
| "The model server's context window is only 2048 tokens" | Informational. Raise `OLLAMA_CONTEXT_LENGTH` and `llm.context_tokens` ([section 10](#10-tuning-for-your-hardware)) |
| `profile_gen.py` fails with "Resume markdown source not found" | There is no résumé folder in the expected layout. Set `AUTO_APPLIER_RESUME_DIR` to yours, or write `profile.json` by hand ([section 3.1](#31-profilejson-who-you-are-and-what-you-want)) |
| `PyYAML is required to read config.yaml` | `python -m pip install pyyaml` |
| Nothing is ever submitted | Probation is working as designed: run `python sites.py list`, review, then `python sites.py approve <site>`. Also check that no `dry_run` is set and the daily cap is not full |
| "Daily application limit reached" | 25 real submissions in the last rolling 24 h. Wait, or raise `stage3.rate_limit.global_per_day` carefully |
| Jobs piling up as `needs_review` | `python answers.py pending`, answer or add the facts, then `--requeue` |
| "Another daemon instance is actively running" | A daemon is already running (`setup.bat -Status`). If it is not, the lock is stale: `setup.bat -StopAll` |
| LinkedIn / Handshake return nothing | Session expired. Run `python daemon.py --login <url>` again |
| A site is always `unsupported` / parked | Open `applications/<job>/trace/` and read the last step and screenshot. Usually a CAPTCHA, a login wall, a button the agent will not press, or a site that redirects elsewhere (add trusted hosts to `agent.allowed_domains`) |
| An application shows `unconfirmed` | Submit was clicked but no confirmation appeared. Check the site by hand; the bot will not retry it |
| Firefox profile locked | Close any Firefox using `.browser_profile`; the launcher normally clears stale locks |

Logs: `daemon.log` (or `setup.bat -Logs`). Database events: `select * from events order by id desc limit 20;`
in `state.db`.
