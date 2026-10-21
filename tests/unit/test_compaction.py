"""Brief-info storage: only papers queued for Claude keep abstracts and full source records."""

import json

from fixtures.synthetic import REPORT_DATE, records

from research_agent.llm import claude_code_action as cca
from research_agent.metadata.deduplication import ingest_record
from research_agent.retrieval.screening import prescreen, shortlist_and_enqueue
from research_agent.storage.compaction import candidates_for_compaction, compact_papers
from research_agent.trends.updater import update_trends


def _run(db, settings, limit=3):
    ids = sorted({ingest_record(db, r).paper_id for r in records()})
    prescreen(db, settings, ids)
    shortlist_and_enqueue(db, settings, REPORT_DATE)
    cca.build_scoring_manifest(db, settings, run_id="r", report_date=REPORT_DATE, limit=limit)
    return compact_papers(db, candidates_for_compaction(db))


def test_only_queued_papers_keep_full_metadata(db, settings):
    n = _run(db, settings)
    queued = {r[0] for r in db.query("SELECT paper_id FROM analysis_jobs")}
    assert n == db.scalar("SELECT COUNT(*) FROM papers") - len(queued) > 0
    for r in db.query("SELECT id, compacted, data FROM papers"):
        data = json.loads(r["data"])
        if r["id"] in queued:
            assert r["compacted"] == 0
            assert data["abstract"] or "without abstract" in data["title"]   # one fixture has no abstract
        else:
            assert r["compacted"] == 1 and data.get("abstract") is None and len(data["authors"]) <= 3
            assert data["title"] and data["canonical_id"] and data.get("prescreen_score") is not None
    slim = db.one("SELECT payload FROM source_records WHERE paper_id NOT IN (SELECT paper_id FROM analysis_jobs)")
    pl = json.loads(slim["payload"])
    assert pl.get("abstract") is None
    assert pl["abstract_sha256"] and len(pl["abstract_sha256"]) == 64


def test_identical_refetch_rewrites_nothing(db, settings):
    _run(db, settings)
    before = [tuple(r) for r in db.query("SELECT id, data, metadata_version FROM papers ORDER BY id")]
    for r in records():
        ingest_record(db, r)
    after = [tuple(r) for r in db.query("SELECT id, data, metadata_version FROM papers ORDER BY id")]
    assert before == after
    assert db.scalar("SELECT COUNT(*) FROM metadata_revisions") == 0


def test_brief_info_papers_keep_trend_tags(db, settings):
    ids = sorted({ingest_record(db, r).paper_id for r in records()})
    prescreen(db, settings, ids)
    full = update_trends(db, settings, REPORT_DATE, kind="daily")
    compact_papers(db, candidates_for_compaction(db))     # nothing queued: everything becomes brief info
    brief = update_trends(db, settings, REPORT_DATE, kind="daily")
    for tid, t in full.tags.items():
        assert brief.tags[tid]["counts"]["recent"] == t["counts"]["recent"], tid


def test_new_content_restores_full_record(db, settings):
    _run(db, settings, limit=0)
    r = records()[0]
    new = r.model_copy(update={"source_record_id": "fx-new", "abstract": r.abstract + " Revised."})
    res = ingest_record(db, new)
    assert db.scalar("SELECT compacted FROM papers WHERE id=?", (res.paper_id,)) == 0
    assert json.loads(db.scalar("SELECT data FROM papers WHERE id=?", (res.paper_id,)))["abstract"].endswith("Revised.")


def test_compacted_papers_are_never_shortlisted(db, settings):
    _run(db, settings, limit=0)
    assert db.scalar("SELECT COUNT(*) FROM analysis_jobs WHERE status='in_progress'") == 0
    shortlist_and_enqueue(db, settings, REPORT_DATE)
    pending = db.query("SELECT p.compacted FROM analysis_jobs j JOIN papers p ON p.id=j.paper_id")
    assert all(r[0] == 0 for r in pending)


def test_retention_deletes_old_irrelevant_and_slims_old_relevant(db, settings):
    _run(db, settings)
    db.execute("UPDATE papers SET discovered_at='2026-08-01T00:00:00Z' WHERE compacted=1")
    irrelevant = db.scalar("SELECT COUNT(*) FROM papers WHERE compacted=1 AND prescreen_score < 0.15")
    relevant = db.scalar("SELECT COUNT(*) FROM papers WHERE compacted=1 AND prescreen_score >= 0.15")
    from research_agent.storage.compaction import apply_retention
    res = apply_retention(db, today_iso="2026-10-21", keep_days=45, relevance_floor=0.15)
    assert res["deleted_irrelevant"] == irrelevant > 0
    assert db.scalar("SELECT COUNT(*) FROM papers WHERE compacted=1") == relevant
    assert db.scalar("SELECT COUNT(*) FROM source_records WHERE paper_id IN (SELECT id FROM papers WHERE compacted=1)") == 0
    # Queued papers are untouched; identifiers of retained brief papers still deduplicate.
    assert db.scalar("SELECT COUNT(*) FROM source_records WHERE paper_id IN (SELECT paper_id FROM analysis_jobs)") > 0
    before = db.scalar("SELECT COUNT(*) FROM papers")
    for r in records():
        ingest_record(db, r)
    assert db.scalar("SELECT COUNT(*) FROM papers") == before + irrelevant   # only deleted ones come back


def test_partitioned_snapshot_roundtrip_and_determinism(db, settings, tmp_path, monkeypatch):
    from research_agent.storage import snapshot
    from research_agent.storage.database import open_db

    monkeypatch.setattr(snapshot, "ROWS_PER_BUCKET", 5)
    _run(db, settings)
    a = snapshot.export_dir(db, tmp_path / "a")
    b = snapshot.export_dir(db, tmp_path / "b")
    files_a = {p.relative_to(a): p.read_bytes() for p in a.rglob("*") if p.is_file()}
    files_b = {p.relative_to(b): p.read_bytes() for p in b.rglob("*") if p.is_file()}
    assert files_a == files_b                                         # byte-identical
    snapshot.import_dir(a, tmp_path / "restored.sqlite3")
    r = open_db(tmp_path / "restored.sqlite3")
    for t in ("papers", "source_records", "paper_identifiers", "analysis_jobs", "paper_tags", "retrieval_checkpoints"):
        assert r.query(f"SELECT rowid, * FROM {t} ORDER BY rowid") == db.query(f"SELECT rowid, * FROM {t} ORDER BY rowid"), t
    assert r.schema_version() == db.schema_version()
    # A new row changes only the last partition of the affected table.
    db.set_meta("new_key", "x")
    c = snapshot.export_dir(db, tmp_path / "c")
    changed = {p.relative_to(c) for p in c.rglob("*.gz") if files_a.get(p.relative_to(c)) != p.read_bytes()}
    assert changed and all(str(x).startswith("tables/state_meta/") for x in changed)
