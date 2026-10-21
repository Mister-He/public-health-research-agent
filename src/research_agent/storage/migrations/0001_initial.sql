-- 0001: initial schema. Dates are ISO strings; timestamps are UTC ISO-8601 ('...Z').

CREATE TABLE state_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- ------------------------------------------------------------------ papers & identity
CREATE TABLE papers (
    id INTEGER PRIMARY KEY,
    canonical_id TEXT NOT NULL UNIQUE,
    data TEXT NOT NULL,                       -- Paper JSON (canonical, merged)
    metadata_version TEXT NOT NULL,
    first_publication_date TEXT,
    indexed_date TEXT,
    discovered_at TEXT NOT NULL,
    discovered_run_id TEXT,
    updated_at TEXT NOT NULL,
    prescreen_score REAL,
    is_preprint INTEGER NOT NULL DEFAULT 0,
    is_retracted INTEGER NOT NULL DEFAULT 0,
    metadata_completeness REAL NOT NULL DEFAULT 0
);
CREATE INDEX ix_papers_pubdate ON papers(first_publication_date);
CREATE INDEX ix_papers_discovered ON papers(discovered_at);
CREATE INDEX ix_papers_prescreen ON papers(prescreen_score);

-- Persistent identity map: one (scheme, value) can only ever point to one paper.
CREATE TABLE paper_identifiers (
    scheme TEXT NOT NULL,                     -- doi | pmid | pmcid | openalex | arxiv | preprint | canonical_alias
    value TEXT NOT NULL,
    paper_id INTEGER NOT NULL REFERENCES papers(id),
    source TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (scheme, value)
);
CREATE INDEX ix_identifiers_paper ON paper_identifiers(paper_id);

-- Title+first-author fallback key (only used when no identifier matched).
CREATE TABLE paper_title_keys (
    title_key TEXT NOT NULL,
    first_author TEXT NOT NULL,
    year INTEGER,
    paper_id INTEGER NOT NULL REFERENCES papers(id),
    PRIMARY KEY (title_key, first_author, paper_id)
);

CREATE TABLE paper_relations (
    from_paper_id INTEGER NOT NULL REFERENCES papers(id),
    to_paper_id INTEGER NOT NULL REFERENCES papers(id),
    relation TEXT NOT NULL,                   -- published_as (preprint -> journal)
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (from_paper_id, to_paper_id, relation)
);

CREATE TABLE paper_versions (
    paper_id INTEGER NOT NULL REFERENCES papers(id),
    version INTEGER NOT NULL,
    posted_date TEXT,
    abstract_hash TEXT,
    title_hash TEXT,
    source_record_id TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (paper_id, version)
);

CREATE TABLE identity_conflicts (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    candidate_paper_ids TEXT NOT NULL,        -- JSON list
    resolution TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    UNIQUE (provider, source_record_id, candidate_paper_ids)
);

-- Immutable provenance: every distinct provider payload is kept once.
CREATE TABLE source_records (
    id INTEGER PRIMARY KEY,
    paper_id INTEGER NOT NULL REFERENCES papers(id),
    provider TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    first_query_id TEXT NOT NULL,
    first_retrieved_at TEXT NOT NULL,
    last_retrieved_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE (provider, source_record_id, content_hash)
);
CREATE INDEX ix_source_records_paper ON source_records(paper_id);

-- Which (provider, query) discovered which paper; repeated discoveries update counters only.
CREATE TABLE paper_discoveries (
    paper_id INTEGER NOT NULL REFERENCES papers(id),
    provider TEXT NOT NULL,
    query_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    times_seen INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (paper_id, provider, query_id)
);

CREATE TABLE metadata_revisions (
    id INTEGER PRIMARY KEY,
    paper_id INTEGER NOT NULL REFERENCES papers(id),
    field TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    source TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    run_id TEXT
);
CREATE INDEX ix_revisions_paper ON metadata_revisions(paper_id);

-- ------------------------------------------------------------------ ingestion
CREATE TABLE search_queries (
    query_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    strategy TEXT NOT NULL,
    query_text TEXT NOT NULL,
    query_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT NOT NULL,
    PRIMARY KEY (provider, query_id)
);

CREATE TABLE retrieval_checkpoints (
    provider TEXT NOT NULL,
    query_id TEXT NOT NULL,
    query_hash TEXT NOT NULL,
    watermark_date TEXT,                      -- last inclusive date fully stored (UTC calendar)
    status TEXT NOT NULL,                     -- ok | partial | failed
    window_start TEXT,                        -- in-flight chunk (for page resume)
    window_end TEXT,
    cursor TEXT,
    pages_done INTEGER NOT NULL DEFAULT 0,
    last_success_at TEXT,
    last_error TEXT,
    last_run_id TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (provider, query_id)
);

CREATE TABLE ingestion_runs (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    query_id TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    status TEXT NOT NULL,                     -- completed | failed
    records INTEGER NOT NULL DEFAULT 0,
    new_papers INTEGER NOT NULL DEFAULT 0,
    pages INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error TEXT
);
CREATE INDEX ix_ingestion_runs_run ON ingestion_runs(run_id);

-- ------------------------------------------------------------------ configuration history
CREATE TABLE research_profiles (
    profile_hash TEXT PRIMARY KEY,
    profile_version TEXT NOT NULL,
    content TEXT NOT NULL,
    first_seen_at TEXT NOT NULL
);

CREATE TABLE scoring_configs (
    scoring_version TEXT PRIMARY KEY,
    content TEXT NOT NULL,                    -- full dimension definitions (keeps old scores interpretable)
    first_seen_at TEXT NOT NULL
);

-- ------------------------------------------------------------------ analysis queue & evaluations
CREATE TABLE analysis_jobs (
    id INTEGER PRIMARY KEY,
    paper_id INTEGER NOT NULL REFERENCES papers(id),
    canonical_id TEXT NOT NULL,
    metadata_version TEXT NOT NULL,
    rubric_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','in_progress','completed','retryable_failed','permanent_failed','superseded')),
    priority REAL NOT NULL DEFAULT 0,
    intended_report_date TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    format_failures INTEGER NOT NULL DEFAULT 0,
    outage_failures INTEGER NOT NULL DEFAULT 0,
    failure_category TEXT,
    last_error TEXT,
    retry_after TEXT,
    lease_expires_at TEXT,
    leased_by_run TEXT,
    evaluation_id INTEGER REFERENCES evaluations(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (paper_id, metadata_version, rubric_version, prompt_version, model)
);
CREATE INDEX ix_jobs_status ON analysis_jobs(status, retry_after);

CREATE TABLE evaluations (
    id INTEGER PRIMARY KEY,
    paper_id INTEGER NOT NULL REFERENCES papers(id),
    canonical_id TEXT NOT NULL,
    metadata_version TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    rubric_version TEXT NOT NULL,
    payload TEXT NOT NULL,                    -- validated PaperEvaluation JSON
    source_file TEXT,
    run_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (paper_id, metadata_version, model, prompt_version, rubric_version)
);

CREATE TABLE paper_scores (
    id INTEGER PRIMARY KEY,
    paper_id INTEGER NOT NULL REFERENCES papers(id),
    report_date TEXT NOT NULL,
    evaluation_id INTEGER NOT NULL REFERENCES evaluations(id),
    scoring_version TEXT NOT NULL,
    trend_snapshot_id INTEGER REFERENCES trend_snapshots(id),
    dimension_scores TEXT NOT NULL,           -- JSON {dim: {score, weight, method, ...}}
    overall_score REAL NOT NULL,
    computed_at TEXT NOT NULL,
    UNIQUE (paper_id, report_date, scoring_version)
);

-- ------------------------------------------------------------------ trends
CREATE TABLE topic_tags (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL CHECK (status IN ('active','watchlist','archived','merged')),
    pinned INTEGER NOT NULL DEFAULT 0,
    seed INTEGER NOT NULL DEFAULT 0,
    origin TEXT NOT NULL,                     -- config | proposal
    aliases TEXT NOT NULL DEFAULT '[]',
    parent_tags TEXT NOT NULL DEFAULT '[]',
    match_rules TEXT NOT NULL DEFAULT '[]',
    manual_override REAL,
    initial_score REAL,
    current_score REAL,
    frequency_7d INTEGER,
    frequency_30d INTEGER,
    frequency_90d INTEGER,
    baseline_frequency REAL,
    growth_signal REAL,
    external_signal REAL,
    supporting_paper_ids TEXT NOT NULL DEFAULT '[]',
    merged_into TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE tag_events (
    id INTEGER PRIMARY KEY,
    tag_id TEXT NOT NULL,
    event TEXT NOT NULL,
    details TEXT,
    at TEXT NOT NULL
);

CREATE TABLE tag_proposals (
    id INTEGER PRIMARY KEY,
    proposal_key TEXT NOT NULL UNIQUE,        -- normalized name
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    match_rules TEXT NOT NULL,
    evidence TEXT NOT NULL,
    supporting_paper_ids TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('proposed','accepted','rejected')),
    proposed_at TEXT NOT NULL,
    run_id TEXT,
    decided_at TEXT
);

CREATE TABLE paper_tags (
    paper_id INTEGER NOT NULL REFERENCES papers(id),
    tag_id TEXT NOT NULL,
    method TEXT NOT NULL,                     -- lexical | claude
    confidence REAL,
    evaluation_id INTEGER,
    assigned_at TEXT NOT NULL,
    PRIMARY KEY (paper_id, tag_id, method)
);
CREATE INDEX ix_paper_tags_tag ON paper_tags(tag_id);

CREATE TABLE trend_snapshots (
    id INTEGER PRIMARY KEY,
    as_of_date TEXT NOT NULL,
    kind TEXT NOT NULL,                       -- daily | weekly | landscape
    version TEXT NOT NULL,                    -- trend algorithm + params + taxonomy hash
    corpus_size INTEGER NOT NULL,
    payload TEXT NOT NULL,                    -- per-tag metrics, windows, sources, uncertainty
    status TEXT NOT NULL DEFAULT 'valid',
    created_at TEXT NOT NULL,
    run_id TEXT,
    UNIQUE (as_of_date, kind, version)
);

CREATE TABLE trend_observations (
    snapshot_id INTEGER NOT NULL REFERENCES trend_snapshots(id),
    tag_id TEXT NOT NULL,
    window_days INTEGER NOT NULL,
    tagged_count INTEGER NOT NULL,
    corpus_count INTEGER NOT NULL,
    PRIMARY KEY (snapshot_id, tag_id, window_days)
);

CREATE TABLE external_signals (
    tag_id TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    provider TEXT NOT NULL,
    query TEXT NOT NULL,
    recent_count INTEGER,
    prior_count INTEGER,
    signal REAL,
    status TEXT NOT NULL,                     -- ok | unavailable
    retrieved_at TEXT NOT NULL,
    PRIMARY KEY (tag_id, as_of_date, provider)
);

-- ------------------------------------------------------------------ recommendations & reports
CREATE TABLE recommendations (
    report_date TEXT NOT NULL,
    position INTEGER NOT NULL,
    paper_id INTEGER NOT NULL REFERENCES papers(id),
    canonical_id TEXT NOT NULL,
    raw_score REAL NOT NULL,
    adjusted_score REAL NOT NULL,
    raw_rank INTEGER NOT NULL,
    scoring_version TEXT NOT NULL,
    trend_snapshot_id INTEGER,
    evaluation_id INTEGER NOT NULL,
    reason TEXT NOT NULL,                     -- JSON: eligibility, diversity, category
    status TEXT NOT NULL,                     -- selected | published | withdrawn
    run_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (report_date, position),
    UNIQUE (report_date, paper_id)
);
CREATE INDEX ix_recommendations_paper ON recommendations(paper_id);

CREATE TABLE daily_reports (
    report_date TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    status TEXT NOT NULL,                     -- published | blocked
    validation_status TEXT NOT NULL,
    paper_count INTEGER NOT NULL,
    front_matter TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    committed_sha TEXT,
    run_id TEXT
);

CREATE TABLE validation_results (
    id INTEGER PRIMARY KEY,
    report_date TEXT NOT NULL,
    run_id TEXT,
    round INTEGER NOT NULL DEFAULT 0,
    check_name TEXT NOT NULL,
    target TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pass','warn','fail')),
    detail TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX ix_validation_date ON validation_results(report_date);

-- ------------------------------------------------------------------ observability
CREATE TABLE pipeline_runs (
    run_id TEXT PRIMARY KEY,
    scheduled_date TEXT NOT NULL,
    stage TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    duration_seconds REAL,
    status TEXT NOT NULL,
    pipeline_version TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE api_usage (
    id INTEGER PRIMARY KEY,
    run_id TEXT,
    provider TEXT NOT NULL,
    requests INTEGER NOT NULL,
    failures INTEGER NOT NULL,
    retries INTEGER NOT NULL,
    rate_limited INTEGER NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TABLE llm_usage (
    id INTEGER PRIMARY KEY,
    run_id TEXT,
    stage TEXT NOT NULL,
    model TEXT,
    outcome TEXT NOT NULL,
    failure_category TEXT,
    requests INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    num_turns INTEGER,
    reported_cost_usd REAL,
    papers_sent INTEGER NOT NULL DEFAULT 0,
    papers_completed INTEGER NOT NULL DEFAULT 0,
    cache_hits INTEGER NOT NULL DEFAULT 0,
    retries INTEGER NOT NULL DEFAULT 0,
    subscription_limit_event INTEGER NOT NULL DEFAULT 0,
    recorded_at TEXT NOT NULL
);

CREATE TABLE feedback (
    id INTEGER PRIMARY KEY,
    canonical_id TEXT NOT NULL,
    label TEXT NOT NULL,
    note TEXT,
    report_date TEXT,
    recorded_at TEXT NOT NULL,
    UNIQUE (canonical_id, label, report_date)
);
