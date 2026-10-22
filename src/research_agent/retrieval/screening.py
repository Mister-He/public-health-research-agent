"""Two-stage screening, stage 1: cheap lexical + semantic prescreen of every new or changed
paper, then a bounded shortlist enqueued (deduplicated) for Claude assessment."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta

from research_agent.retrieval.embeddings import Embedder, TfidfEmbedder
from research_agent.retrieval.keyword_filter import lexical_screen
from research_agent.settings import Settings
from research_agent.scoring.journal import floor_reason, journal_context
from research_agent.storage import analysis_queue as q
from research_agent.storage.database import Database
from research_agent.storage.repositories import load_paper, save_paper
from research_agent.timeutil import iso_utc
from research_agent.trends.taxonomy import lexical_tags_for

log = logging.getLogger(__name__)

LEXICAL_WEIGHT = 0.7
SEMANTIC_WEIGHT = 0.3
SEMANTIC_SCALE = 0.35   # cosine at/above this counts as full semantic evidence
MIN_PRESCREEN = 0.25    # below this a paper is never shortlisted


@dataclass
class ScreenStats:
    screened: int = 0
    above_threshold: int = 0
    enqueued: int = 0
    already_evaluated_or_queued: int = 0
    excluded_by_journal: int = 0


def prescreen(db: Database, settings: Settings, paper_ids: list[int], embedder: Embedder | None = None) -> int:
    """Compute and store prescreen_score/prescreen_topics for the given papers."""
    if not paper_ids:
        return 0
    embedder = embedder or TfidfEmbedder()
    compacted = {r[0] for r in db.query("SELECT id FROM papers WHERE compacted=1")}
    papers = [load_paper(db, pid) for pid in paper_ids if pid not in compacted]
    if not papers:
        return 0
    tags = [t for t in settings.trends.tags if t.status != "archived"]
    docs = [f"{p.title or ''}. {p.abstract or ''} {' '.join(p.author_keywords)}" for p in papers]
    sims = embedder.similarities(docs, settings.profile)
    imp = {t.id: t.importance for t in settings.profile.topics}
    with db.transaction():
        for p, sim in zip(papers, sims):
            lex = lexical_screen(p, settings.profile)
            if lex.excluded_reason:
                score, topics = 0.0, []
            else:
                sem = max((min(1.0, v / SEMANTIC_SCALE) * imp[t] for t, v in sim.items()), default=0.0)
                score = round(LEXICAL_WEIGHT * lex.score + SEMANTIC_WEIGHT * sem, 4)
                # lexical topics first, then semantically closest as fallback evidence
                topics = sorted(lex.topic_scores, key=lambda k: (-lex.topic_scores[k], k))
                if not topics and sim:
                    best = max(sorted(sim), key=lambda k: sim[k])
                    if sim[best] >= SEMANTIC_SCALE * 0.6:
                        topics = [best]
            p.prescreen_score = score
            p.prescreen_topics = topics
            save_paper(db, p)
            # Trend tags are assigned now, while the abstract is available; brief-info papers
            # keep these assignments (see trends.updater).
            db.execute("DELETE FROM paper_tags WHERE paper_id=? AND method='lexical'", (p.id,))
            for tid in lexical_tags_for(f"{p.title} {p.abstract or ''} {' '.join(p.author_keywords)}", tags):
                db.execute("INSERT INTO paper_tags(paper_id, tag_id, method, confidence, assigned_at)"
                           " VALUES (?,?,'lexical',1.0,?)", (p.id, tid, iso_utc()))
    return len(papers)


def candidate_issns(db: Database, settings: Settings, report_date: date) -> set[str]:
    """ISSNs of journal articles that could be shortlisted (for journal-metric lookup)."""
    horizon = report_date - timedelta(days=settings.ranking.publication_horizon_days)
    out: set[str] = set()
    for r in db.query("SELECT json_extract(data, '$.journal_issn') AS i FROM papers WHERE compacted=0 AND is_preprint=0"
                      " AND prescreen_score >= ? AND first_publication_date BETWEEN ? AND ?",
                      (MIN_PRESCREEN, horizon.isoformat(), report_date.isoformat())):
        out.update(json.loads(r["i"] or "[]"))
    return out


def shortlist_and_enqueue(db: Database, settings: Settings, report_date: date) -> ScreenStats:
    """Shortlist recent, sufficiently relevant papers and enqueue Claude jobs (deduplicated)."""
    st = ScreenStats()
    horizon = report_date - timedelta(days=settings.ranking.publication_horizon_days)
    llm = settings.llm
    rubric = settings.scoring.rubric_version()
    cap = int(min(llm.screening["shortlist_max"], llm.stages["scoring"].max_papers_per_run or 10_000))
    expl_cap = int(cap * llm.screening.get("exploratory_share_max", 0.3))
    q.expire_out_of_horizon(db, horizon)
    rows = db.query(
        "SELECT id, canonical_id, metadata_version, prescreen_score, first_publication_date, data FROM papers"
        " WHERE is_retracted=0 AND compacted=0 AND prescreen_score >= ?"
        " AND first_publication_date IS NOT NULL AND first_publication_date BETWEEN ? AND ?"
        " ORDER BY prescreen_score DESC, first_publication_date DESC, canonical_id ASC",
        (MIN_PRESCREEN, horizon.isoformat(), report_date.isoformat()))
    st.above_threshold = len(rows)
    picked = 0
    expl = 0
    for r in rows:
        if picked >= cap:
            break
        # Journal floor / block list BEFORE Claude: low-impact journals never use analysis allowance.
        if floor_reason(journal_context(db, settings, load_paper(db, r["id"])), settings):
            st.excluded_by_journal += 1
            continue
        topics = json.loads(r["data"]).get("prescreen_topics") or []
        kind = settings.profile.topic(topics[0]).kind if topics else "core"
        if kind == "exploratory" and expl >= expl_cap:
            continue
        added = q.enqueue(db, paper_id=r["id"], canonical_id=r["canonical_id"], metadata_version=r["metadata_version"],
                          rubric_version=rubric, prompt_version=llm.prompt_version, model=llm.active_model,
                          priority=r["prescreen_score"], intended_report_date=report_date)
        if added:
            st.enqueued += 1
            picked += 1
            expl += kind == "exploratory"
        else:
            st.already_evaluated_or_queued += 1
            picked += 1   # an existing job/evaluation still occupies a shortlist slot
            expl += kind == "exploratory"
    st.screened = len(rows)
    return st
