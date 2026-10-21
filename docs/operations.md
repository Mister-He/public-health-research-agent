# Operations and troubleshooting

## Exit codes

| Code | Meaning | What happened to state |
|---|---|---|
| 0 | success (or nothing to do) | persisted |
| 1 | blocking failure: validation gate blocked the digest, or all providers failed | persisted; no report |
| 2 | configuration / input error | untouched |
| 3 | analysis deferred: Claude auth/quota/outage and too few verified papers | ingestion + queue persisted; jobs `retryable_failed` |
| 4 | not enough eligible papers (coverage) | persisted; no report |
| 5 | data branch kept moving during retries | not persisted; next run replays |

## Claude failures (analysis job)

The *Classify … outcome* steps write `analysis/output/<stage>_status.json` (category, token counts,
turns — never message content). Categories:

| Category | Typical cause | Effect | Action |
|---|---|---|---|
| `auth` | OAuth token expired/revoked (`OAuth token has expired`, 401) | jobs kept, retried after `quota_retry_hours` (20 h) | run `claude setup-token`, update secret `CLAUDE_CODE_OAUTH_TOKEN`; retries alone cannot fix a revoked token |
| `quota` | subscription usage limit reached (shared with your other Claude Code use) | jobs kept, retried next run; `llm_usage.subscription_limit_event = 1` | wait, or lower `llm.stages.scoring.max_papers_per_run` |
| `max_turns` | turn budget too small for the batch count | partial outputs imported; rest retried | raise `max_turns` or lower `batch_size` × papers |
| `timeout` / `action_failure` | step timeout, runner problem, action error | jobs kept, exponential backoff | check the action log |
| `malformed` / `ungrounded` / `missing_output` | invalid JSON, schema errors, unverifiable quotes or numbers | backoff 6 h → 12 h; `permanent_failed` after 3 | inspect with `research-agent history --paper <id>` |

Never add `ANTHROPIC_API_KEY` as a workaround: the pipeline intentionally has no API fallback.
`research-agent queue retry --eligible` clears backoff for retryable jobs; `--all` also resurrects
permanent failures (an explicit decision).

## Forcing re-scoring

Evaluations are cached per (paper, metadata version, model, prompt version, rubric version). To
re-assess papers deliberately, change one of these in Git — e.g. bump `llm.prompt_version` in
`config/llm.yaml` or a dimension's `rubric_version` in `config/scoring.yaml`. Eligible papers are
then re-queued on the next run, and old evaluations stay stored and interpretable. (There is no
"overwrite" switch, so history is never silently replaced.) Only papers that still keep full
metadata (those already queued/evaluated) can be re-assessed; brief-info papers are re-queued only
if they are re-retrieved with new content.

## Provider failures

A failing provider/query keeps its watermark and is retried next run from the same point;
other providers continue. The footer lists providers unavailable this run. If fewer than
`minimum_successful_providers` succeed for several days, check `ingestion_runs.error`
(`sqlite3 state/research.sqlite3 "select * from ingestion_runs order by id desc limit 20"`).

Known behaviours (verified 2026-10-21):
- OpenAlex without a key: shared $0.10/day per IP and no cursor paging (page paging is used). Add
  `OPENALEX_API_KEY` for reliability.
- Crossref `indexed` dates change on every re-index; the adapter uses `created`.
- medRxiv DOIs use `10.1101/` and, since 2026, `10.64898/`.
- A window with more records than `max_records_per_query_window` fails loudly; reduce `chunk_days`.
- A 429 whose `Retry-After` exceeds 2 minutes (e.g. an exhausted daily budget) fails that provider
  immediately instead of stalling the run; it is caught up next run.

## Missed runs and outages

Each (provider, query) resumes from its last durable watermark minus the 7-day overlap and catches
up in bounded chunks (`max_chunks_per_query_per_run`), so a week-long outage is recovered over the
next run(s) without gaps. Analysis jobs keep their original intended report date and are leased
oldest first. To regenerate digests for missed dates: run the workflow with `mode: backfill`
(trend snapshots and selections then only use papers published/discovered by each date).

## State inspection and manual edits

```bash
research-agent state restore                 # data branch -> state/research.sqlite3
research-agent queue status
research-agent history --paper doi:10.1371/journal.pmed.1004000
sqlite3 state/research.sqlite3 'select provider, query_id, watermark_date, status from retrieval_checkpoints'
research-agent state persist --message "chore(data): manual fix"   # fast-forward only; refused if CI moved it
```

## Data-branch size

Check with `git count-objects -vH` after `git fetch origin research-data`. If history grows beyond a
few hundred MB, either (a) start a fresh history: create an orphan commit from the latest snapshot
and replace the branch — a deliberate, manual force-push performed only when no workflow is running
(disable the schedule first); or (b) migrate to object storage (S3/R2/GCS): replace
`DataBranch.restore/persist` with object get/put using conditional writes (ETag/generation match),
which keeps the same stale-write protection.

## Strict timing

GitHub's scheduler can start late under load. For strict 06:00 delivery, run the same CLI on a
persistent host (self-hosted runner or a server with cron + `TZ=Asia/Shanghai`), or trigger
`workflow_dispatch` from an external scheduler. Claude analysis still needs the action (or a manual
local Claude Code run on the manifest files).

## Licensing of abstracts

Abstracts are reproduced verbatim with their source. Publisher copyright may apply; keep the
repository private, or set `reporting.abstract_policy: link_only` (or list licences in
`link_only_licenses`) to publish links only.

## Logs

Console logs are human-readable; `logs/run.jsonl` is JSON lines with secrets redacted;
`logs/*-summary.json` are the execution records uploaded as artifacts (14 days).
