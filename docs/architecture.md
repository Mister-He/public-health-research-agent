# Architecture

Deterministic Python owns identifiers, dates, scores, validation and publication. Claude owns
semantic interpretation and is reached only through `anthropics/claude-code-action` as a file
adapter (manifest in, JSON out). No Python code calls an LLM API.

## Flow (GitHub Actions, `.github/workflows/daily_digest.yml`)

The whole pipeline runs daily, so ingestion windows and Claude batches stay small. On days other
than `reporting.digest_weekday` (default Monday), `prepare` writes no narrative manifest and
`publish` only imports the assessments and updates trends (status `not_scheduled`); the weekly
digest then selects from everything assessed during the week. `force_digest` overrides this.

```
            ┌──────────────────── ingest job (contents: write) ────────────────────┐
schedule →  │ restore SQLite from research-data                                    │
06:00 CST   │ per-(provider, query) checkpointed retrieval, from durable watermark │
            │   Strategy A topic queries: PubMed, Europe PMC, OpenAlex             │
            │   medRxiv window dump (screened locally)                             │
            │   Strategy B journal queries: Crossref by ISSN                       │
            │ normalize → identity map (DOI/PMID/PMCID/arXiv/OpenAlex) → merge      │
            │ prescreen (lexical + TF-IDF) → shortlist → dedup'd analysis queue     │
            │ lease ≤N jobs → write analysis/scoring/manifest + batches            │
            │ PUSH state to research-data  ◄── before Claude                       │
            └──────────────────────────────────────────────────────────────────────┘
                                   │ artifact: manifest + snapshot
            ┌──────────────────── analyze job (contents: read) ────────────────────┐
            │ claude-code-action (OAuth) → analysis/output/evaluations_NNN.json    │
            │ classify outcome (auth / quota / max_turns / timeout / …)            │
            │ prepare: import + trends + selection on a throwaway DB copy          │
            │ claude-code-action → narrative.json  (+ weekly tag_proposals.json)   │
            └──────────────────────────────────────────────────────────────────────┘
                                   │ artifact: outputs + status files
            ┌──────────────────── publish job (contents: write) ───────────────────┐
            │ restore state; validate/import outputs (schema + grounding)          │
            │ settle every leased job: completed | retryable | permanent           │
            │ trend snapshot (daily; weekly reassessment on configured weekday)    │
            │ digest weekday only: scoring + selection (≤10, no padding)           │
            │ render to reports/.staging → independent gate (+ bounded repair)     │
            │ PUSH state → promote report atomically → commit docs(digest): …      │
            └──────────────────────────────────────────────────────────────────────┘
```

A Claude failure never rolls back ingestion: the publish job still persists state (jobs become
`retryable_failed` with the failure category) and simply produces no report when fewer than
`min_verified_papers` verified papers are available.

## Modules (`src/research_agent/`)

| Package | Responsibility |
|---|---|
| `settings.py` | Typed YAML configuration, cross-file invariants, version hashes |
| `ingestion/` | `http.py` (timeouts, retries, 429, throttling, usage); one adapter per provider |
| `metadata/` | `schemas.py` (SourceRecord, Paper), `normalization.py`, `deduplication.py` (identity map, merges, preprint links), `provenance.py` (canonical record = pure function of source records) |
| `retrieval/` | Query builder (provider syntax), lexical screen, TF-IDF/Ollama similarity, shortlist |
| `storage/` | SQLite + migrations, repositories, durable analysis queue |
| `llm/` | Claude Code Action file adapter, output schemas + grounding checks, evaluation cache, fixture and Ollama providers |
| `scoring/` | Dimension registry, built-in evaluators (freshness, trend relevance), aggregator |
| `trends/` | Statistics, taxonomy (YAML-authoritative), snapshots, clustering for proposals |
| `ranking/` | Eligibility, bounded diversity, selection |
| `reporting/` | Jinja2 template, renderer, landscape report |
| `validation/` | Markdown parser/structure, metadata + provenance + identity, URL checks, score recomputation, gate |
| `git/` | Data-branch snapshots, Conventional Commit report publishing |
| `pipeline/` | Checkpointed ingestion, stage runner, run records |
| `evaluation/` | Feedback, gold-set metrics, historical replay |

## Data model (SQLite, `storage/migrations/0001_initial.sql`)

papers · paper_identifiers (unique scheme/value → paper) · paper_title_keys · paper_relations
(preprint `published_as` journal) · paper_versions · identity_conflicts · source_records (immutable
provenance) · paper_discoveries (which query found what) · metadata_revisions · search_queries ·
retrieval_checkpoints · ingestion_runs · research_profiles · scoring_configs · analysis_jobs ·
evaluations (cache) · paper_scores · topic_tags · tag_events · tag_proposals · paper_tags ·
trend_snapshots · trend_observations · external_signals · recommendations · daily_reports ·
validation_results · pipeline_runs · api_usage · llm_usage · feedback · state_meta.

## Storage policy: brief info by default

No PDFs or full texts are ever downloaded — only API metadata. Every retrieved paper is screened
and trend-tagged while its full record (incl. abstract) is in memory. Afterwards, papers that are
not queued for Claude keep **brief info only**: identifiers (for deduplication), title, first three
authors, venue, dates, keywords/MeSH, publication type, relevance score, topics and trend tags
(`storage/compaction.py`). Their source records keep provider, record id, dates, identifiers,
title and a SHA-256 fingerprint of the dropped abstract. Abstracts and complete source records are
kept only for papers with an analysis job, evaluation or recommendation, so every field shown in a
report keeps full provenance. An identical re-fetch (overlap window) only updates "last seen";
genuinely new content (e.g. a new preprint version) restores a full record until re-screened.

## Identity rules

DOI (lower-cased) is the primary identifier; PMID, PMCID, arXiv and OpenAlex ids are fallbacks; a
title + first-author + year match is used only when no identifier matched and no identifier
conflicts. Identifiers of the same scheme that disagree are never merged. A preprint and its journal
version are separate papers linked by `published_as`; only the journal version is eligible when both
are candidates. Preprint revisions are versions of one paper; a materially changed abstract changes
the metadata version and triggers a new assessment.

## Dates

Tracked separately: first online, print, preprint posted, preprint revision, indexed (PubMed EDAT /
Europe PMC FIRST_IDATE / Crossref created / medRxiv posting), modified, discovered, last verified.
`first_publication_date` = preprint first posting, or the earliest day-precision online/print date.
All timestamps are stored in UTC; report dates are Asia/Shanghai calendar dates.
