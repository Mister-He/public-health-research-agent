# Feedback

- `feedback.csv` — your judgements on recommended papers (`research-agent feedback add …`).
  Labels: highly_relevant, relevant, methodologically_interesting, already_known, too_far_from_core, not_relevant.
- `gold.csv` — a manually labelled evaluation set (relevant 0/1, category core/exploratory,
  novelty/applicability/priority 0–3), kept separate from model scores; used by `research-agent evaluate`.
- `proposals/` — dated, versioned profile-adjustment proposals from `research-agent feedback suggest`.
  They are never applied automatically; copy what you agree with into `config/research_profile.yaml`.
