# Public Health Research Intelligence Agent

[中文说明](README.zh-CN.md)

An unattended literature monitor for a computational public-health researcher. Every day at
**06:00 Asia/Shanghai**, it:

1. retrieves new and newly indexed papers from **PubMed, Europe PMC, OpenAlex, medRxiv** (topic
   queries) and **Crossref** (high-priority journals by ISSN), resuming from durable per-query
   watermarks;
2. normalizes and deduplicates them on DOI/PMID/PMCID/preprint identifiers, with source provenance
   for every field;
3. prescreens cheaply, then sends a bounded shortlist to **Claude via `anthropics/claude-code-action`
   (Claude Code subscription OAuth, no API key)** for structured, evidence-grounded assessment;
4. maintains a versioned trend-tag database (180-day baseline, daily updates, weekly reassessment);
5. computes weighted scores **in Python**, selects up to 10 papers, and writes
   `reports/YYYY-MM-DD.md` with a narrative overview and full score breakdowns;
6. **independently validates** the file against stored records and authoritative sources; only a
   passing digest is committed (`docs(digest): add public health research digest for YYYY-MM-DD`).

If Claude is unavailable (expired token, usage limit), ingestion still completes, the backlog is kept
and retried, and no unverified report is published.

**Optional DeepSeek provider:** set `provider: deepseek` / `auth: api_key` in `config/llm.yaml` and
add a `DEEPSEEK_API_KEY` secret to use the DeepSeek API (pay-per-use) instead of Claude. Same prompts,
same JSON contract, same validation; one provider per run, never an automatic fallback.

→ Architecture: [docs/architecture.md](docs/architecture.md) · Decisions: [docs/decisions/](docs/decisions/)
· Example output (synthetic fixture data): [docs/examples/fixture-digest-2026-10-21.md](docs/examples/fixture-digest-2026-10-21.md)

## Supported sources

| Source | Role | Incremental axis |
|---|---|---|
| PubMed (E-utilities) | topic queries (`[tiab]`, MeSH) | Entrez date (EDAT) |
| Europe PMC | topic queries, incl. preprints | first index date |
| OpenAlex | topic queries (`title_and_abstract.search`), external trend signals | publication date + overlap |
| medRxiv | full daily window, screened locally; versions + journal-publication links | posting date |
| Crossref | journal-driven discovery by ISSN; DOI identity checks | DOI registration (`created`) |
| arXiv (optional, off) | computational methods | submission date |

High-priority journals: The Lancet Public Health, The Lancet Regional Health – Western Pacific,
PLOS Medicine, BMC Medicine, JAMA Network Open; plus The Lancet, Lancet Digital Health, Lancet Global
Health, Nature Medicine, Nature Methods, BMJ, Health Affairs, EClinicalMedicine, and health-economics,
epidemiology, biostatistics, mathematical-biology and informatics journals (`config/journals.yaml`).

## Install and run locally

```bash
uv sync --locked --extra dev            # Python ≥ 3.11
cp .env.example .env && $EDITOR .env    # RESEARCH_AGENT_CONTACT_EMAIL (+ optional NCBI/OpenAlex keys)
set -a; . ./.env; set +a
uv run research-agent init
uv run research-agent doctor
uv run pytest                            # offline suite; `uv run pytest -m live` hits real APIs
```

Claude runs only inside GitHub Actions. Local commands therefore either reuse cached evaluations
(`--skip-llm`) or use the **fixture provider** (`--provider fixture`), which produces clearly labelled
*simulated* scores under the model id `fixture-simulated` for testing:

```bash
uv run research-agent research-landscape --lookback-days 180 --time-budget 2700
uv run research-agent run --date 2026-10-21 --dry-run --provider fixture   # simulated scores
uv run research-agent digest validate --date 2026-10-21
```

## Deploy (GitHub Actions)

Full guide: [docs/setup.md](docs/setup.md). In short:

```bash
gh repo create <you>/public-health-research-agent --private --source . --push
claude setup-token                                  # with an eligible Pro/Max subscription
gh secret set CLAUDE_CODE_OAUTH_TOKEN               # paste the token
gh variable set RESEARCH_AGENT_CONTACT_EMAIL --body you@institution.edu
gh secret set OPENALEX_API_KEY                      # optional, recommended
gh secret set NCBI_API_KEY                          # optional
```

Never create an `ANTHROPIC_API_KEY` secret — the workflow refuses to run if it exists, so
pay-as-you-go billing cannot be triggered. Then run *Actions → Daily research digest → Run workflow*
with `dry_run: true` once; the schedule takes over afterwards.

Manual inputs: report date, mode (daily / backfill / validation-only), backfill range, dry run,
maximum evaluated papers, force metadata refresh, skip LLM.

## Configure

Everything is YAML under `config/` (see [docs/configuration.md](docs/configuration.md)):

- **Research topics** — `config/research_profile.yaml`: add a topic with `id`, `name`, `kind`
  (`core`/`exploratory`), `importance`, `description`, `keywords`, optional `mesh_terms`, `exclude`.
- **Search queries** — `config/search_queries.yaml`: seed-phrase groups; provider syntax is generated.
- **Journals** — `config/journals.yaml`: add `{id, name, issns: [print, electronic], enabled: true}`;
  verify ISSNs at `https://api.crossref.org/journals/<issn>` and `https://api.openalex.org/sources/issn:<issn>`.
- **Scoring** — `config/scoring.yaml`: weights (must sum to 1), rubric versions, new dimensions
  ([docs/scoring_methodology.md](docs/scoring_methodology.md#adding-a-dimension)).
- **Trend tags** — `config/trends.yaml` or `research-agent tags add|rename|merge|archive|reactivate|pin|override|accept`
  ([docs/trend_methodology.md](docs/trend_methodology.md)).
- **Claude limits** — `config/llm.yaml`: model alias, turns, batch size, papers per run, retry policy.

## Commands

| Command | Purpose |
|---|---|
| `init`, `doctor` | create/migrate storage, validate config; check secrets presence (never values), DB, connectivity |
| `research-landscape --lookback-days 180` | initial baseline + `reports/landscape/<date>.md` |
| `fetch --date D` | checkpointed retrieval and normalization |
| `score --date D [--provider manifest|import|fixture]` | lease jobs into a Claude manifest / import outputs |
| `trends update [--weekly]` | trend snapshot |
| `digest generate|validate --date D [--offline] [--publish]` | render to staging / independent validation |
| `run --date D [--dry-run] [--skip-llm] [--provider fixture]` | complete local pipeline |
| `backfill --start A --end B` | digests for past dates without look-ahead |
| `queue status`, `queue retry --eligible` | inspect / release the analysis backlog |
| `history [--paper ID]` | recommendation history and per-paper audit trail |
| `feedback add|import|suggest`, `evaluate`, `replay --date D` | quality evaluation (see below) |
| `state restore|persist` | fetch/push the data-branch snapshot |
| `ci ingest|prepare|publish|validate|backfill|classify|claude-args` | GitHub Actions stages |

Exit codes: 0 ok · 1 blocked/failed · 2 config/input error · 3 analysis deferred (Claude outage) ·
4 not enough eligible papers · 5 stale data branch. See [docs/operations.md](docs/operations.md).

## Validation

Every digest is checked for file name/date/front matter, structure (on a Markdown token stream),
field-by-field agreement with the canonical records, provenance of every field, article identity
against Crossref/PubMed/medRxiv, landing-page identity (robots.txt-aware, bot-protection tolerant),
and full score recomputation. Failing papers are refreshed, excluded and refilled (≤ 3 rounds); a
failing digest is never committed. Details: [docs/validation_policy.md](docs/validation_policy.md).

## Recommendation-quality evaluation

- Feedback: `research-agent feedback add --canonical-id doi:… --label highly_relevant|relevant|not_relevant|already_known|methodologically_interesting|too_far_from_core`
  (stored in `feedback/feedback.csv`); `feedback suggest` writes *proposed* profile changes to
  `feedback/proposals/` and never edits your configuration.
- Gold set: `feedback/gold.csv` (`canonical_id, relevant, category, novelty, applicability, priority`);
  `research-agent evaluate` reports Precision@10, NDCG@10, recall on the curated set, diversity,
  duplicate rate, validation-failure rate — or `None` when there is no labelled data.
- Replay: `research-agent replay --date D` re-ranks on a throwaway copy using only papers discovered
  by that date.

## Costs and limits

- **Claude**: uses your Claude Code subscription allowance (shared with your other Claude Code use).
  Daily workload is bounded by `llm.yaml` (default ≤ 40 papers in batches of 8, ≤ 30 turns, plus a
  short narrative call and a weekly proposal call). Usage-limit hits are recorded and the backlog is
  retried; no paid API fallback exists.
- **GitHub Actions**: roughly 10–30 runner-minutes per day (longer during the initial baseline);
  free for public repositories and within the included minutes of most private plans.
- **APIs**: all free. OpenAlex keyless use shares a small daily budget per IP; a free key is recommended.
- Optional paid costs: none required.

## Troubleshooting

See [docs/operations.md](docs/operations.md): OAuth expiry (`auth`), usage limits (`quota`),
provider outages, missed runs, data-branch size, strict timing, abstract licensing.

## Repository layout

`config/` YAML · `prompts/` version-controlled task prompts · `src/research_agent/` code ·
`tests/` unit, integration (recorded API responses), end-to-end (synthetic fixtures), live ·
`reports/` verified digests · `docs/` documentation and ADRs · `.github/workflows/` automation.
State lives on the `research-data` branch, not on `main`.

## License

MIT (see `LICENSE`). Reproduced abstracts remain subject to their publishers' terms.
