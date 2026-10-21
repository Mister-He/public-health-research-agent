-- 0002: brief-info storage. Papers that are not sent to Claude keep only brief metadata.
ALTER TABLE papers ADD COLUMN compacted INTEGER NOT NULL DEFAULT 0;
CREATE INDEX ix_papers_compacted ON papers(compacted);
