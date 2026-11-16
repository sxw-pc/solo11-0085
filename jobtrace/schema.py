"""SQLite schema for runs, jobs, snapshots, events, and raw evidence."""

SCHEMA_VERSION = "2"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, site_key TEXT NOT NULL, scenario TEXT NOT NULL,
    base_url TEXT NOT NULL, config_hash TEXT NOT NULL,
    started_at TEXT NOT NULL, finished_at TEXT,
    business_date TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running', is_complete INTEGER NOT NULL DEFAULT 0,
    pages_fetched INTEGER NOT NULL DEFAULT 0, list_cards INTEGER NOT NULL DEFAULT 0,
    unique_jobs INTEGER NOT NULL DEFAULT 0, details_ok INTEGER NOT NULL DEFAULT 0,
    details_failed INTEGER NOT NULL DEFAULT 0, duplicate_ids INTEGER NOT NULL DEFAULT 0,
    parse_warnings INTEGER NOT NULL DEFAULT 0, resumed INTEGER NOT NULL DEFAULT 0,
    stop_reason TEXT, notes_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_runs_site_started
    ON runs(site_key, started_at);

CREATE TABLE IF NOT EXISTS jobs (
    job_uid TEXT PRIMARY KEY, site_key TEXT NOT NULL, external_id TEXT NOT NULL,
    title TEXT NOT NULL, company TEXT NOT NULL,
    location_raw TEXT NOT NULL DEFAULT '', city TEXT, district TEXT,
    salary_min INTEGER, salary_max INTEGER, salary_text TEXT NOT NULL DEFAULT '',
    salary_currency TEXT NOT NULL DEFAULT 'CNY',
    salary_period TEXT NOT NULL DEFAULT 'month',
    salary_months INTEGER NOT NULL DEFAULT 12,
    salary_negotiable INTEGER NOT NULL DEFAULT 0,
    experience_text TEXT, experience_min INTEGER, experience_max INTEGER,
    employment_type TEXT, description TEXT NOT NULL DEFAULT '',
    tags_json TEXT NOT NULL DEFAULT '[]', detail_url TEXT NOT NULL,
    posted_at TEXT, source_status TEXT,
    status TEXT NOT NULL DEFAULT 'open', missing_streak INTEGER NOT NULL DEFAULT 0,
    reopen_count INTEGER NOT NULL DEFAULT 0,
    first_seen_run_id TEXT NOT NULL, first_seen_at TEXT NOT NULL,
    last_seen_run_id TEXT NOT NULL, last_seen_at TEXT NOT NULL, closed_at TEXT,
    content_hash TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(site_key, external_id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_site_status ON jobs(site_key, status);
CREATE INDEX IF NOT EXISTS idx_jobs_last_seen ON jobs(last_seen_at);

CREATE TABLE IF NOT EXISTS job_snapshots (
    job_uid TEXT NOT NULL, run_id TEXT NOT NULL, content_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL, observed_at TEXT NOT NULL,
    PRIMARY KEY(job_uid, run_id)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_uid TEXT NOT NULL,
    run_id TEXT NOT NULL, event_type TEXT NOT NULL, field TEXT NOT NULL DEFAULT '',
    old_value TEXT, new_value TEXT, created_at TEXT NOT NULL,
    UNIQUE(job_uid, run_id, event_type, field)
);

CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_uid, id);

CREATE TABLE IF NOT EXISTS corrections (
    correction_id TEXT PRIMARY KEY, site_key TEXT NOT NULL,
    run_id TEXT NOT NULL, corrected_run_id TEXT NOT NULL,
    business_date TEXT NOT NULL, mode TEXT NOT NULL,
    source_summary TEXT NOT NULL, fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'applied', semantics TEXT NOT NULL DEFAULT '',
    diff_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_corrections_site ON corrections(site_key, business_date);

CREATE TABLE IF NOT EXISTS derived_archive (
    id INTEGER PRIMARY KEY AUTOINCREMENT, correction_id TEXT NOT NULL,
    kind TEXT NOT NULL, ref TEXT NOT NULL,
    payload_json TEXT NOT NULL, archived_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_derived_archive_correction
    ON derived_archive(correction_id, kind);

CREATE TABLE IF NOT EXISTS raw_documents (
    sha256 TEXT PRIMARY KEY, size_bytes INTEGER NOT NULL,
    content_type TEXT NOT NULL, first_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_documents (
    run_id TEXT NOT NULL, sha256 TEXT NOT NULL, url TEXT NOT NULL,
    kind TEXT NOT NULL, http_status INTEGER NOT NULL, fetched_at TEXT NOT NULL,
    relative_path TEXT NOT NULL, size_bytes INTEGER NOT NULL,
    PRIMARY KEY(run_id, sha256, url)
);

CREATE INDEX IF NOT EXISTS idx_run_documents_run ON run_documents(run_id);
"""
