# Trend methodology

## Corpus

Trend statistics use the **topic-filtered candidate corpus** — every stored paper with
`prescreen_score ≥ 0.15` whose evidence date (first publication, else index date) falls in the
recent (30 d) or baseline (180 d) window — not just recommended papers. Tag membership for counting
uses deterministic phrase rules (`match` in `config/trends.yaml`: a list of conjunctions) over title,
abstract and author keywords, so counts are reproducible. Tags are assigned when a paper is
screened; papers later reduced to brief info keep those assignments. **Limitation:** if you change a
tag's match rules or add a tag, older brief-info papers can only be re-matched on their title and
keywords (their abstracts are no longer stored), so the new tag's baseline is somewhat undercounted
until new papers accumulate.

## Signals per tag

```
p_recent   = (c_r + 1) / (N_r + 2)                 (additive smoothing; no divide-by-zero)
p_baseline = (c_b + 1) / (N_b + 2)
frequency  = 1 − exp(−p_recent / 0.05)             popularity (absolute; 5 % share → 0.63)
growth     = logistic(ln(p_recent / p_baseline) / 0.7)   0.5 = no change
             UNAVAILABLE if c_r + c_b < 5, N_r < 5 or N_b < 5
external   = logistic(ln(share_recent / share_prior) / 0.7) from OpenAlex field-wide counts
             (90-day windows, normalized by all works per window to correct for indexing lag),
             refreshed weekly; UNAVAILABLE if the API fails or the sample is too small
raw        = Σ w·signal over AVAILABLE signals, weights (0.5/0.3/0.2) renormalized
score      = 0.3 · raw + 0.7 · previous score          (EMA: a one-day spike cannot dominate)
```

A pinned tag with `manual_override` uses the override (noted in the snapshot). Unavailable signals
are recorded as such; they are never imputed.

## Cadence

- **Initialization**: a 180-day landscape baseline (`research-agent research-landscape`, or
  automatically on the first CI runs until caught up), with a report under `reports/landscape/`
  giving sample sizes, windows, sources, Wilson 95 % intervals, and popularity separately from growth.
- **Daily**: a `daily` snapshot (incremental counts; latest external signals ≤ 14 days old).
- **Weekly** (configured weekday, default Monday, Asia/Shanghai): a `weekly` snapshot with refreshed
  external signals, plus clustering of recent relevant papers that match **no** tag; Claude may
  propose up to 3 tags with verbatim evidence and supporting paper ids from the clusters. Proposals
  are stored in `tag_proposals` (status `proposed`) and never become active automatically.
- If Claude is unavailable, the last valid snapshot remains in use and weekly proposals are skipped.

## Reproducibility and history

Snapshots are unique per (date, kind, algorithm+parameter+taxonomy hash) and re-runs update them in
place. `trend_observations` stores per-tag window counts; `topic_tags` holds the latest statistics;
`tag_events` records every change. Historical replay (`research-agent replay --date`) rebuilds a
snapshot using only papers discovered on or before the date.

## Seed tags

The twelve seed tags are provisional labels from the specification, marked `seed: true`. Their
appearance in reports does not mean they are validated trends; the landscape report and snapshots
show the actual counts and uncertainty.

## Managing tags

`config/trends.yaml` is authoritative for user decisions. CLI helpers edit it (and bump
`taxonomy_version`); commit the file for CI to use it:

```bash
research-agent tags add --name "Microsimulation emulators" --description "…" --match "emulator + microsimulation"
research-agent tags rename --tag <id> --name "New name"
research-agent tags merge --tag <source_id> --into <target_id>
research-agent tags archive|reactivate|watch|pin|unpin --tag <id>
research-agent tags override --tag <id> --value 0.8     # negative value clears
research-agent tags proposals                           # needs the restored DB (state restore)
research-agent tags accept --proposal 3                 # adds as `watchlist`
```
