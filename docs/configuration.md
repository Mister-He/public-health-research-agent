# Configuration

All behaviour is in `config/*.yaml`, validated at load (`research-agent init` or any command;
invalid configuration exits with code 2 and names the file). The configuration hash is recorded in
every run and report.

| File | Controls |
|---|---|
| `research_profile.yaml` | Topics: id, name, description, keywords/synonyms, MeSH terms, `importance` (0–1), `kind` core/exploratory, optional `application_domain`, `exclude` terms; global exclusions |
| `search_queries.yaml` | Strategy A query groups (seed phrases, MeSH + anchors, required context terms with `*` prefix wildcards) and which providers run them |
| `journals.yaml` | Strategy B journals by verified ISSN (print + electronic), priority, enabled |
| `sources.yaml` | Provider endpoints, client-side rate limits, page sizes, chunk sizes, per-window record limits, HTTP timeouts/retries, minimum providers |
| `scoring.yaml` | Dimensions: id, name, description, weight, method, rubric_version, prompt/evaluator, params, missing_data_policy |
| `trends.yaml` | Taxonomy (status, pinned, aliases, parents, match rules, manual_override, seed) and trend parameters (windows, weights, smoothing, minimum sample, corpus threshold, reassessment weekday, external signals, proposal limits) |
| `ranking.yaml` | Max recommendations, minimum verified papers, horizon, relevance thresholds, exploratory cap, completeness minimum, diversity λ, resurfacing rules |
| `llm.yaml` | Analysis provider: `claude_code_action` + `oauth_subscription` (default) or `deepseek` + `api_key` (optional, pay-per-use); `allow_api_key_fallback: false` (enforced — never switches automatically), model, prompt version, per-stage turn/time/batch/workload limits, DeepSeek model/token budget, shortlist sizes, queue retry policy, optional local Ollama |
| `reporting.yaml` | Time zone, narrative word range, abstract policy (`full` / `link_only`, per-license overrides), footer |
| `validation.yaml` | Online identity and URL checks, timeouts, repair rounds, title-match ratio |
| `runtime.yaml` | DB path, windows (overlap 7 d, reconciliation 30 d, baseline 180 d, horizon 30 d, backfill max), chunks per run, data-branch name/path, git remote/branch |

## Documented defaults (no hidden configuration)

| Decision | Default | Where |
|---|---|---|
| Maximum candidate age (recommendation horizon) | 30 days | `ranking.publication_horizon_days` |
| Core / exploratory relevance threshold | 55 / 60 | `ranking.*_relevance_threshold` |
| Maximum exploratory selections | 2 | `ranking.max_exploratory` |
| Minimum verified papers to publish | 2 | `ranking.min_verified_papers` |
| Minimum metadata completeness | 0.5 | `ranking.min_metadata_completeness` |
| Daily Claude workload | ≤ 40 papers, batches of 8, 30 turns | `llm.stages.scoring` |
| Maximum daily API cost | no paid API is used; Claude usage counts against the subscription and is bounded by the workload/turn limits above | `llm.yaml` |
| Search backfill window | 180-day baseline; overlap 7 days; backfill ≤ 365 days | `runtime.windows` |
| Prescreen shortlist threshold | 0.25 | `retrieval/screening.py` (`MIN_PRESCREEN`) |
| Journal impact floor (unlisted journals) | OpenAlex 2-yr mean citedness 2.0 | `ranking.journal_floor` |
| Journal impact weight / trusted minimum / preprint score | 0.15 / 85 / 40 | `scoring.yaml` → `journal_impact` |

## Environment variables

| Variable | Where | Purpose |
|---|---|---|
| `RESEARCH_AGENT_CONTACT_EMAIL` | repo **variable** + local `.env` | identifies the tool to NCBI/Crossref/OpenAlex (required etiquette) |
| `NCBI_API_KEY` | secret (optional) | 10 instead of 3 PubMed requests/s |
| `OPENALEX_API_KEY` | secret (optional, recommended) | own $1/day budget and cursor paging (keyless: shared $0.10/day per IP, page paging) |
| `CLAUDE_CODE_OAUTH_TOKEN` | secret (required in CI) | passed only to `claude-code-action` |
| `DEEPSEEK_API_KEY` | secret (only with `provider: deepseek`) | pay-per-use DeepSeek API key; exposed only to the DeepSeek workflow steps |
| `ANTHROPIC_API_KEY` | must **not** exist | the workflow fails if this secret is present |
