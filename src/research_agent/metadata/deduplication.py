"""Persistent identifier-based deduplication.

Rules (conservative):
1. Any shared identifier (DOI, PMID, PMCID, arXiv id, OpenAlex id) => same paper.
2. Identifiers of the same scheme that disagree => different papers, never merged.
   A preprint DOI and a journal DOI are therefore always two papers, linked by a
   `published_as` relation instead.
3. Title fallback only when NO identifier matched: identical normalized title (>= 20 chars)
   + identical first-author surname + publication years within 1 + no conflicting identifiers.
4. A record that bridges two existing papers (e.g. DOI -> A, PMID -> B) merges them if they
   have no conflicting identifiers; otherwise it is logged as an identity conflict and
   attached to the strongest match only.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from research_agent.metadata.normalization import normalize_title, surname
from research_agent.metadata.provenance import build_paper, canonical_id_for
from research_agent.metadata.schemas import CRITICAL_FIELDS, Paper, SourceRecord
from research_agent.storage.database import Database, dumps
from research_agent.storage.repositories import load_paper, save_paper, source_records_for
from research_agent.timeutil import iso_utc

SCHEME_STRENGTH = ["doi", "pmid", "pmcid", "arxiv", "openalex"]
_FK_TABLES = [  # (table, column, unique-conflict-prone)
    ("paper_identifiers", "paper_id"),
    ("paper_title_keys", "paper_id"),
    ("paper_versions", "paper_id"),
    ("source_records", "paper_id"),
    ("paper_discoveries", "paper_id"),
    ("metadata_revisions", "paper_id"),
    ("analysis_jobs", "paper_id"),
    ("evaluations", "paper_id"),
    ("paper_scores", "paper_id"),
    ("paper_tags", "paper_id"),
    ("recommendations", "paper_id"),
]


@dataclass
class IngestResult:
    paper_id: int
    canonical_id: str
    created: bool
    metadata_changed: bool
    conflict: bool = False


def _title_key(rec: SourceRecord) -> tuple[str, str, int | None] | None:
    t = normalize_title(rec.title)
    if len(t) < 20 or not rec.authors:
        return None
    d = rec.online_date or rec.print_date or rec.preprint_posted_date
    return t, surname(rec.authors[0]), d.year if d else None


def _ids_of(db: Database, paper_id: int) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for r in db.query("SELECT scheme, value FROM paper_identifiers WHERE paper_id=? AND scheme!='canonical_alias'",
                      (paper_id,)):
        out.setdefault(r["scheme"], set()).add(r["value"])
    return out


def _conflicts(a: dict[str, set[str]], b: dict[str, set[str]]) -> bool:
    for scheme in ("doi", "pmid", "pmcid", "arxiv"):
        if a.get(scheme) and b.get(scheme) and not (a[scheme] & b[scheme]):
            return True
    return False


def _rec_ids(rec: SourceRecord) -> dict[str, set[str]]:
    return {s: {v} for s, v in rec.identity_keys()}


def find_matches(db: Database, rec: SourceRecord) -> dict[int, list[str]]:
    """paper_id -> schemes that matched."""
    matches: dict[int, list[str]] = {}
    for scheme, value in rec.identity_keys():
        pid = db.scalar("SELECT paper_id FROM paper_identifiers WHERE scheme=? AND value=?", (scheme, value))
        if pid is not None:
            matches.setdefault(pid, []).append(scheme)
    return matches


def _title_fallback(db: Database, rec: SourceRecord) -> int | None:
    key = _title_key(rec)
    if key is None:
        return None
    title, author, year = key
    rows = db.query("SELECT paper_id, year FROM paper_title_keys WHERE title_key=? AND first_author=?", (title, author))
    rec_ids = _rec_ids(rec)
    candidates = []
    for r in rows:
        if year is not None and r["year"] is not None and abs(r["year"] - year) > 1:
            continue
        if _conflicts(_ids_of(db, r["paper_id"]), rec_ids):
            continue
        # Never fold a preprint into a journal article (or vice versa) by title alone.
        is_pp = db.scalar("SELECT is_preprint FROM papers WHERE id=?", (r["paper_id"],))
        if bool(is_pp) != rec.is_preprint:
            continue
        candidates.append(r["paper_id"])
    return candidates[0] if len(set(candidates)) == 1 else None


def merge_papers(db: Database, keep: int, drop: int, *, reason: str) -> None:
    """Fold paper `drop` into `keep`, re-pointing all references; keep an alias."""
    drop_cid = db.scalar("SELECT canonical_id FROM papers WHERE id=?", (drop,))
    now = iso_utc()
    for table, col in _FK_TABLES:
        db.execute(f"UPDATE OR IGNORE {table} SET {col}=? WHERE {col}=?", (keep, drop))
        db.execute(f"DELETE FROM {table} WHERE {col}=?", (drop,))
    db.execute("UPDATE OR IGNORE paper_relations SET from_paper_id=? WHERE from_paper_id=?", (keep, drop))
    db.execute("UPDATE OR IGNORE paper_relations SET to_paper_id=? WHERE to_paper_id=?", (keep, drop))
    db.execute("DELETE FROM paper_relations WHERE from_paper_id=? OR to_paper_id=? OR from_paper_id=to_paper_id",
               (drop, drop))
    db.execute("DELETE FROM papers WHERE id=?", (drop,))
    db.execute("INSERT OR IGNORE INTO paper_identifiers(scheme, value, paper_id, source, first_seen_at)"
               " VALUES ('canonical_alias', ?, ?, ?, ?)", (drop_cid, keep, f"merge:{reason}", now))


def ingest_record(db: Database, rec: SourceRecord, *, run_id: str | None = None) -> IngestResult:
    """Persist one source record and update the canonical paper. Idempotent."""
    if not rec.identity_keys():
        raise ValueError(f"{rec.provider}:{rec.source_record_id} has no identifiers; refusing to store")
    now = iso_utc()
    with db.transaction():
        same = db.one("SELECT id, paper_id FROM source_records WHERE provider=? AND source_record_id=? AND content_hash=?",
                      (rec.provider, rec.source_record_id, rec.content_hash()))
        if same is not None:
            # Identical re-fetch (overlap window): record the sighting (paper_discoveries.last_seen_at)
            # and touch nothing else, so stored snapshot partitions stay byte-identical.
            pid = same["paper_id"]
            _record_discovery(db, pid, rec)
            return IngestResult(pid, db.scalar("SELECT canonical_id FROM papers WHERE id=?", (pid,)), False, False)
        matches = find_matches(db, rec)
        conflict = False
        created = False
        if not matches:
            pid = _title_fallback(db, rec)
        else:
            ordered = sorted(matches, key=lambda p: min(SCHEME_STRENGTH.index(s) for s in matches[p]))
            pid = ordered[0]
            for other in ordered[1:]:
                if _conflicts(_ids_of(db, pid), _ids_of(db, other)) or _conflicts(_ids_of(db, other), _rec_ids(rec)):
                    conflict = True
                    db.execute(
                        "INSERT OR IGNORE INTO identity_conflicts(provider, source_record_id, candidate_paper_ids,"
                        " resolution, detected_at) VALUES (?,?,?,?,?)",
                        (rec.provider, rec.source_record_id, json.dumps(sorted(matches)), f"attached_to:{pid}", now))
                else:
                    merge_papers(db, pid, other, reason=f"{rec.provider}:{rec.source_record_id}")

        if pid is not None:
            rec = _sanitize_conflicts(db, rec, pid, now)
        if pid is None:
            ids = dict(rec.identity_keys())
            cid = canonical_id_for(ids.get("doi"), ids.get("pmid"), ids.get("arxiv"), ids.get("pmcid"), ids.get("openalex"))
            stub = Paper(canonical_id=cid, discovered_at=now, metadata_version="new")
            pid = save_paper(db, stub, run_id=run_id)
            created = True

        payload = rec.model_dump_json()
        db.execute(
            "INSERT INTO source_records(paper_id, provider, source_record_id, content_hash, first_query_id,"
            " first_retrieved_at, last_retrieved_at, payload) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(provider, source_record_id, content_hash) DO UPDATE SET last_retrieved_at=excluded.last_retrieved_at",
            (pid, rec.provider, rec.source_record_id, rec.content_hash(), rec.query_id, rec.retrieved_at,
             rec.retrieved_at, payload))
        for scheme, value in rec.identity_keys():
            owner = db.scalar("SELECT paper_id FROM paper_identifiers WHERE scheme=? AND value=?", (scheme, value))
            if owner is None:
                db.execute("INSERT INTO paper_identifiers(scheme, value, paper_id, source, first_seen_at) VALUES (?,?,?,?,?)",
                           (scheme, value, pid, rec.provider, now))
            elif owner != pid:
                conflict = True
        _record_discovery(db, pid, rec)
        if rec.abstract:
            # Genuinely new content (e.g. a new preprint version): full data again until re-screened.
            db.execute("UPDATE papers SET compacted=0 WHERE id=?", (pid,))
        tk = _title_key(rec)
        if tk:
            db.execute("INSERT OR IGNORE INTO paper_title_keys(title_key, first_author, year, paper_id) VALUES (?,?,?,?)",
                       (tk[0], tk[1], tk[2], pid))
        if rec.preprint_version:
            db.execute(
                "INSERT OR IGNORE INTO paper_versions(paper_id, version, posted_date, abstract_hash, title_hash,"
                " source_record_id, first_seen_at) VALUES (?,?,?,?,?,?,?)",
                (pid, rec.preprint_version, str(rec.preprint_revision_date or rec.preprint_posted_date or ""),
                 _h(rec.abstract), _h(rec.title), rec.source_record_id, now))

        changed = rebuild(db, pid, run_id=run_id, source=rec.provider)
        link_publications(db, pid)
        cid = db.scalar("SELECT canonical_id FROM papers WHERE id=?", (pid,))
    return IngestResult(pid, cid, created, changed, conflict)


_ID_ATTR = {"doi": "doi", "pmid": "pmid", "pmcid": "pmcid", "arxiv": "arxiv_id", "openalex": "openalex_id"}


def _sanitize_conflicts(db: Database, rec: SourceRecord, pid: int, now: str) -> SourceRecord:
    """A record matched through a strong identifier may still carry a DIFFERENT identifier of a
    scheme the paper already has (e.g. an OpenAlex work listing another PMID). The stronger match
    wins; the conflicting value is dropped from this record and logged — never merged."""
    have = _ids_of(db, pid)
    drop = {}
    for scheme, value in rec.identity_keys():
        if scheme in ("doi", "pmid", "pmcid", "arxiv") and have.get(scheme) and value not in have[scheme]:
            drop[_ID_ATTR[scheme]] = None
            db.execute("INSERT OR IGNORE INTO identity_conflicts(provider, source_record_id, candidate_paper_ids,"
                       " resolution, detected_at) VALUES (?,?,?,?,?)",
                       (rec.provider, rec.source_record_id, json.dumps([pid]),
                        f"dropped conflicting {scheme} {value} (paper has {sorted(have[scheme])})", now))
    return rec.model_copy(update=drop) if drop else rec


def _record_discovery(db: Database, pid: int, rec: SourceRecord) -> None:
    db.execute(
        "INSERT INTO paper_discoveries(paper_id, provider, query_id, strategy, first_seen_at, last_seen_at)"
        " VALUES (?,?,?,?,?,?) ON CONFLICT(paper_id, provider, query_id) DO UPDATE SET"
        " last_seen_at=excluded.last_seen_at, times_seen=times_seen+1",
        (pid, rec.provider, rec.query_id, rec.strategy, rec.retrieved_at, rec.retrieved_at))


def rebuild(db: Database, paper_id: int, *, run_id: str | None = None, source: str = "rebuild") -> bool:
    """Recompute the canonical paper from its source records; log critical-field revisions.
    Returns True if the metadata version changed."""
    old = load_paper(db, paper_id)
    records = source_records_for(db, paper_id)
    cid = old.canonical_id
    doi = next((r.doi for r in records if r.doi), None)
    if doi and not cid.startswith("doi:"):
        # Upgrade to DOI-based canonical id; keep the old one as an alias.
        db.execute("INSERT OR IGNORE INTO paper_identifiers(scheme, value, paper_id, source, first_seen_at)"
                   " VALUES ('canonical_alias', ?, ?, 'canonical_upgrade', ?)", (cid, paper_id, iso_utc()))
        cid = f"doi:{doi}"
    new = build_paper(records, canonical_id=cid, discovered_at=old.discovered_at or iso_utc(),
                      paper_id=paper_id, last_verified_at=old.last_verified_at)
    new.prescreen_score = old.prescreen_score
    new.prescreen_topics = old.prescreen_topics
    new.validation_status = old.validation_status if old.metadata_version == new.metadata_version else "unverified"
    compacted = db.scalar("SELECT compacted FROM papers WHERE id=?", (paper_id,))
    if old.metadata_version not in ("new", new.metadata_version) and not compacted:
        for f in CRITICAL_FIELDS:
            a, b = getattr(old, f), getattr(new, f)
            if a != b:
                db.execute("INSERT INTO metadata_revisions(paper_id, field, old_value, new_value, source, changed_at, run_id)"
                           " VALUES (?,?,?,?,?,?,?)", (paper_id, f, dumps(a), dumps(b), source, iso_utc(), run_id))
    if cid != old.canonical_id:
        db.execute("UPDATE analysis_jobs SET canonical_id=? WHERE paper_id=?", (cid, paper_id))
    if new.model_dump(exclude={"ai_topic_tags"}) != old.model_dump(exclude={"ai_topic_tags"}):
        save_paper(db, new)
    return old.metadata_version != new.metadata_version


def link_publications(db: Database, paper_id: int) -> None:
    """Create preprint -> journal `published_as` relations in both directions of discovery."""
    now = iso_utc()
    for r in source_records_for(db, paper_id):
        if r.published_doi:
            target = db.scalar("SELECT paper_id FROM paper_identifiers WHERE scheme='doi' AND value=?", (r.published_doi,))
            if target and target != paper_id:
                db.execute("INSERT OR IGNORE INTO paper_relations(from_paper_id, to_paper_id, relation, source, created_at)"
                           " VALUES (?,?,'published_as',?,?)", (paper_id, target, r.provider, now))
    doi = db.scalar("SELECT value FROM paper_identifiers WHERE paper_id=? AND scheme='doi'", (paper_id,))
    if doi:
        # Preprints that announced this DOI before the journal record arrived.
        for row in db.query("SELECT DISTINCT paper_id FROM source_records WHERE paper_id != ? AND"
                            " json_extract(payload, '$.published_doi') = ?", (paper_id, doi)):
            db.execute("INSERT OR IGNORE INTO paper_relations(from_paper_id, to_paper_id, relation, source, created_at)"
                       " VALUES (?,?,'published_as','preprint_record',?)", (row[0], paper_id, now))


def _h(text: str | None) -> str | None:
    return hashlib.sha256(normalize_title(text).encode()).hexdigest()[:12] if text else None
