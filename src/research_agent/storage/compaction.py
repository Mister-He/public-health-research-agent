"""Brief-info storage policy.

Every retrieved paper is screened (and trend-tagged) while its full metadata is in memory.
Afterwards, papers that are NOT going to Claude keep only brief information: identifiers,
title, first authors, venue, dates, keywords/MeSH, publication type, relevance score and topics.
Abstracts and full source responses are kept only for papers with an analysis job, evaluation or
recommendation — i.e. the papers Claude assesses and the report may show. Their provenance
stays complete, so the validator can still verify every rendered field.

Brief-info source records keep provider, record id, dates, identifiers and title, plus a
SHA-256 fingerprint of the dropped abstract.
"""

from __future__ import annotations

import hashlib
import json

from research_agent.storage.database import Database

BRIEF_AUTHORS = 3
_PAPER_DROP = {"abstract": None, "abstract_source": None, "field_provenance": {}, "source_urls": [],
               "retrieval_queries": [], "external_topics": [], "open_access_url": None}
_PAYLOAD_KEEP = {"provider", "source_record_id", "query_id", "strategy", "retrieved_at", "doi", "pmid", "pmcid",
                 "openalex_id", "arxiv_id", "preprint_server", "preprint_version", "published_doi", "title", "authors",
                 "journal", "journal_issns", "publication_type", "is_preprint", "is_retracted", "online_date",
                 "print_date", "preprint_posted_date", "preprint_revision_date", "indexed_date", "modified_date",
                 "date_precision", "author_keywords", "mesh_terms", "source_url", "license"}


def candidates_for_compaction(db: Database) -> list[int]:
    return [r[0] for r in db.query(
        "SELECT id FROM papers WHERE compacted=0 AND prescreen_score IS NOT NULL"
        " AND id NOT IN (SELECT paper_id FROM analysis_jobs)"
        " AND id NOT IN (SELECT paper_id FROM evaluations)"
        " AND id NOT IN (SELECT paper_id FROM recommendations) ORDER BY id")]


def _sha(text: str | None) -> str | None:
    return hashlib.sha256(text.encode()).hexdigest() if text else None


def compact_papers(db: Database, paper_ids: list[int]) -> int:
    with db.transaction():
        for pid in paper_ids:
            data = json.loads(db.scalar("SELECT data FROM papers WHERE id=?", (pid,)))
            data.update(_PAPER_DROP)
            data["authors"] = (data.get("authors") or [])[:BRIEF_AUTHORS]
            db.execute("UPDATE papers SET data=?, compacted=1 WHERE id=?", (_dense(data), pid))
            for sr in db.query("SELECT id, payload FROM source_records WHERE paper_id=?", (pid,)):
                pl = json.loads(sr["payload"])
                slim = {k: v for k, v in pl.items() if k in _PAYLOAD_KEEP}
                slim["authors"] = (slim.get("authors") or [])[:BRIEF_AUTHORS]
                slim["abstract_sha256"] = pl.get("abstract_sha256") or _sha(pl.get("abstract"))
                db.execute("UPDATE source_records SET payload=? WHERE id=?", (_dense(slim), sr["id"]))
    return len(paper_ids)


def _dense(d: dict) -> str:
    """JSON without empty fields (model defaults restore them on load)."""
    return json.dumps({k: v for k, v in sorted(d.items()) if v not in (None, [], {}, "")}, ensure_ascii=False)


def apply_retention(db: Database, *, today_iso: str, keep_days: int = 45, relevance_floor: float = 0.15) -> dict:
    """Brief-info papers older than `keep_days` (past the overlap/reconciliation windows):
    - below the trend-corpus relevance floor: deleted entirely (they only served deduplication);
    - otherwise: per-source records dropped (identifiers, discovery provenance and the brief
      record remain for trend statistics and future deduplication)."""
    cutoff = f"{today_iso}T00:00:00Z"
    with db.transaction():
        old = ("SELECT id FROM papers WHERE compacted=1 AND julianday(?) - julianday(discovered_at) > ?"
               " AND id NOT IN (SELECT paper_id FROM analysis_jobs) AND id NOT IN (SELECT paper_id FROM evaluations)"
               " AND id NOT IN (SELECT paper_id FROM recommendations)")
        irrelevant = [r[0] for r in db.query(old + " AND COALESCE(prescreen_score, 0) < ?",
                                             (cutoff, keep_days, relevance_floor))]
        for pid in irrelevant:
            for t in ("paper_identifiers", "paper_title_keys", "paper_versions", "source_records", "paper_discoveries",
                      "metadata_revisions", "paper_tags"):
                db.execute(f"DELETE FROM {t} WHERE paper_id=?", (pid,))
            db.execute("DELETE FROM paper_relations WHERE from_paper_id=? OR to_paper_id=?", (pid, pid))
            db.execute("DELETE FROM papers WHERE id=?", (pid,))
        aged = [r[0] for r in db.query(old, (cutoff, keep_days))]
        dropped = 0
        for pid in aged:
            dropped += db.execute("DELETE FROM source_records WHERE paper_id=?", (pid,)).rowcount
            db.execute("DELETE FROM metadata_revisions WHERE paper_id=?", (pid,))
    return {"deleted_irrelevant": len(irrelevant), "source_records_dropped": dropped}
