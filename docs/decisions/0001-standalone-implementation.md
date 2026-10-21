# ADR 0001 — Build a standalone implementation (option C)

- Status: accepted
- Date: 2026-10-22
- Deciders: project owner (Yichen He), implementation assistant

## Context

The specification requires persistent DOI/PMID/preprint identity mapping, durable per-query
retrieval watermarks, a durable Claude analysis queue that survives OAuth expiry, an extensible
scoring registry, a trend engine with snapshots, and a fail-closed validation gate, running
unattended in GitHub Actions with `anthropics/claude-code-action` (subscription OAuth only).

Three open-source projects were evaluated by cloning them (2026-10-21) and reading their
implementation and tests, not only their READMEs.

| Criterion | X-PG13/paper-digest | MassEast/paper-agent | udaykdk/litsurvey |
|---|---|---|---|
| License | MIT | MIT | MIT |
| Last push / maintenance | 2026-08-20; single maintainer, very large governance tooling | 2026-10-20; active | 2026-10-17; active |
| Size (Python LOC) | ~15.7k package (+ ~38k repo tooling) | ~5.9k app | ~3.4k |
| Architecture | CLI + delivery integrations (email, Slack, Feishu …), static archive site | Flask web app, SQLAlchemy, Kubernetes manifests | Interactive search CLI/web + agent loop |
| Academic APIs | arXiv, PubMed, Crossref, OpenAlex, Semantic Scholar | arXiv, bioRxiv, Semantic Scholar, publisher pages | PubMed, OpenAlex, Crossref, Europe PMC, arXiv, Semantic Scholar, Unpaywall, IACR |
| Paper model | arXiv-centric dataclass (`paper_id`, `categories`) | `arxiv_id TEXT UNIQUE NOT NULL` — non-arXiv identity is a workaround | plain dicts |
| Incremental ingestion | per-feed "seen ids" in a JSON file, pruned by retention days | nightly crawl of new arXiv/bioRxiv windows | none (one-shot searches) |
| DOI/PMID dedup | no cross-source identity map | DOI column, arXiv-keyed | in-memory, conflict-aware identifier merge (good design) |
| Persistent history | JSON state file | SQL DB (web app) | JSON run history |
| LLM integration | OpenAI-compatible API | OpenAI-compatible API | pluggable backends (CLI agents) |
| Structured scoring extensibility | none (free-form analysis) | relevance screening only | none |
| Trend analysis | none | none | none |
| Markdown generation | yes (digest) | no (web UI) | yes (export) |
| Automated validation of output | docs/policy checks, not article identity | no | no |
| GitHub Actions + claude-code-action OAuth | Actions-oriented, but OpenAI API calls from Python | Kubernetes cron | none |
| Tests | extensive (≈98 files) | moderate (≈35 files) | small (7 files) |

## Options

A. **Extend an existing project.** paper-digest is the closest operationally, but its core types
are arXiv-shaped, its state is a seen-ID JSON file, its LLM path calls an API directly from Python
(which this project must not do), and it carries a large governance/delivery surface. Adapting it
would mean replacing its data model, state, LLM path and scoring — most of the code — while still
maintaining the rest.

B. **Reuse selected modules.** The reusable parts are small, generic utilities (HTTP retry,
per-host throttling, identifier normalization, litsurvey's merge rule). Copying them would create
divergent forks of small functions with different types and error contracts; rewriting them
against this project's typed models is less code to maintain.

C. **Standalone implementation**, borrowing *ideas* (not code):
- litsurvey: "identifiers of the same kind that disagree mean different papers; a preprint DOI and a
  journal DOI are one work in two places, not a conflict" — adopted in `metadata/deduplication.py`.
- litsurvey/paper-digest: per-provider throttling and `Retry-After`-aware backoff.

## Decision

Option C. No third-party code was copied, so no license obligations arise beyond our
dependencies (httpx, pydantic, PyYAML, Jinja2, markdown-it-py — all permissive licenses).
The ideas above are acknowledged here.

## Consequences

+ One typed codebase (~7.7k lines of Python) whose every module maps to a requirement; SQLite schema with
  migrations; a Claude integration that is a file adapter, as required.
+ No dependency on third-party project roadmaps.
− We maintain provider adapters ourselves (mitigated by recorded-response tests and `pytest -m live`).
− Delivery channels (e-mail, Slack) are not included; Git commits are the delivery mechanism.
