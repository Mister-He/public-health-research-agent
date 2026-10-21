# Scoring methodology

## Two-stage screening

1. **Prescreen (every new or changed paper, no LLM).**
   `prescreen = 0.7 × lexical + 0.3 × semantic`.
   - *Lexical*: for each profile topic, title phrase matches weigh 1.0, abstract/keyword matches 0.5,
     MeSH agreement 0.5; topic evidence saturates as `1 − exp(−w/1.2)` and is multiplied by the
     topic's importance. The best topic counts fully, a second distinct topic adds 25 %
     (methods × application). Comments, letters, errata etc. are down-weighted ×0.3; configured
     exclusion terms remove a topic match.
   - *Semantic*: TF-IDF cosine between the paper and each topic's description + keywords, scaled so
     0.35 counts as full evidence. This is a lexical-statistical proxy, not a neural embedding; an
     optional local Ollama embedding model can replace it for local runs.
2. **Shortlist.** Papers first published within the 30-day horizon with prescreen ≥ 0.25, ordered by
   prescreen, are enqueued up to `min(shortlist_max, max_papers_per_run)` per day (exploratory ≤ 30 %).
   Existing jobs/evaluations for the same version are not re-enqueued.
3. **Claude assessment** of leased jobs (default ≤ 40 per run, batches of 8).

## Dimensions (config/scoring.yaml)

| Dimension | Weight | Method | Computed by |
|---|---:|---|---|
| Research alignment | 0.30 | llm | Claude, `prompts/alignment.md` |
| Methodological innovation | 0.25 | llm | Claude, `prompts/innovation.md` (novelty class + basis) |
| Emerging trend relevance | 0.15 | hybrid | Python: `100 × (1 − Π(1 − confidence_t × trend_t))` over Claude-assigned and lexical tags |
| Freshness | 0.15 | deterministic | Python: `100 × 0.5^(age_days / 14)` on the verified first-publication date |
| Evidence and methodological rigor | 0.10 | llm | Claude, `prompts/methodology_review.md` |
| Cross-disciplinary transferability | 0.05 | llm | Claude, `prompts/transferability.md` |

`overall = Σ weightᵢ × scoreᵢ` (enabled weights must sum to 1). Python computes it; Claude never
sees weights. Missing-data policy per dimension: `reject` (unrankable), `zero`, or
`exclude_and_renormalize`. A failed Claude assessment is never replaced by a default score.

## What Claude returns (schema-validated, `llm/structured_output.py`)

Per dimension: `score` (0–100), `confidence` (0–1), `rationale`, `evidence` (verbatim quotes),
`limitations`. Per paper: `profile_category` (core/exploratory/none), `research_domains`,
`topic_tags` (taxonomy ids with evidence), `novelty_class` (new method / new combination / new
application / incremental / not methodological / insufficient information), `novelty_basis`
(abstract-only ⇒ preliminary), `methodological_contributions`, `methodology_assessment`,
`why_useful`, and `claims` classified as reported finding / author claim / interpretation /
external context / speculation / missing information.

## Grounding checks (rejection, not coercion)

- every evidence quote must occur in the supplied record (normalized); unfound quotes are dropped,
  and a dimension with no grounded evidence and confidence > 0.4 rejects the evaluation;
- any number in interpretive text that does not occur in the record rejects the evaluation;
- confidence > 0.5 without an abstract rejects the evaluation;
- unknown tag ids are dropped; `canonical_id` and `metadata_version` must match the job.
Rejected evaluations are retried with backoff and become `permanent_failed` after 3 format failures.

## Ranking

Eligibility: not retracted, not failed validation, verified identifier + title + URL, first
published within the horizon and not after the report date, completeness ≥ 0.5, alignment ≥ 55
(core) / 60 (exploratory), not previously recommended (unless a resurfacing rule applies), and — for
a preprint whose journal version is also a candidate — the journal version only.

Selection is greedy: adjusted = raw − λ·100·max-Jaccard(tags, already selected), λ = 0.10 (bounded:
diversity can reorder near-ties only). At most 2 exploratory papers and a core majority. Papers are
then **presented in raw-score order** (ties: newer first, then canonical id); raw score, raw rank,
adjusted score, diversity penalty and selection step are all stored. Fewer than 10 eligible papers
⇒ fewer recommendations; fewer than 3 ⇒ no digest.

## Versioning and caching

`scoring_version` hashes enabled dimensions/weights/rubrics/params; `rubric_version` hashes LLM
rubric versions. Evaluations are cached by (paper, metadata version, model, prompt version, rubric
version); changing any of them creates new jobs, and old evaluations stay interpretable through
`scoring_configs`.

## Adding a dimension

1. Add it to `config/scoring.yaml` (rebalance weights to sum to 1).
2. For `llm`: write `prompts/<name>.md` and set `prompt:`; the manifest picks it up automatically.
   For `deterministic`/`hybrid`: register an evaluator:

   ```python
   from research_agent.scoring.registry import DimensionScore, register

   @register("open_access_bonus")
   def open_access_bonus(ctx):
       return DimensionScore(100.0 if ctx.paper.open_access_url else 0.0, "deterministic")
   ```
3. Add a test (see `tests/unit/test_scoring_ranking.py::test_custom_dimension_registration`).
