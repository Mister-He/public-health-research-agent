"""Deterministic SIMULATED assessments for offline tests and the fixture end-to-end run.

It writes the same output files the Claude Code Action writes, so the import/validation path is
exercised exactly. Scores are derived from the lexical prescreen and are NOT research judgements;
every rationale says so. Never enabled in production configuration."""

from __future__ import annotations

import json
import re
from pathlib import Path

from research_agent.llm.claude_code_action import OUTPUT_DIR, SCORING_DIR, _write_json
from research_agent.llm.structured_output import SCHEMA_VERSION
from research_agent.metadata.normalization import normalize_text_for_match, phrase_in
from research_agent.settings import Settings

LABEL = "[simulated fixture assessment — not a research judgement]"
FIXTURE_MODEL = "fixture-simulated"


def as_fixture(settings: Settings) -> Settings:
    """Settings copy whose model id is FIXTURE_MODEL, so simulated evaluations get their own
    cache keys and are labelled as simulated everywhere (never mistaken for Claude output)."""
    s = settings.model_copy(deep=True)
    s.llm.model = FIXTURE_MODEL
    return s


def _first_sentence(text: str) -> str:
    s = re.split(r"(?<=[.!?])\s+", text.strip())[0]
    s = re.sub(r"\d", "", s)  # keep quotes free of numbers (they must still be verbatim substrings)
    return s.split("  ")[0][:160].strip(" ,;:") or text[:60]


def _quote(rec: dict) -> str:
    """A verbatim fragment from the record without digits (digit-free spans are still substrings)."""
    src = rec.get("abstract") or rec.get("title") or ""
    for chunk in re.split(r"[\d.;:]+", src):
        if len(chunk.split()) >= 4:
            return chunk.strip(" ,()")[:160]
    return (rec.get("title") or "")[:80]


def simulate_scoring(settings: Settings, *, fail_batches: set[str] = frozenset(), malformed_batches: set[str] = frozenset(),
                     ungrounded_ids: set[str] = frozenset()) -> int:
    root = settings.analysis_dir / SCORING_DIR
    manifest = json.loads((root / "manifest.json").read_text())
    taxonomy = {t.id: t for t in settings.trends.tags if t.status in ("active", "watchlist")}
    written = 0
    for b in manifest["batches"]:
        if b["batch_id"] in fail_batches:
            continue
        out = settings.root / b["output"]
        if b["batch_id"] in malformed_batches:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text('{"schema_version": "1.0", "batch_id": "%s", "evaluations": [ {"oops": ' % b["batch_id"])
            continue
        papers = json.loads((settings.root / b["file"]).read_text())["papers"]
        evs = [_evaluate(rec, settings, taxonomy, ungrounded=rec["canonical_id"] in ungrounded_ids) for rec in papers]
        _write_json(out, {"schema_version": SCHEMA_VERSION, "batch_id": b["batch_id"], "evaluations": evs})
        written += 1
    return written


def _evaluate(rec: dict, settings: Settings, taxonomy: dict, ungrounded: bool = False) -> dict:
    topics = rec.get("prescreen_topics") or []
    kind = settings.profile.topic(topics[0]).kind if topics else "none"
    base = {0: 40, 1: 70, 2: 82}.get(min(len(topics), 2), 40)
    has_abs = bool(rec.get("abstract"))
    conf = 0.7 if has_abs else 0.3
    q = _quote(rec)
    text = normalize_text_for_match(f"{rec.get('title')} {rec.get('abstract')}")
    novelty = "new_method" if phrase_in("novel", text) or phrase_in("we propose", text) or phrase_in("new method", text) \
        else ("new_application" if topics else "incremental_application")
    nscore = {"new_method": 78, "new_application": 50, "incremental_application": 25}[novelty]

    def dim(score: float) -> dict:
        return {"score": score if has_abs else min(score, 50), "confidence": conf,
                "rationale": f"{LABEL} Derived from lexical topic matches: {', '.join(topics) or 'none'}.",
                "evidence": ["fabricated quote that is not in the record"] if ungrounded else [q],
                "limitations": [] if has_abs else ["Abstract unavailable; insufficient methods information."]}

    tags = []
    for tid, t in taxonomy.items():
        for conj in t.match:
            if all(phrase_in(p, text) for p in conj):
                tags.append({"tag_id": tid, "confidence": 0.8, "evidence": q})
                break
    return {
        "canonical_id": rec["canonical_id"],
        "metadata_version": rec["metadata_version"],
        "dimensions": {
            "research_alignment": dim(base),
            "methodological_innovation": dim(nscore),
            "methodological_rigor": dim(55),
            "cross_disciplinary_transferability": dim(60 if kind == "exploratory" else 45),
        },
        "profile_category": kind if kind in ("core", "exploratory") else "none",
        "research_domains": topics[:4],
        "topic_tags": tags[:4],
        "untagged_themes": [],
        "methodological_contributions": [f"{LABEL} see source abstract"],
        "novelty_class": novelty if has_abs else "insufficient_information",
        "novelty_basis": "abstract_only" if has_abs else "title_only",
        "methodology_assessment": f"{LABEL} Preliminary, abstract-only classification: {novelty.replace('_', ' ')}.",
        "why_useful": f"{LABEL} Matches profile topics: {', '.join(topics) or 'none'}.",
        "claims": [{"type": "interpretation", "text": f"{LABEL} topic fit inferred from lexical matches"}],
    }


def simulate_narrative(settings: Settings) -> Path:
    m = json.loads((settings.analysis_dir / "narrative" / "manifest.json").read_text())
    themes = sorted({t for p in m["papers"] for t in p.get("matched_trend_tags", [])})
    domains = sorted({d for p in m["papers"] for d in p.get("research_domains", [])})
    text = (
        f"{LABEL} This simulated overview was produced by the offline fixture provider rather than by Claude, "
        "so it only restates structured fields of this week's selection. The selected set is drawn from the configured "
        "research profile, with research domains spanning "
        f"{', '.join(domains) or 'no specific domain'}, and the matched trend tags include "
        f"{', '.join(themes) or 'none of the tracked tags'}. Methodologically, the set mixes simulation and "
        "decision-analytic modelling with statistical and economic evaluation, which is consistent with the profile's "
        "emphasis on model-based public health decision-making. Cross-disciplinary content is limited to what the "
        "lexical screen detected, and no claim is made here about field-wide changes, because a small weekly selection "
        "cannot support such conclusions. In a production run this paragraph is written by Claude from the same "
        "manifest and must pass the same validation: prose only, no per-paper enumeration, no numbers absent from the "
        "manifest, and a word count within the configured range. Readers should treat this fixture paragraph purely as "
        "a demonstration of report structure and of the validation gate rather than as an interpretation of the "
        "literature."
    )
    out = settings.analysis_dir / OUTPUT_DIR / "narrative.json"
    _write_json(out, {"schema_version": SCHEMA_VERSION, "report_date": m["report_date"],
                      "selection_hash": m["selection_hash"], "narrative": text})
    return out
