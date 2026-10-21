"""Small data-access helpers for papers and their provenance."""

from __future__ import annotations

import json
from dataclasses import dataclass

from research_agent.metadata.schemas import Paper, SourceRecord
from research_agent.storage.database import Database, dumps
from research_agent.timeutil import iso_utc


def load_paper(db: Database, paper_id: int) -> Paper:
    row = db.one("SELECT id, data FROM papers WHERE id=?", (paper_id,))
    if row is None:
        raise KeyError(f"paper {paper_id} not found")
    p = Paper.model_validate_json(row["data"])
    p.id = row["id"]
    p.ai_topic_tags = [r[0] for r in db.query(
        "SELECT DISTINCT tag_id FROM paper_tags WHERE paper_id=? AND method='claude' ORDER BY tag_id", (paper_id,))]
    return p


def paper_id_for(db: Database, canonical_or_alias: str) -> int | None:
    pid = db.scalar("SELECT id FROM papers WHERE canonical_id=?", (canonical_or_alias,))
    if pid is None:
        pid = db.scalar("SELECT paper_id FROM paper_identifiers WHERE scheme='canonical_alias' AND value=?",
                        (canonical_or_alias,))
    return pid


def save_paper(db: Database, paper: Paper, *, run_id: str | None = None) -> int:
    data = paper.model_dump_json(exclude={"id", "ai_topic_tags"})
    params = (
        paper.canonical_id, data, paper.metadata_version,
        paper.first_publication_date.isoformat() if paper.first_publication_date else None,
        paper.indexed_date.isoformat() if paper.indexed_date else None,
        paper.discovered_at, iso_utc(), paper.prescreen_score, int(paper.is_preprint),
        int(paper.is_retracted), paper.metadata_completeness,
    )
    if paper.id is None:
        cur = db.execute(
            "INSERT INTO papers(canonical_id, data, metadata_version, first_publication_date, indexed_date,"
            " discovered_at, updated_at, prescreen_score, is_preprint, is_retracted, metadata_completeness,"
            " discovered_run_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            params + (run_id,),
        )
        paper.id = cur.lastrowid
    else:
        db.execute(
            "UPDATE papers SET canonical_id=?, data=?, metadata_version=?, first_publication_date=?, indexed_date=?,"
            " discovered_at=?, updated_at=?, prescreen_score=?, is_preprint=?, is_retracted=?, metadata_completeness=?"
            " WHERE id=?",
            params + (paper.id,),
        )
    return paper.id  # type: ignore[return-value]


def source_records_for(db: Database, paper_id: int) -> list[SourceRecord]:
    """Latest payload per (provider, source_record_id)."""
    rows = db.query(
        "SELECT provider, source_record_id, payload, last_retrieved_at FROM source_records WHERE paper_id=?"
        " ORDER BY provider, source_record_id, last_retrieved_at DESC, id DESC", (paper_id,))
    seen: set[tuple[str, str]] = set()
    out = []
    for r in rows:
        key = (r["provider"], r["source_record_id"])
        if key in seen:
            continue
        seen.add(key)
        out.append(SourceRecord.model_validate_json(r["payload"]))
    return out


def iter_paper_ids(db: Database, where: str = "1=1", params: tuple = ()) -> list[int]:
    return [r[0] for r in db.query(f"SELECT id FROM papers WHERE {where} ORDER BY id", params)]


@dataclass
class RecordedUsage:
    provider: str
    requests: int = 0
    failures: int = 0
    retries: int = 0
    rate_limited: int = 0


def record_api_usage(db: Database, run_id: str, usage: dict[str, RecordedUsage]) -> None:
    for u in usage.values():
        if u.requests or u.failures:
            db.execute(
                "INSERT INTO api_usage(run_id, provider, requests, failures, retries, rate_limited, recorded_at)"
                " VALUES (?,?,?,?,?,?,?)", (run_id, u.provider, u.requests, u.failures, u.retries, u.rate_limited, iso_utc()))


def log_tag_event(db: Database, tag_id: str, event: str, details: dict | None = None) -> None:
    db.execute("INSERT INTO tag_events(tag_id, event, details, at) VALUES (?,?,?,?)",
               (tag_id, event, dumps(details or {}), iso_utc()))


def json_list(value: str | None) -> list:
    return json.loads(value) if value else []
