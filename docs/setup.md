# Setup and deployment

## 1. Local installation

Requirements: Python ≥ 3.11, Git, and [uv](https://docs.astral.sh/uv/) (or pip).

```bash
git clone <your-repo-url> public-health-research-agent && cd public-health-research-agent
uv sync --locked --extra dev          # or: python -m venv .venv && .venv/bin/pip install -e '.[dev]'
cp .env.example .env                   # set RESEARCH_AGENT_CONTACT_EMAIL (and optional API keys)
set -a; . ./.env; set +a
uv run research-agent init             # validates config, creates state/research.sqlite3
uv run research-agent doctor           # config, secrets presence, DB integrity, provider connectivity
uv run pytest                          # offline suite (no network, no credentials)
```

## 2. Create the GitHub repository

```bash
gh repo create <you>/public-health-research-agent --private --source . --push
```

(Private is recommended: reports reproduce abstracts — see "Licensing" in operations.md.)

## 3. Claude Code subscription OAuth token

On a machine where you are logged in to Claude Code with an eligible Pro/Max subscription:

```bash
claude setup-token
```

Store it as a repository **secret** and verify it in one step — never in a file, issue or log:

```bash
scripts/set_claude_token.sh                    # paste the token (hidden); sets the secret, then
                                               # runs the "Claude auth check" workflow and waits
```

The check makes one Claude turn ("Reply OK") with no tools and prints ✅ or the exact error.
GitHub has no "secret changed" trigger, so always change the token through this script (or start
*Actions → Claude auth check → Run workflow* by hand after `gh secret set CLAUDE_CODE_OAUTH_TOKEN`).

Do **not** create `ANTHROPIC_API_KEY`; the workflow refuses to run if that secret exists, so
pay-as-you-go billing can never be used by accident. Re-run `claude setup-token` and update the
secret when the token expires or is revoked (see operations.md → OAuth).

## 4. Other secrets and variables

```bash
gh variable set RESEARCH_AGENT_CONTACT_EMAIL --body "you@your-institution.edu"   # required
gh secret set NCBI_API_KEY        # optional: https://www.ncbi.nlm.nih.gov/account/settings/
gh secret set OPENALEX_API_KEY    # optional, recommended: https://openalex.org/settings/api
```

## 5. Workflow permissions

Settings → Actions → General:
- *Actions permissions*: allow GitHub Actions (or allow `actions/*`, `astral-sh/setup-uv`,
  `anthropics/claude-code-action`).
- *Workflow permissions*: leave the default token **read-only**; each job requests only what it
  needs in YAML (`ingest`/`publish`: `contents: write`, `analyze`: `contents: read`).
- If `main` is protected, allow `github-actions[bot]` to push (or use a ruleset bypass), otherwise
  report commits are rejected. The `research-data` branch must not require pull requests.

The Claude GitHub App is **not** required: the analysis job passes the read-only `GITHUB_TOKEN` to
the action, so no OIDC/app installation is needed.

## 6. First run

1. Actions → *Daily research digest* → *Run workflow* with `dry_run: true`. The first run starts the
   180-day landscape baseline; ingestion is time-boxed (45 min) and resumes on later runs from the
   watermarks until caught up. Check the `ingest` job summary: `landscape: in_progress|completed`,
   provider statuses, watermarks, and the `research-data` branch now exists.
2. Run again without `dry_run`. Inspect the `analyze` job: the Claude step should succeed and the
   *Classify scoring outcome* step should print `"category": null`.
3. The `publish` job commits `reports/YYYY-MM-DD.md` with
   `docs(digest): add public health research digest for YYYY-MM-DD` when validation passes, plus the
   landscape report once the baseline is complete.

## 7. Verify the scheduled run

The schedule is `cron: "0 6 * * *"` with `timezone: "Asia/Shanghai"`. GitHub may start scheduled
runs late; the report date is computed in Asia/Shanghai, so it is still correct. After the first
06:00 run: the run appears with event `schedule`; `logs-publish` → `publish-summary.json` shows
`scheduled_date`, start/completion and duration; a new commit appears on `main` (or the summary says
why no report was published, e.g. `insufficient` with the Claude failure category).

In public repositories GitHub disables scheduled workflows after 60 days without repository
activity. If the schedule is ever disabled (the Actions tab shows a banner), re-enable it there.

## 8. Manual operations

Workflow inputs: `report_date`, `mode` (daily / backfill / validation-only), `backfill_start/end`,
`dry_run`, `max_eval`, `force_metadata_refresh`, `skip_llm`. All are validated before anything runs.
A dry run generates and validates the digest (see the `logs-publish` artifact → `reports/.staging/`)
without publishing it: no report is promoted or committed and nothing is marked as recommended.
Ingestion checkpoints and validated Claude evaluations are still persisted, so the subscription
usage of a dry run is not wasted.

Locally (after `research-agent state restore` to fetch the CI state):

```bash
uv run research-agent queue status
uv run research-agent history --days 14
uv run research-agent digest validate --date 2026-10-21
uv run research-agent replay --date 2026-10-15
```
