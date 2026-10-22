"""Scoring + selection for a report date.

1. Candidates: papers with a completed evaluation for the CURRENT metadata/rubric/prompt/model.
2. Exclude invalid records and unresolved duplicates (a preprint whose journal version is also
   a candidate is dropped in favour of the journal version).
3. Hard eligibility (horizon, alignment threshold, completeness, history).
4. Composite score (Python, deterministic) with the latest valid trend snapshot.
5. Order: raw score desc, publication date desc, canonical_id asc (stable).
6. Greedy bounded-diversity selection, exploratory cap, core majority, up to N papers.
Raw score, raw rank, adjusted score and final position are all stored.
Re-running for a date that already has a PUBLISHED selection returns it unchanged.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date

from research_agent.llm.cache import current_evaluation
from research_agent.ranking.diversity import penalty
from research_agent.ranking.eligibility import Candidate, check, counterpart_ids, previously_recommended
from research_agent.scoring.aggregator import Unscorable, combine, score_dimensions
from research_agent.scoring.registry import ScoreContext
from research_agent.settings import Settings
from research_agent.storage.database import Database, dumps
from research_agent.storage.repositories import load_paper
from research_agent.timeutil import iso_utc
from research_agent.trends.taxonomy import lexical_tags_for
from research_agent.trends.updater import Snapshot, latest_snapshot

log = logging.getLogger(__name__)


@dataclass
class Selection:
    report_date: date
    selected: list[dict]
    funnel: dict
    trend_snapshot_id: int | None
    scoring_version: str
    excluded: dict[str, int] = field(default_factory=dict)
    reused: bool = False


def _evaluated_papers(db: Database, settings: Settings) -> list[tuple[int, int]]:
    llm = settings.llm
    rows = db.query(
        "SELECT p.id AS pid, e.id AS eid FROM papers p JOIN evaluations e ON e.paper_id=p.id AND"
        " e.metadata_version=p.metadata_version WHERE e.model=? AND e.prompt_version=? AND e.rubric_version=?"
        " ORDER BY p.id", (llm.active_model, llm.prompt_version, settings.scoring.rubric_version()))
    return [(r["pid"], r["eid"]) for r in rows]


def build_candidates(db: Database, settings: Settings, report_date: date, snapshot: Snapshot | None,
                     *, exclude_paper_ids: set[int] = frozenset()) -> tuple[list[Candidate], dict[str, int]]:
    llm = settings.llm
    excluded: dict[str, int] = {}
    cands: list[Candidate] = []
    active_tags = [t for t in settings.trends.tags if t.status != "archived"]
    trend_scores = snapshot.scores if snapshot else {}
    for pid, _ in _evaluated_papers(db, settings):
        if pid in exclude_paper_ids:
            excluded["excluded_by_validation"] = excluded.get("excluded_by_validation", 0) + 1
            continue
        p = load_paper(db, pid)
        found = current_evaluation(db, pid, p.metadata_version, model=llm.active_model, prompt_version=llm.prompt_version,
                                   rubric_version=settings.scoring.rubric_version())
        if not found:
            continue
        eid, ev = found
        c = Candidate(p, eid, ev, ev.profile_category)
        reason = check(c, settings.ranking, report_date) or previously_recommended(db, c, report_date, settings.ranking)
        if reason:
            key = reason.split(" ")[0] if not reason.startswith("research_alignment") else "below_alignment_threshold"
            excluded[key] = excluded.get(key, 0) + 1
            continue
        lex = lexical_tags_for(f"{p.title} {p.abstract or ''} {' '.join(p.author_keywords)}", active_tags)
        ctx = ScoreContext(paper=p, report_date=report_date, evaluation=ev, params={}, trend_scores=trend_scores,
                           lexical_tags=lex, trend_snapshot_id=snapshot.id if snapshot else None)
        try:
            dims = score_dimensions(settings.scoring, ctx)
            comp = combine(settings.scoring, dims)
        except Unscorable as exc:
            excluded["unscorable"] = excluded.get("unscorable", 0) + 1
            log.warning("unscorable", extra={"paper": p.canonical_id, "error": str(exc)})
            continue
        c.raw_score = comp.overall
        c.dimensions = comp.dimensions
        c.tags = frozenset({t.tag_id for t in ev.topic_tags if t.confidence >= 0.5} | set(lex))
        cands.append(c)
    # Unresolved duplicates: prefer the journal version over its preprint.
    ids = {c.paper.id for c in cands}
    keep = []
    for c in cands:
        if c.paper.is_preprint and (counterpart_ids(db, c.paper.id) & ids):
            excluded["preprint_superseded_by_journal_version"] = excluded.get("preprint_superseded_by_journal_version", 0) + 1
            continue
        keep.append(c)
    keep.sort(key=lambda c: (-c.raw_score, -(c.paper.first_publication_date.toordinal()), c.paper.canonical_id))
    return keep, excluded


def select(cands: list[Candidate], settings: Settings) -> list[tuple[Candidate, float, float, int]]:
    """Greedy selection -> [(candidate, adjusted_score, penalty, raw_rank)]."""
    cfg = settings.ranking
    lam = float(cfg.diversity.get("lambda", 0.1)) if cfg.diversity.get("enabled", True) else 0.0
    raw_rank = {id(c): i + 1 for i, c in enumerate(cands)}
    remaining = list(cands)
    chosen: list[tuple[Candidate, float, float, int]] = []
    n_expl = 0
    while remaining and len(chosen) < cfg.max_recommendations:
        sel_tags = [c.tags for c, *_ in chosen]
        best = None
        for c in remaining:
            if c.category == "exploratory" and n_expl >= cfg.max_exploratory:
                continue
            pen = penalty(c.tags, sel_tags, lam)
            key = (-(c.raw_score - pen), raw_rank[id(c)])
            if best is None or key < best[0]:
                best = (key, c, pen)
        if best is None:
            break
        _, c, pen = best
        remaining.remove(c)
        chosen.append((c, round(c.raw_score - pen, 6), round(pen, 6), raw_rank[id(c)]))
        n_expl += c.category == "exploratory"
    # Core majority: drop the weakest exploratory picks if they are not a minority.
    while chosen:
        core = sum(1 for c, *_ in chosen if c.category == "core")
        expl = len(chosen) - core
        if expl == 0 or core > expl:
            break
        weakest = min((x for x in chosen if x[0].category == "exploratory"), key=lambda x: (x[0].raw_score, -x[3]))
        chosen.remove(weakest)
    return chosen


def run_selection(db: Database, settings: Settings, report_date: date, *, run_id: str | None = None,
                  force: bool = False, exclude_paper_ids: set[int] = frozenset(), persist: bool = True) -> Selection:
    d = report_date.isoformat()
    published = db.query("SELECT * FROM recommendations WHERE report_date=? AND status='published' ORDER BY position", (d,))
    if published and not force:
        return Selection(report_date, [dict(r) for r in published], {}, published[0]["trend_snapshot_id"],
                         published[0]["scoring_version"], reused=True)
    snapshot = latest_snapshot(db, on_or_before=report_date)
    cands, excluded = build_candidates(db, settings, report_date, snapshot, exclude_paper_ids=exclude_paper_ids)
    chosen = select(cands, settings)
    # Diversity decides WHICH papers are selected; presentation order is the raw score order
    # (the greedy pick order is kept as selection_step for auditability).
    steps = {id(c): i + 1 for i, (c, *_) in enumerate(chosen)}
    chosen.sort(key=lambda x: (-x[0].raw_score, -(x[0].paper.first_publication_date.toordinal()),
                               x[0].paper.canonical_id))
    sv = settings.scoring.version()
    rows = []
    for pos, (c, adj, pen, rr) in enumerate(chosen, start=1):
        rows.append({
            "report_date": d, "position": pos, "paper_id": c.paper.id, "canonical_id": c.paper.canonical_id,
            "raw_score": c.raw_score, "adjusted_score": adj, "raw_rank": rr, "scoring_version": sv,
            "trend_snapshot_id": snapshot.id if snapshot else None, "evaluation_id": c.evaluation_id,
            "reason": {"category": c.category, "diversity_penalty": pen, "selection_step": steps[id(c)],
                       "tags": sorted(c.tags),
                       "metadata_version": c.paper.metadata_version, "preprint_version": c.paper.preprint_version,
                       "dimensions": c.dimensions},
        })
    if persist:
        with db.transaction():
            # A forced regeneration replaces the date's selection (the published file stays in Git history).
            db.execute("DELETE FROM recommendations WHERE report_date=?" + ("" if force else " AND status!='published'"), (d,))
            for c in cands:
                db.execute("INSERT INTO paper_scores(paper_id, report_date, evaluation_id, scoring_version, trend_snapshot_id,"
                           " dimension_scores, overall_score, computed_at) VALUES (?,?,?,?,?,?,?,?)"
                           " ON CONFLICT(paper_id, report_date, scoring_version) DO UPDATE SET"
                           " evaluation_id=excluded.evaluation_id, trend_snapshot_id=excluded.trend_snapshot_id,"
                           " dimension_scores=excluded.dimension_scores, overall_score=excluded.overall_score,"
                           " computed_at=excluded.computed_at",
                           (c.paper.id, d, c.evaluation_id, sv, snapshot.id if snapshot else None, dumps(c.dimensions),
                            c.raw_score, iso_utc()))
            for r in rows:
                db.execute("INSERT INTO recommendations(report_date, position, paper_id, canonical_id, raw_score, adjusted_score,"
                           " raw_rank, scoring_version, trend_snapshot_id, evaluation_id, reason, status, run_id, created_at)"
                           " VALUES (?,?,?,?,?,?,?,?,?,?,?,'selected',?,?)",
                           (d, r["position"], r["paper_id"], r["canonical_id"], r["raw_score"], r["adjusted_score"],
                            r["raw_rank"], sv, r["trend_snapshot_id"], r["evaluation_id"], dumps(r["reason"]), run_id,
                            iso_utc()))
    for r in rows:
        r["reason"] = json.dumps(r["reason"], sort_keys=True)
    funnel = {"evaluated_current": len(_evaluated_papers(db, settings)), "eligible_ranked": len(cands),
              "selected": len(rows)}
    return Selection(report_date, rows, funnel, snapshot.id if snapshot else None, sv, excluded)
