# AI Auto Job Applier

A job-application assistant that runs **entirely on your own computer**. It finds postings, decides which ones
suit you, fills in the application forms, and (once you trust it) submits them. It runs as a background daemon
until you stop it or shut the PC down.

There is no cloud AI behind it. The language model is a small open model (default: `qwen2.5:7b`) served locally
by [Ollama](https://ollama.com), and everything that matters for safety is decided by ordinary code, not by the model.

> **Status:** active development. The pipeline, the answer engine and the safety rails are covered by 187
> automated tests, but a brand-new install has no application history, so **every kind of site starts on
> probation** (filled in and reviewed, not submitted) until you approve it. See [Safety model](#safety-model).
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
   companies and keywords, minimum salary, title relevance (field and seniority) and a skill-match score. No model
   is needed here. (Location and work authorization are not hard filters: the fit check in stage 3 weighs them.)
3. **Stage 3: applying.** For the few jobs that survive (at most the daily cap), a local-model **fit check** reads
   the real posting text, the form is opened and filled, and the result is verified against what the page says.

## Features

### Job discovery
- Greenhouse and Lever boards (no login required), LinkedIn and Handshake searches (browser, persistent session).
- Optional cookie import from your own Firefox profile so the bot starts out logged in.

### Filtering and fit
- Hard filters: duplicates (across runs), jobs already applied to, expired postings, scam heuristics,
  company/keyword exclusions, salary floor, title relevance (your field and seniority) and a skill-match score.
- **Model-based fit scoring** on the actual posting text, cached per posting/profile/model. It judges what the
  cheap rules cannot, such as location, remote policy and work authorization, against your facts. A "deal-breaker" only
  vetoes a job if the model can quote it **verbatim** from the posting, so a small model cannot invent a reason
  to skip a job.

### Answering application questions
- Answers come only from **`facts.yaml`** (a file you own and edit), your contact details in `profile.json`, and
  answers you saved with `answers.py`. Yes/no and numeric questions (work authorization, sponsorship, years of
  experience with a skill, salary, notice period…) are answered from those facts; nothing is guessed.
- If a *required* question cannot be answered, the job is **parked as `needs_review`** and listed by
  `python answers.py pending`. Answer it once (`answers.py set`) and it is used for all future applications.
- Consent boxes (privacy policy, terms, "my information is accurate") can be ticked automatically; marketing
  opt-ins never are. Essay/opinion questions are parked for you (or drafted from a `narrative` block if you opt in).

### Handling any site: the application agent
- Dedicated handlers: **Greenhouse, Lever, Workday, LinkedIn Easy Apply, Handshake Quick Apply**.
- For everything else (and for "apply on the company site" links from LinkedIn/Handshake) an
  **observe → decide → act → verify** agent takes over. It works in new tabs, iframes and shadow DOM, handles
  multi-step wizards, declines cookie banners, uploads your résumé, and notices when a site rejected a step.
- Plain rules decide almost every step. The model is asked only about an unusual button (for example
  "Let's go!"), only chooses from a menu of buttons the code already judged safe, and is asked in several menu
  orders; if its answer changes with the order it is guessing and the agent stops instead of acting.
- Every run leaves a **step-by-step trace with a screenshot per step** in `applications/<job>/trace/`.

### Safety and reliability
- **Probation per site type**: a new ATS platform or company domain is filled in and reviewed, never submitted,
  until you approve it, it completes a few clean runs, or one application there is confirmed.
- **Dry run is authoritative**: in dry-run mode nothing is ever submitted, whatever else is configured.
- **Duplicate protection**: a job is never applied to twice. An application that was clicked but not confirmed is
  recorded as `unconfirmed` and is **never** retried automatically.
- **Rate limits**: a daily cap (default 25), a per-domain hourly cap and randomised delays.
- **Verification**: "submitted" requires evidence from the page (a confirmation message or URL), not just a click.
- **Crash-safe**: single-instance lock, graceful shutdown on Ctrl+C / system shutdown, interrupted applications
  are parked for review rather than guessed at.
- **Audit trail**: SQLite database of runs, attempts and events, plus a per-job folder of screenshots and answers.

### Interfaces
- **CLI** for the daemon, answers and site trust.
- **Local REST API** (loopback only, bearer-token protected) for profile, status, scrape/filter/apply.
- **Langflow** visual pipeline (optional) with ready-made components.
- **Windows helper** (`setup.ps1`) to start/stop services and auto-start them at login.

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
  are skipped or parked for you.
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
# create profile.json and facts.yaml (see SETUP.md), then:
python daemon.py --once --dry-run
```

Look in `applications/<job>/` for what it would have submitted. When you are happy with a site:

```powershell
python sites.py list
python sites.py approve greenhouse
```

Then run it for real, either `python daemon.py` or `setup.bat -All -Background`.

## Everyday commands

| Command | What it does |
|---|---|
| `python daemon.py` | Run the daemon (one cycle per `daemon.interval_seconds`) |
| `python daemon.py --once --dry-run` | One cycle, nothing submitted |
| `python daemon.py --login <url>` | Open a visible browser to log in to a site once |
| `python daemon.py --sync-cookies` | Import cookies from your personal Firefox profile |
| `python answers.py pending` | Required questions that blocked applications |
| `python answers.py set "<question>" "<answer>" --requeue` | Answer once for all future applications |
| `python sites.py list` / `approve <site>` / `revoke <site>` | Manage which sites may be submitted to |
| `python api.py --port 8000` | Local REST API |
| `setup.bat -Status` | Show daemon / API / Langflow state |
| `python -m pytest tests -q` | Run the test suite |

## Project layout

| Path | Purpose |
|---|---|
| `daemon.py` | The loop: scheduling, locking, signals, stage 1→2→3 per cycle |
| `scraper.py`, `job_text.py` | Greenhouse / Lever / browser ingestion and text cleanup |
| `stage2_filter/` | Normalise, dedupe, filter, relevance, rank |
| `stage3_apply/` | Application runner, rate limiter, confirmation checks |
| `stage3_apply/ats_handlers/` | Greenhouse, Lever, Workday, LinkedIn, Handshake and the agent handler |
| `agent.py`, `page_state.py` | The application agent and the page/button classifier |
| `form_questions.py`, `answerer.py`, `facts.py`, `answers.py` | Reading form fields and answering from your facts |
| `fit.py`, `llm_client.py` | Model-based fit scoring and the local-model client |
| `state.py`, `schema.sql`, `sites.py` | SQLite state, migrations, site trust (probation) |
| `api.py` | Local REST API |
| `langflow_components.py`, `run_langflow.py`, `auto_applier_flow.json` | Optional visual pipeline |
| `outreach.py` | Recruiter outreach (disabled by default) |
| `stage1_generate/` | Experimental sandboxed scraper-code generator (not part of the daemon loop) |
| `applier.py`, `store.py` | The original standalone prototype, kept for reference |
| `tests/` | Test suite, including a fake application server driven by real browsers |
| `setup.ps1`, `setup.bat` | Windows launcher: install, start/stop services, auto-start |

## Privacy and responsible use

- All processing is local. The only network traffic is to the job sites themselves (and a one-time model download).
- These files contain personal data or live sessions; keep them private and out of version control:
  `profile.json`, `facts.yaml`, `state.db*`, `applications/`, `.browser_profile/`, `.api_token`, `daemon.log`.
  The included `.gitignore` already excludes them (plus `jobs.json`, `*.pid` and other runtime output). If you
  ever staged them before adding it, unstage with `git rm -r --cached <path>`; if you already committed or pushed
  them, treat the browser session and API token as leaked and rotate them.
- Automated applications can breach the terms of some platforms (LinkedIn in particular). You are responsible for
  how you use this tool; start with dry runs, keep the caps low and review what it sends.

## Documentation

- **[SETUP.md](SETUP.md)** for installation, configuration, first run, running as a service, tuning and troubleshooting.
- `config.yaml` is commented inline; every option is explained next to its value.
