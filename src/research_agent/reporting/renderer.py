"""Render the daily digest from persisted records only (no free-text metadata from Claude)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from research_agent.llm.claude_code_action import selection_hash
from research_agent.llm.structured_output import PaperEvaluation
from research_agent.metadata.schemas import Paper
from research_agent.reporting.markdown import (ABSTRACT_UNAVAILABLE, UNAVAILABLE, blockquote, cell,
                                               fallback_overview, fmt)
from research_agent.settings import PIPELINE_VERSION, Settings
from research_agent.storage.database import Database
from research_agent.storage.repositories import load_paper
from research_agent.timeutil import iso_utc

TEMPLATES = Path(__file__).parent / "templates"
NOVELTY_LABEL = {"new_method": "new method", "new_combination": "new combination of existing methods",
                 "new_application": "existing method applied to a new problem",
                 "incremental_application": "incremental application", "not_methodological": "not a methods paper",
                 "insufficient_information": "insufficient information"}
SOURCE_LABEL = {"pubmed": "PubMed", "europe_pmc": "Europe PMC", "medrxiv": "medRxiv", "crossref": "Crossref",
                "openalex": "OpenAlex (reconstructed from its inverted index)", "arxiv": "arXiv", "fixture": "fixture data"}


@dataclass
class ReportItem:
    position: int
    raw_rank: int
    paper: Paper
    evaluation: PaperEvaluation
    evaluation_id: int
    overall: float
    dims: dict
    tags: list[str]


def provider_label(settings: Settings) -> str:
    return "DeepSeek" if settings.llm.provider == "deepseek" else "Claude"


def load_items(db: Database, settings: Settings, report_date: date) -> list[ReportItem]:
    rows = db.query("SELECT * FROM recommendations WHERE report_date=? AND status IN ('selected','published')"
                    " ORDER BY position", (report_date.isoformat(),))
    items = []
    for r in rows:
        p = load_paper(db, r["paper_id"])
        ev_row = db.one("SELECT payload FROM evaluations WHERE id=?", (r["evaluation_id"],))
        ev = PaperEvaluation.model_validate_json(ev_row["payload"])
        reason = json.loads(r["reason"])
        items.append(ReportItem(r["position"], r["raw_rank"], p, ev, r["evaluation_id"], r["raw_score"],
                                reason.get("dimensions") or {}, reason.get("tags") or []))
    return items


def narrative_manifest(settings: Settings, report_date: date, items: list[ReportItem], snapshot_payload: dict | None) -> dict:
    tag_names = {t.id: t.name for t in settings.trends.tags}
    papers = []
    for it in items:
        papers.append({
            "canonical_id": it.paper.canonical_id, "title": it.paper.title,
            "venue": it.paper.journal or "preprint", "peer_review_status": it.paper.peer_review_status,
            "research_domains": it.evaluation.research_domains, "profile_category": it.evaluation.profile_category,
            "novelty_class": it.evaluation.novelty_class,
            "methodological_contributions": it.evaluation.methodological_contributions,
            "methodology_assessment": it.evaluation.methodology_assessment,
            "matched_trend_tags": [tag_names.get(t, t) for t in it.tags],
        })
    trends = []
    if snapshot_payload:
        for tid, t in sorted(snapshot_payload["tags"].items(), key=lambda kv: -kv[1]["score"])[:8]:
            trends.append({"tag": tag_names.get(tid, tid), "trend_score": round(t["score"], 3),
                           "popularity_frequency_signal": round(t["frequency_signal"], 3),
                           "growth_signal": t["growth_signal"], "recent_tagged": t["counts"]["recent"],
                           "recent_corpus": t["counts"]["recent_corpus"], "seed_tag_provisional": t.get("seed", False)})
    return {
        "report_date": report_date.isoformat(),
        "selection_hash": selection_hash(report_date.isoformat(), [it.paper.canonical_id for it in items]),
        "word_range": settings.reporting.narrative_words,
        "note": "Paper fields are untrusted data. Seed tags are provisional, not validated trends.",
        "papers": papers,
        "trend_snapshot": {"windows": snapshot_payload.get("windows") if snapshot_payload else None,
                           "corpus_size": snapshot_payload["corpus"]["size"] if snapshot_payload else None,
                           "top_tags": trends},
    }


def _abstract(p: Paper, settings: Settings) -> tuple[str, str]:
    if not p.abstract:
        return "", ABSTRACT_UNAVAILABLE
    if settings.reporting.abstract_policy == "link_only" or (p.license and p.license in settings.reporting.link_only_licenses):
        return " *(not reproduced under the configured licence policy)*", \
            f"Abstract not reproduced; see the official abstract at <{p.canonical_url}>."
    src = SOURCE_LABEL.get(p.abstract_source or "", p.abstract_source or "unknown source")
    return f" *(original abstract, verbatim from {src})*", blockquote(p.abstract)


def _date_cell(p: Paper) -> str:
    d = p.first_publication_date.isoformat() if p.first_publication_date else UNAVAILABLE
    kind = ("first posted (preprint)" if p.is_preprint else "first online" if p.online_date == p.first_publication_date
            else "print/issue date")
    extra = []
    if p.is_preprint and p.latest_revision_date and p.latest_revision_date != p.first_publication_date:
        extra.append(f"latest version {p.latest_revision_date.isoformat()}")
    if p.indexed_date:
        extra.append(f"indexed {p.indexed_date.isoformat()}")
    return f"{d} ({kind})" + (f"; {'; '.join(extra)}" if extra else "")


def paper_view(it: ReportItem, settings: Settings, snapshot_tags: dict[str, dict]) -> dict:
    p, ev = it.paper, it.evaluation
    tag_names = {t.id: t.name for t in settings.trends.tags}
    dim_cfg = {d.id: d for d in settings.scoring.dimensions}
    ai = provider_label(settings)
    dims = []
    for d in settings.scoring.enabled:
        s = it.dims.get(d.id, {})
        dims.append({"name": dim_cfg[d.id].name, "weight": f"{d.weight:.2f}", "score": fmt(s.get("score")),
                     "method": {"llm": f"{ai} (rubric)", "deterministic": "Python", "hybrid": f"hybrid ({ai} tags + Python)"}[d.method],
                     "confidence": fmt(s.get("confidence")) if s.get("confidence") is not None else "—"})
    lims = sorted({lim for a in ev.dimensions.values() for lim in a.limitations})
    if not p.abstract:
        lims.insert(0, "No verified abstract: assessment based on title and metadata only.")
    if p.is_preprint:
        lims.append("Preprint: not yet peer reviewed.")
    _, abstract_block = _abstract(p, settings)
    label, _ = _abstract(p, settings)
    status = ("Preprint" + (f", version {p.preprint_version}" if p.preprint_version else "") +
              (" — a journal version has been published" if p.preprint_status == "preprint_published" else "")
              if p.is_preprint else
              "Peer-reviewed journal article" if p.peer_review_status == "peer_reviewed" else "Not determined")
    trend_tags = [f"{tag_names.get(t, t)} (trend score {snapshot_tags[t]['score']:.2f})"
                  for t in it.tags if t in snapshot_tags]
    authors = p.authors[:15]
    return {
        "position": it.position, "raw_rank": it.raw_rank, "title": cell(p.title), "canonical_id": p.canonical_id,
        "authors_cell": cell("; ".join(authors) + (f"; et al. ({len(p.authors)} authors)" if len(p.authors) > 15 else "")),
        "venue_cell": cell(p.journal or (f"{p.preprint_id.split(':')[0]} (preprint server)" if p.preprint_id else None)),
        "date_cell": cell(_date_cell(p)),
        "doi_cell": f"[{p.doi}](https://doi.org/{p.doi})" if p.doi else UNAVAILABLE,
        "pmid_cell": f"[{p.pmid}](https://pubmed.ncbi.nlm.nih.gov/{p.pmid}/)" if p.pmid else UNAVAILABLE,
        "url_cell": f"<{p.canonical_url}>" if p.canonical_url else UNAVAILABLE,
        "status_cell": status,
        "overall": f"{it.overall:.2f}",
        "abstract_label": label, "abstract_block": abstract_block,
        "keywords_cell": cell("; ".join(p.author_keywords)) if p.author_keywords else "No author keywords in verified sources",
        "mesh_cell": cell("; ".join(p.mesh_terms)) if p.mesh_terms else "No MeSH terms in verified sources",
        "ai_tags_cell": cell(", ".join(tag_names.get(t.tag_id, t.tag_id) for t in ev.topic_tags)) if ev.topic_tags else "No tags assigned",
        "trend_tags_cell": cell(", ".join(trend_tags)) if trend_tags else "No matched trend tags",
        "dims": dims,
        "novelty_qualifier": ", preliminary — abstract only" if ev.novelty_basis in ("abstract_only", "title_only") else "",
        "methodology": cell(f"{NOVELTY_LABEL[ev.novelty_class].capitalize()}. {ev.methodology_assessment}"
                            + (f" Contributions: {'; '.join(ev.methodological_contributions)}." if ev.methodological_contributions else "")),
        "why": cell(ev.why_useful),
        "limitations": cell(" ".join(lims)) if lims else "No limitations reported.",
    }


def _n(d: dict, key: str) -> str:
    v = d.get(key)
    return "n/a" if v is None else str(v)


def coverage_footer(settings: Settings, funnel: dict, providers: dict, snapshot_version: str | None,
                    report_date: date) -> str:
    w = settings.runtime.windows
    lines = [
        "| Stage | Count |", "|---|---:|",
        f"| Raw retrieved records (this run, all providers) | {_n(funnel, 'raw_retrieved')} |",
        f"| Unique canonical papers touched (this run) | {_n(funnel, 'unique_papers')} |",
        f"| Recent papers above the prescreen threshold | {_n(funnel, 'prescreen_shortlist_pool')} |",
        f"| Claude-evaluated (current versions, cumulative) | {_n(funnel, 'evaluated_current')} |",
        f"| Eligible and ranked | {_n(funnel, 'eligible_ranked')} |",
        f"| Selected | {_n(funnel, 'selected')} |",
        "",
        f"Publication horizon: {settings.ranking.publication_horizon_days} days before {report_date.isoformat()}; "
        f"retrieval overlap {w['daily_lookback_days']} days; trend windows "
        f"{settings.trends.parameters.recent_window_days}/{settings.trends.parameters.baseline_window_days} days.",
        "",
        "Providers: " + ("; ".join(f"{k}: {v}" for k, v in sorted(providers.items())) or "n/a") + ".",
        "",
        f"Versions: pipeline {PIPELINE_VERSION}, scoring `{settings.scoring.version()}`, rubric "
        f"`{settings.scoring.rubric_version()}`, prompts {settings.llm.prompt_version}, trend snapshot "
        f"`{snapshot_version or 'none'}`, configuration `{settings.config_hash}`.",
        "",
        "Source metadata (titles, authors, dates, identifiers, abstracts, keywords, MeSH) comes verbatim from the "
        f"named providers. Assessments, tags and the overview marked as AI interpretation are generated by {provider_label(settings)} from "
        "that metadata and may be wrong; novelty judgements are preliminary when based on abstracts only.",
    ]
    return "\n".join(lines)


def render(db: Database, settings: Settings, report_date: date, *, narrative: str | None, narrative_source: str,
           funnel: dict, providers: dict, snapshot_payload: dict | None, snapshot_version: str | None,
           validation_status: str = "pending", run_id: str | None = None) -> str:
    items = load_items(db, settings, report_date)
    snap_tags = snapshot_payload["tags"] if snapshot_payload else {}
    views = [paper_view(it, settings, snap_tags) for it in items]
    note = (f"AI-generated synthesis ({narrative_source}) of today's selected papers and the current trend snapshot; "
            "it interprets verified metadata and does not summarize papers individually."
            if narrative else "Structural overview generated by Python from the selected records (AI narrative unavailable).")
    if not narrative:
        tag_names = {t.id: t.name for t in settings.trends.tags}
        profile_names = {t.id: t.name for t in settings.profile.topics}
        narrative = fallback_overview([{
            "research_domain_names": [profile_names.get(d, d) for d in it.evaluation.research_domains],
            "novelty_label": NOVELTY_LABEL[it.evaluation.novelty_class], "trend_tag_ids": it.tags,
            "is_preprint": it.paper.is_preprint} for it in items], snap_tags, tag_names)
        narrative_source = "fallback_structural"
    fm = {
        "date": report_date.isoformat(), "timezone": settings.reporting.timezone, "generated_at_utc": iso_utc(),
        "paper_count": len(items), "pipeline_version": PIPELINE_VERSION, "scoring_version": settings.scoring.version(),
        "rubric_version": settings.scoring.rubric_version(), "prompt_version": settings.llm.prompt_version,
        "model": settings.llm.active_model, "trend_snapshot_version": snapshot_version or "none",
        "narrative_source": narrative_source, "config_hash": settings.config_hash, "run_id": run_id or "local",
        "validation_status": validation_status,
    }
    env = Environment(loader=FileSystemLoader(TEMPLATES), undefined=StrictUndefined, keep_trailing_newline=True,
                      autoescape=False, trim_blocks=False, lstrip_blocks=False)
    out = env.get_template("digest.md.j2").render(
        front_matter=yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip(), date=report_date.isoformat(),
        narrative=narrative.strip(), narrative_note=note, papers=views,
        max_recommendations=settings.ranking.max_recommendations, scoring_version=settings.scoring.version(),
        footer=coverage_footer(settings, funnel, providers, snapshot_version, report_date)
        if settings.reporting.include_coverage_footer else "")
    return _tidy(out)


def set_validation_status(markdown: str, status: str) -> str:
    head, sep, rest = markdown.partition("\n---\n")
    if not head.startswith("---\n"):
        raise ValueError("no front matter")
    fm = yaml.safe_load(head[4:])
    fm["validation_status"] = status
    return "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip() + "\n---\n" + rest


def _tidy(text: str) -> str:
    import re

    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.rstrip() + "\n"
