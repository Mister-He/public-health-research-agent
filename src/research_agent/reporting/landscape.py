"""Initial 180-day research landscape report (deterministic; no LLM text)."""

from __future__ import annotations

import json

from research_agent.reporting.markdown import cell, fmt
from research_agent.settings import Settings
from research_agent.storage.database import Database
from research_agent.timeutil import iso_utc


def render_landscape(settings: Settings, snap, db: Database) -> str:
    payload = json.loads(db.scalar("SELECT payload FROM trend_snapshots WHERE id=?", (snap.id,)))
    names = {t.id: t.name for t in settings.trends.tags}
    win = payload["windows"]
    corp = payload["corpus"]
    tags = payload["tags"]
    by_pop = sorted(tags.items(), key=lambda kv: (-kv[1]["frequency_signal"], kv[0]))
    by_growth = sorted(((k, v) for k, v in tags.items() if v["growth_signal"] is not None),
                       key=lambda kv: (-kv[1]["growth_signal"], kv[0]))
    lines = [
        "---", f"date: {snap.as_of.isoformat()}", "timezone: Asia/Shanghai", f"generated_at_utc: {iso_utc()}",
        "kind: research_landscape_baseline", f"trend_snapshot_version: {snap.version}", "---", "",
        f"# Initial Research Landscape — {snap.as_of.isoformat()}", "",
        "This baseline describes the topic-filtered corpus retrieved over the initial window. Seed tags are "
        "provisional labels from the configuration; the figures below are descriptive counts with uncertainty, "
        "not validated evidence of field-wide trends.", "",
        "## Sample", "",
        f"- Corpus definition: {corp['definition']}",
        f"- Corpus size: {corp['size']} papers ({corp['recent']} in the recent window {win['recent'][0]}..{win['recent'][1]}, "
        f"{corp['baseline']} in the baseline window {win['baseline'][0]}..{win['baseline'][1]})",
        "- Papers by source (a paper can come from several): "
        + ", ".join(f"{k} {v}" for k, v in corp["sources"].items()),
        f"- External signals (OpenAlex field-wide counts) available for: {', '.join(payload['external_available']) or 'none'}",
        "", "## Popularity (share of the recent corpus)", "",
        "| Tag | Seed | Recent tagged / corpus | Share (95% CI) | Frequency signal |", "|---|---|---:|---|---:|",
    ]
    for tid, t in by_pop:
        c = t["counts"]
        lo, hi = t["recent_ci95"]
        lines.append(f"| {cell(names.get(tid, tid))} | {'yes' if t.get('seed') else 'no'} | {c['recent']} / {c['recent_corpus']} | "
                     f"{fmt(c['recent'] / c['recent_corpus'] if c['recent_corpus'] else None, 3)} ({lo:.3f}–{hi:.3f}) | "
                     f"{t['frequency_signal']:.3f} |")
    lines += ["", "## Growth relative to baseline", "",
              "Growth = logistic(log ratio of smoothed recent vs baseline share); 0.5 means no change. Tags below "
              f"the minimum sample ({settings.trends.parameters.min_sample}) are reported as unavailable.", "",
              "| Tag | Baseline tagged / corpus | Growth signal | External signal |", "|---|---:|---:|---:|"]
    for tid, t in by_growth:
        c = t["counts"]
        lines.append(f"| {cell(names.get(tid, tid))} | {c['baseline']} / {c['baseline_corpus']} | {t['growth_signal']:.3f} | "
                     f"{fmt(t['external_signal'], 3)} |")
    unavailable = [names.get(k, k) for k, v in tags.items() if v["growth_signal"] is None]
    if unavailable:
        lines += ["", f"Growth unavailable (insufficient sample): {', '.join(sorted(unavailable))}."]
    lines += ["", "## Notes", "",
              "- Counts use deterministic phrase rules from `config/trends.yaml` over title, abstract and author keywords.",
              "- Recent windows are affected by indexing delay; external signals are normalized by all works per window.",
              "- Edit tags with `research-agent tags ...` or directly in `config/trends.yaml`.", ""]
    return "\n".join(lines)
