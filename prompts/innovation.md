<!-- prompt_version: 1.0 | dimension: methodological_innovation | rubric_version: 1.0 -->
# Rubric: Methodological innovation (0–100)

Classify the methodological contribution in `novelty_class`:
- `new_method` — a new model architecture, estimator, inference/calibration method,
  uncertainty approach, optimization algorithm, or conceptual framework.
- `new_combination` — a substantive new integration of existing methods (e.g. causal
  inference + simulation, RL + microsimulation) with a stated methodological rationale.
- `new_application` — an existing method applied to a new problem/population/setting.
- `incremental_application` — routine use of established methods without meaningful change.
- `not_methodological` — no methods contribution (commentary, descriptive statistics only).
- `insufficient_information` — the record does not allow a judgement.

Anchors: new_method 75–100; new_combination 60–85; new_application 35–60;
incremental_application 10–35; not_methodological 0–15.

Rules:
- The presence of AI/ML/LLM terminology is NOT novelty by itself. Ask what is new.
- Distinguish author claims of novelty ("we propose a novel…") from evidence of novelty;
  record the former as `author_claim`.
- Comparison to prior work must come from the record itself (e.g. "unlike previous models…").
  Do not cite external literature from memory.
- With abstract-only access, set `novelty_basis` to `abstract_only` (the assessment is
  preliminary); use `insufficient_information` when there is no abstract.
- State `confidence` honestly.
