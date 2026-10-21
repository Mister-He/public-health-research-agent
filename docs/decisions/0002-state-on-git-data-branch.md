# ADR 0002 — Persist state on a dedicated Git data branch, in brief-info form

- Status: accepted (confirmed deployment choice; storage format revised after live measurement)
- Date: 2026-10-22

## Context

GitHub-hosted runners are ephemeral. Ingestion watermarks, the analysis queue, the evaluation cache
and recommendation history must survive between runs, including runs where Claude is unavailable.

A first live test (14 days, all providers) stored every retrieved paper's full metadata and every
source's raw response: **7,224 papers, 72 MB database, 12.4 MB gzipped**. Extrapolated to the
180-day baseline that is ~160 MB per snapshot — above GitHub's 100 MB file limit — and a full copy
committed every day would grow the branch by gigabytes per year. Nothing was downloaded beyond API
metadata; the volume came from storing abstracts and raw responses for papers that are only screened.

## Decision

1. **Brief info by default** (`storage/compaction.py`). Every paper is screened and trend-tagged
   while its full record is in memory; papers not queued for Claude then keep only identifiers,
   title, first three authors, venue, dates, keywords/MeSH, relevance score and tags. Abstracts and
   full source records are kept only for papers with a job, evaluation or recommendation (full
   provenance for everything a report can show). Identical re-fetches touch nothing but "last seen".
2. **Retention**: brief-info papers older than 45 days lose their per-source records; those below
   the trend-corpus relevance floor are deleted entirely (they only served deduplication).
3. **OpenAlex topic queries only for modelling-methods and AI groups** (60 % of records in the first
   test duplicated PubMed/Europe PMC).
4. **Snapshot format**: a consistent `VACUUM INTO` copy is serialized as deterministic,
   gzip-compressed NDJSON partitioned by rowid (500 rows per file) under `state/`
   (`storage/snapshot.py`). Unchanged partitions are byte-identical, so Git stores them once.
5. **Commit protocol**: Git plumbing with a temporary index; parent = the head that was restored;
   plain fast-forward push; a rejected push raises `StaleDataBranch`, the stage reloads and replays
   (idempotent). No force pushes. Checkpoints and leased jobs are pushed before Claude runs; a report
   commit happens only after the gate passes. A single concurrency group serializes writers.
   GitHub Actions cache is not used for state.

## Measurements after the change (live APIs, 2026-10-22)

| | Before | After |
|---|---|---|
| 14-day snapshot | 12.4 MB, one file | 1.98 MB in 55 files (largest 0.6 MB)¹ |
| Papers keeping abstracts | all | 40 (those queued for Claude) |
| Re-run with no new data (worst case: every paper recent) | full copy | 0.34 MB of changed files |

¹ OpenAlex was unavailable during this measurement (shared keyless budget exhausted by testing), so
the two OpenAlex query groups are not included.

Projection (not measured): ~15–30 MB snapshot for the 180-day window and a few hundred KB of new Git
storage per day (≈0.1–0.2 GB/year).

## Consequences

+ No external database or cost; full history; reproducible restores; no file near 100 MB.
− Changing trend-tag rules re-tags old brief-info papers on title/keywords only (documented).
− Re-assessing a brief-info paper requires it to be re-retrieved with new content.
− If history still grows too large, see `docs/operations.md` → "Data-branch size" for compaction of
  history or migration to object storage with conditional writes.
