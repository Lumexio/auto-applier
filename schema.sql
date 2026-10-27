PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;

-- Schema version
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('version', '2.0');

-- Single-writer daemon mutual exclusion lock
CREATE TABLE IF NOT EXISTS daemon_lock (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    holder_pid   INTEGER NOT NULL,
    holder_host  TEXT    NOT NULL,
    acquired_at  TEXT    NOT NULL,
    heartbeat_at TEXT    NOT NULL
);

-- Runs: one record per daemon cycle
CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL CHECK (status IN ('running','completed','failed','halted')),
    stage           TEXT CHECK (stage IN ('stage1','stage2','stage3')),
    error_code      TEXT,
    error_message   TEXT,
    config_hash     TEXT NOT NULL,
    profile_hash    TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);

-- Artifacts: generated scrapers and adapters
CREATE TABLE IF NOT EXISTS artifacts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    domain          TEXT NOT NULL,
    site_hash       TEXT NOT NULL,
    schema_version  TEXT NOT NULL,
    scraper_path    TEXT NOT NULL,
    applier_path    TEXT NOT NULL,
    schema_path     TEXT NOT NULL,
    model_hash      TEXT NOT NULL,
    prompt_hash     TEXT NOT NULL,
    generated_at    TEXT NOT NULL,
    is_active       INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    UNIQUE(domain, site_hash)
);
CREATE INDEX IF NOT EXISTS idx_artifacts_domain ON artifacts(domain, is_active);

-- Jobs: filtered postings
CREATE TABLE IF NOT EXISTS jobs (
    id                TEXT PRIMARY KEY,
    run_id            INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    title             TEXT NOT NULL,
    company           TEXT NOT NULL,
    company_norm      TEXT NOT NULL,
    location          TEXT,
    remote            INTEGER CHECK (remote IN (0,1)),
    url               TEXT,
    apply_url         TEXT NOT NULL,
    apply_url_canon   TEXT NOT NULL,
    posted_at         TEXT,
    description       TEXT,
    salary_min        INTEGER,
    salary_max        INTEGER,
    currency          TEXT,
    source            TEXT NOT NULL,
    score             REAL NOT NULL CHECK (score >= 0 AND score <= 1),
    reason            TEXT,
    ats               TEXT CHECK (ats IN ('workday','greenhouse','lever','icims','taleo','custom') OR ats IS NULL),
    status            TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','in_progress','submitted','failed','duplicate','skipped','unconfirmed')),
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE(company_norm, apply_url_canon)
);
CREATE INDEX IF NOT EXISTS idx_jobs_status   ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_run      ON jobs(run_id);
CREATE INDEX IF NOT EXISTS idx_jobs_company  ON jobs(company_norm);

-- Applications: submission attempts
CREATE TABLE IF NOT EXISTS applications (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id            TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    run_id            INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    status            TEXT NOT NULL CHECK (status IN
                        ('in_progress','submitted','unconfirmed','failed','duplicate','skipped','needs_review')),
    mode              TEXT NOT NULL CHECK (mode IN ('dry_run','assisted','autonomous','auto_apply_allowlist')),
    ats               TEXT,
    ats_id            TEXT,
    started_at        TEXT NOT NULL,
    submitted_at      TEXT,
    finished_at       TEXT,
    error_code        TEXT,
    error_message     TEXT,
    artifacts_dir     TEXT,
    approved_by_user  INTEGER CHECK (approved_by_user IN (0,1)),
    approval_at       TEXT,
    checkpoint_step   TEXT,
    attempts          INTEGER NOT NULL DEFAULT 0,
    UNIQUE(job_id)
);
CREATE INDEX IF NOT EXISTS idx_apps_status   ON applications(status);
CREATE INDEX IF NOT EXISTS idx_apps_started  ON applications(started_at DESC);

-- Rate events: sliding window tracking
CREATE TABLE IF NOT EXISTS rate_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    domain      TEXT NOT NULL,
    job_id      TEXT,
    event_at    TEXT NOT NULL,
    event_type  TEXT NOT NULL CHECK (event_type IN ('attempt','submit'))
);
CREATE INDEX IF NOT EXISTS idx_rate_domain_time ON rate_events(domain, event_at DESC);
CREATE INDEX IF NOT EXISTS idx_rate_time        ON rate_events(event_at DESC);

-- Diagnostics per run
CREATE TABLE IF NOT EXISTS run_diagnostics (
    run_id                   INTEGER PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    total_scraped            INTEGER NOT NULL DEFAULT 0,
    removed_expired          INTEGER NOT NULL DEFAULT 0,
    removed_location         INTEGER NOT NULL DEFAULT 0,
    removed_authorization    INTEGER NOT NULL DEFAULT 0,
    removed_salary           INTEGER NOT NULL DEFAULT 0,
    removed_exclusions       INTEGER NOT NULL DEFAULT 0,
    removed_duplicates       INTEGER NOT NULL DEFAULT 0,
    removed_scam             INTEGER NOT NULL DEFAULT 0,
    removed_relevance        INTEGER NOT NULL DEFAULT 0,
    truncated_by_cap         INTEGER NOT NULL DEFAULT 0
);

-- Event log: structured audit trail
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    job_id      TEXT,
    level       TEXT NOT NULL CHECK (level IN ('DEBUG','INFO','WARN','ERROR')),
    stage       TEXT,
    event       TEXT NOT NULL,
    payload     TEXT,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_time  ON events(occurred_at DESC);
-- Recruiter Outreach: direct messages and connection notes sent to hiring teams
CREATE TABLE IF NOT EXISTS recruiter_outreach (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT NOT NULL,
    company         TEXT,
    recruiter_name  TEXT NOT NULL,
    recruiter_url   TEXT,
    message_text    TEXT NOT NULL,
    channel         TEXT NOT NULL CHECK (channel IN ('message', 'connection_note', 'inmail')),
    status          TEXT NOT NULL CHECK (status IN ('sent', 'drafted', 'inmail_locked', 'limit_reached', 'already_connected', 'skipped', 'failed')),
    sent_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outreach_recruiter ON recruiter_outreach(recruiter_url);
CREATE INDEX IF NOT EXISTS idx_outreach_sent      ON recruiter_outreach(sent_at DESC);

-- Answers the user gave explicitly (python answers.py set ...). They override every rule and the model.
CREATE TABLE IF NOT EXISTS answer_bank (
    question_key  TEXT PRIMARY KEY,
    question_text TEXT NOT NULL,
    answer        TEXT NOT NULL,
    source        TEXT NOT NULL DEFAULT 'user' CHECK (source IN ('user','llm','fact')),
    updated_at    TEXT NOT NULL
);

-- Required questions an application could not answer from the fact sheet (what python answers.py pending lists).
CREATE TABLE IF NOT EXISTS pending_questions (
    job_id        TEXT NOT NULL,
    question_key  TEXT NOT NULL,
    question_text TEXT NOT NULL,
    kind          TEXT,
    options       TEXT,
    reason        TEXT,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (job_id, question_key)
);
CREATE INDEX IF NOT EXISTS idx_pending_key ON pending_questions(question_key);

-- Model fit assessments, cached per posting and per profile / model / prompt version.
CREATE TABLE IF NOT EXISTS fit_scores (
    job_id     TEXT NOT NULL,
    cache_key  TEXT NOT NULL,
    score      REAL,
    verdict    TEXT NOT NULL CHECK (verdict IN ('apply','skip','unknown')),
    payload    TEXT,
    model      TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (job_id, cache_key)
);

-- Which site types (an ATS platform, or a company domain) may be submitted to without a human looking first.
CREATE TABLE IF NOT EXISTS site_trust (
    site_key              TEXT PRIMARY KEY,
    approved              INTEGER NOT NULL DEFAULT 0 CHECK (approved IN (0,1)),
    confirmed_submissions INTEGER NOT NULL DEFAULT 0,
    updated_at            TEXT NOT NULL
);

-- One row per distinct job a site type was filled in cleanly for while on probation (dry runs count too).
CREATE TABLE IF NOT EXISTS site_clean_runs (
    site_key TEXT NOT NULL,
    job_id   TEXT NOT NULL,
    run_at   TEXT NOT NULL,
    PRIMARY KEY (site_key, job_id)
);

-- Useful views
CREATE VIEW IF NOT EXISTS v_daily_application_count AS
SELECT substr(submitted_at, 1, 10) AS day, COUNT(*) AS submitted
FROM applications
WHERE status = 'submitted'
GROUP BY day;

CREATE VIEW IF NOT EXISTS v_stale_in_progress AS
SELECT a.*, j.apply_url
FROM applications a
JOIN jobs j ON j.id = a.job_id
WHERE a.status = 'in_progress'
  AND datetime(a.started_at) < datetime('now', '-10 minutes');
