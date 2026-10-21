"""Hard eligibility rules. Each rule returns a reason string when a paper is ineligible."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from research_agent.llm.structured_output import PaperEvaluation
from research_agent.metadata.schemas import Paper
from research_agent.settings import RankingConfig
from research_agent.storage.database import Database


@dataclass
class Candidate:
    paper: Paper
    evaluation_id: int
    evaluation: PaperEvaluation
    category: str                              # core | exploratory
    raw_score: float = 0.0
    dimensions: dict | None = None
    tags: frozenset[str] = frozenset()
    ineligible: str | None = None


def check(c: Candidate, cfg: RankingConfig, report_date: date) -> str | None:
    p = c.paper
    if p.is_retracted:
        return "retracted"
    if p.validation_status == "failed":
        return "failed identity/metadata validation"
    if not (p.title and p.canonical_url and (p.doi or p.pmid or p.preprint_id)):
        return "missing title/identifier/url"
    if p.first_publication_date is None:
        return "no verified publication date"
    if p.first_publication_date > report_date:
        return "published after the report date (look-ahead)"
    if p.first_publication_date < report_date - timedelta(days=cfg.publication_horizon_days):
        return f"older than the {cfg.publication_horizon_days}-day horizon"
    if p.metadata_completeness < cfg.min_metadata_completeness:
        return f"metadata completeness {p.metadata_completeness:.2f} < {cfg.min_metadata_completeness}"
    align = c.evaluation.dimensions.get("research_alignment")
    if align is None:
        return "no research_alignment assessment"
    if c.category == "none":
        return "not aligned with any profile topic"
    thr = cfg.exploratory_relevance_threshold if c.category == "exploratory" else cfg.core_relevance_threshold
    if align.score < thr:
        return f"research_alignment {align.score:.0f} < {c.category} threshold {thr:.0f}"
    return None


def previously_recommended(db: Database, c: Candidate, report_date: date, cfg: RankingConfig) -> str | None:
    """Default: never repeat a paper (or its preprint/journal counterpart) unless a resurfacing
    rule applies."""
    res = cfg.resurfacing
    rows = db.query("SELECT report_date, json_extract(reason, '$.preprint_version') AS v FROM recommendations"
                    " WHERE paper_id=? AND report_date < ? AND status='published' ORDER BY report_date DESC",
                    (c.paper.id, report_date.isoformat()))
    if rows:
        if res.get("allow_previously_recommended"):
            return None
        prior_version = rows[0]["v"]
        if res.get("allow_on_new_preprint_version") and c.paper.preprint_version and prior_version \
                and c.paper.preprint_version > prior_version:
            return None
        return f"already recommended on {rows[0]['report_date']}"
    # Counterpart (preprint <-> journal) previously recommended?
    counterpart = db.query(
        "SELECT r.report_date, rel.from_paper_id AS pre FROM paper_relations rel JOIN recommendations r ON"
        " r.paper_id IN (rel.from_paper_id, rel.to_paper_id) WHERE (rel.from_paper_id=? OR rel.to_paper_id=?)"
        " AND r.paper_id != ? AND r.report_date < ? AND r.status='published'",
        (c.paper.id, c.paper.id, c.paper.id, report_date.isoformat()))
    if counterpart:
        is_journal_version = counterpart[0]["pre"] != c.paper.id
        if is_journal_version and res.get("allow_on_preprint_published", True):
            return None
        return f"counterpart already recommended on {counterpart[0]['report_date']}"
    return None


def counterpart_ids(db: Database, paper_id: int) -> set[int]:
    rows = db.query("SELECT from_paper_id, to_paper_id FROM paper_relations WHERE from_paper_id=? OR to_paper_id=?",
                    (paper_id, paper_id))
    return {x for r in rows for x in (r[0], r[1])} - {paper_id}
