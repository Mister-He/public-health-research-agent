"""Evaluation cache keyed by (paper, metadata_version, model, prompt_version, rubric_version)."""

from __future__ import annotations

from research_agent.llm.structured_output import PaperEvaluation
from research_agent.storage.database import Database
from research_agent.timeutil import iso_utc


def store_evaluation(db: Database, *, paper_id: int, ev: PaperEvaluation, model: str, prompt_version: str,
                     rubric_version: str, run_id: str | None, source_file: str | None) -> tuple[int, bool]:
    """Insert (or reuse) an evaluation; returns (evaluation_id, cache_hit)."""
    existing = db.scalar(
        "SELECT id FROM evaluations WHERE paper_id=? AND metadata_version=? AND model=? AND prompt_version=? AND rubric_version=?",
        (paper_id, ev.metadata_version, model, prompt_version, rubric_version))
    if existing:
        return existing, True
    cur = db.execute(
        "INSERT INTO evaluations(paper_id, canonical_id, metadata_version, model, prompt_version, rubric_version, payload,"
        " source_file, run_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (paper_id, ev.canonical_id, ev.metadata_version, model, prompt_version, rubric_version, ev.model_dump_json(),
         source_file, run_id, iso_utc()))
    eid = cur.lastrowid
    for t in ev.topic_tags:
        db.execute("INSERT INTO paper_tags(paper_id, tag_id, method, confidence, evaluation_id, assigned_at) VALUES (?,?,?,?,?,?)"
                   " ON CONFLICT(paper_id, tag_id, method) DO UPDATE SET confidence=excluded.confidence,"
                   " evaluation_id=excluded.evaluation_id, assigned_at=excluded.assigned_at",
                   (paper_id, t.tag_id, "claude", t.confidence, eid, iso_utc()))
    return eid, False


def current_evaluation(db: Database, paper_id: int, metadata_version: str, *, model: str, prompt_version: str,
                       rubric_version: str) -> tuple[int, PaperEvaluation] | None:
    row = db.one("SELECT id, payload FROM evaluations WHERE paper_id=? AND metadata_version=? AND model=?"
                 " AND prompt_version=? AND rubric_version=?",
                 (paper_id, metadata_version, model, prompt_version, rubric_version))
    if row is None:
        return None
    return row["id"], PaperEvaluation.model_validate_json(row["payload"])
