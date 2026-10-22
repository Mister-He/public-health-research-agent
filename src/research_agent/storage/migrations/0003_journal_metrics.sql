-- 0003: open journal-impact metrics (OpenAlex sources), cached per ISSN.
CREATE TABLE journal_metrics (
    issn TEXT PRIMARY KEY,
    found INTEGER NOT NULL,                   -- 0 = OpenAlex has no source for this ISSN
    openalex_source_id TEXT,
    display_name TEXT,
    two_year_mean_citedness REAL,             -- open analogue of the Journal Impact Factor
    h_index INTEGER,
    is_in_doaj INTEGER,
    source_type TEXT,
    retrieved_at TEXT NOT NULL
);
