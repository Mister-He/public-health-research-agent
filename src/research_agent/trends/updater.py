"""Trend snapshots.

daily     — incremental statistics every run (latest external signals reused if ≤ 14 days old)
weekly    — full reassessment on the configured weekday: refresh external OpenAlex signals,
            cluster unmatched papers, prepare Claude tag proposals
landscape — the one-off 180-day initial baseline

Snapshots are unique per (as_of_date, kind, version) and updated in place on re-runs, so a
repeated run never creates a second, incompatible snapshot for the same logical run.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta

from research_agent.metadata.normalization import normalize_text_for_match
from research_agent.settings import Settings
from research_agent.storage.database import Database, dumps
from research_agent.timeutil import iso_utc
from research_agent.trends.statistics import TagCounts, compute_trend, external_signal
from research_agent.trends.taxonomy import sync_tags, tag_matches

log = logging.getLogger(__name__)
ALGORITHM = "trend-v1"


@dataclass
class Snapshot:
    id: int
    as_of: date
    kind: str
    version: str
    corpus_size: int
    tags: dict[str, dict]

    @property
    def scores(self) -> dict[str, float]:
        return {k: v["score"] for k, v in self.tags.items()}


def snapshot_version(settings: Settings) -> str:
    p = settings.trends.parameters
    payload = {"params": p.model_dump(), "tags": [(t.id, t.match, t.aliases, t.name) for t in settings.trends.tags],
               "taxonomy_version": settings.trends.taxonomy_version}
    return f"{ALGORITHM}-{hashlib.sha256(dumps(payload).encode()).hexdigest()[:10]}"


def corpus(db: Database, settings: Settings, as_of: date, *, strict_as_of: bool) -> list[dict]:
    p = settings.trends.parameters
    start = as_of - timedelta(days=p.recent_window_days + p.baseline_window_days)
    cond = ["prescreen_score >= ?", "COALESCE(first_publication_date, indexed_date) > ?",
            "COALESCE(first_publication_date, indexed_date) <= ?", "is_retracted = 0"]
    params: list = [p.corpus_min_prescreen, start.isoformat(), as_of.isoformat()]
    if strict_as_of:  # historical replay: nothing discovered after the simulated date
        cond.append("discovered_at <= ?")
        params.append(f"{as_of.isoformat()}T23:59:59Z")
    rows = db.query(f"SELECT id, canonical_id, compacted, COALESCE(first_publication_date, indexed_date) AS d, data FROM papers"
                    f" WHERE {' AND '.join(cond)} ORDER BY id", params)
    out = []
    for r in rows:
        data = json.loads(r["data"])
        text = " ".join([data.get("title") or "", data.get("abstract") or "", " ".join(data.get("author_keywords") or [])])
        out.append({"id": r["id"], "canonical_id": r["canonical_id"], "date": date.fromisoformat(r["d"]),
                    "compacted": bool(r["compacted"]),
                    "text": normalize_text_for_match(text), "title": data.get("title"),
                    "keywords": data.get("author_keywords") or [], "sources": data.get("retrieval_sources") or []})
    return out


def latest_snapshot(db: Database, on_or_before: date | None = None, *, before: date | None = None) -> Snapshot | None:
    cond, params = ["status='valid'"], []
    if on_or_before:
        cond.append("as_of_date <= ?")
        params.append(on_or_before.isoformat())
    if before:
        cond.append("as_of_date < ?")
        params.append(before.isoformat())
    row = db.one(f"SELECT * FROM trend_snapshots WHERE {' AND '.join(cond)}"
                 " ORDER BY as_of_date DESC, CASE kind WHEN 'daily' THEN 0 WHEN 'weekly' THEN 1 ELSE 2 END, id DESC LIMIT 1",
                 params)
    if row is None:
        return None
    payload = json.loads(row["payload"])
    return Snapshot(row["id"], date.fromisoformat(row["as_of_date"]), row["kind"], row["version"], row["corpus_size"],
                    payload["tags"])


def refresh_external(db: Database, settings: Settings, as_of: date, openalex) -> dict[str, float | None]:
    """OpenAlex field-wide counts per tag (recent vs prior window), normalized by all works."""
    p = settings.trends.parameters
    win = int(p.external.get("window_days", 90))
    r0, r1 = as_of - timedelta(days=win - 1), as_of
    p0, p1 = r0 - timedelta(days=win), r0 - timedelta(days=1)
    out: dict[str, float | None] = {}
    try:
        tot_r = openalex.count(f"from_publication_date:{r0},to_publication_date:{r1}")
        tot_p = openalex.count(f"from_publication_date:{p0},to_publication_date:{p1}")
    except Exception as exc:  # unavailable => renormalize, never invent
        log.warning("external signals unavailable", extra={"error": str(exc)[:200]})
        tot_r = tot_p = 0
    for t in settings.trends.tags:
        if t.status == "archived":
            continue
        query = " OR ".join(f'"{" ".join(conj)}"' if len(conj) == 1 else "(" + " AND ".join(f'"{x}"' for x in conj) + ")"
                            for conj in (t.match or [[t.name]]))
        status, sig, rc, pc = "unavailable", None, None, None
        if tot_r and tot_p:
            try:
                rc = openalex.count(f"title_and_abstract.search:{query},from_publication_date:{r0},to_publication_date:{r1}")
                pc = openalex.count(f"title_and_abstract.search:{query},from_publication_date:{p0},to_publication_date:{p1}")
                sig = external_signal(rc, tot_r, pc, tot_p, pseudo_count=p.smoothing.get("pseudo_count", 1.0),
                                      growth_scale=p.smoothing.get("growth_logistic_scale", 0.7), min_sample=p.min_sample)
                status = "ok" if sig is not None else "unavailable"
            except Exception as exc:
                log.warning("external signal failed", extra={"tag": t.id, "error": str(exc)[:200]})
        db.execute("INSERT INTO external_signals(tag_id, as_of_date, provider, query, recent_count, prior_count, signal,"
                   " status, retrieved_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(tag_id, as_of_date, provider) DO UPDATE SET"
                   " query=excluded.query, recent_count=excluded.recent_count, prior_count=excluded.prior_count,"
                   " signal=excluded.signal, status=excluded.status, retrieved_at=excluded.retrieved_at",
                   (t.id, as_of.isoformat(), "openalex", query, rc, pc, sig, status, iso_utc()))
        out[t.id] = sig
    return out


def stored_external(db: Database, as_of: date, max_age_days: int = 14) -> dict[str, float | None]:
    rows = db.query("SELECT tag_id, signal, as_of_date FROM external_signals WHERE status='ok' AND as_of_date <= ?"
                    " AND as_of_date >= ? ORDER BY as_of_date", (as_of.isoformat(),
                                                                 (as_of - timedelta(days=max_age_days)).isoformat()))
    return {r["tag_id"]: r["signal"] for r in rows}


def update_trends(db: Database, settings: Settings, as_of: date, *, kind: str = "daily", run_id: str | None = None,
                  openalex=None, strict_as_of: bool = False) -> Snapshot:
    p = settings.trends.parameters
    sm = p.smoothing
    version = snapshot_version(settings)
    with db.transaction():
        sync_tags(db, settings)
        docs = corpus(db, settings, as_of, strict_as_of=strict_as_of)
        tags = [t for t in settings.trends.tags if t.status != "archived"]
        recent_start = as_of - timedelta(days=p.recent_window_days)
        tagged: dict[str, list[dict]] = {t.id: [] for t in tags}
        stored: dict[int, set[str]] = {}
        for r in db.query("SELECT paper_id, tag_id FROM paper_tags WHERE method='lexical'"):
            stored.setdefault(r[0], set()).add(r[1])
        full = [d for d in docs if not d["compacted"]]
        for d in docs:
            for t in tags:
                # Brief-info papers no longer have their abstract: use the tags assigned at
                # screening time (a match-rule change only re-tags them on title/keywords).
                hit = tag_matches(t, d["text"]) or (d["compacted"] and t.id in stored.get(d["id"], set()))
                if hit:
                    tagged[t.id].append(d)
        # Re-derive lexical assignments for full papers (match rules may have changed).
        corpus_ids = [d["id"] for d in full]
        for i in range(0, len(corpus_ids), 500):
            chunk = corpus_ids[i:i + 500]
            db.execute(f"DELETE FROM paper_tags WHERE method='lexical' AND paper_id IN ({','.join('?' * len(chunk))})", chunk)
        now = iso_utc()
        for tid, ds in tagged.items():
            for d in ds:
                if d["compacted"]:
                    continue
                db.execute("INSERT OR IGNORE INTO paper_tags(paper_id, tag_id, method, confidence, assigned_at)"
                           " VALUES (?,?,'lexical',1.0,?)", (d["id"], tid, now))

        n_recent = sum(1 for d in docs if d["date"] > recent_start)
        n_base = len(docs) - n_recent
        if kind in ("weekly", "landscape") and p.external.get("enabled", True) and openalex is not None:
            ext = refresh_external(db, settings, as_of, openalex)
        elif p.external.get("enabled", True):
            ext = stored_external(db, as_of)
        else:
            ext = {}
        prev = latest_snapshot(db, before=as_of)
        prev_scores = prev.scores if prev else {}
        result: dict[str, dict] = {}
        for t in tags:
            ds = tagged[t.id]
            windows = {}
            for w in p.frequency_windows:
                ws = as_of - timedelta(days=w)
                windows[w] = (sum(1 for d in ds if d["date"] > ws), sum(1 for d in docs if d["date"] > ws))
            rec = sorted((d for d in ds if d["date"] > recent_start), key=lambda d: (d["date"], d["canonical_id"]),
                         reverse=True)
            counts = TagCounts(t.id, len(rec), n_recent, len(ds) - len(rec), n_base, windows,
                               [d["canonical_id"] for d in rec[:25]])
            tr = compute_trend(counts, weights=p.weights, pseudo_count=sm.get("pseudo_count", 1.0),
                               frequency_scale=sm.get("frequency_scale", 0.05),
                               growth_scale=sm.get("growth_logistic_scale", 0.7), min_sample=p.min_sample,
                               ema_alpha=sm.get("ema_alpha", 0.3), previous=prev_scores.get(t.id),
                               external=ext.get(t.id), manual_override=t.manual_override)
            result[t.id] = {**tr.to_json(), "supporting_paper_ids": counts.supporting_ids, "seed": t.seed,
                            "pinned": t.pinned, "status": t.status}
        payload = {
            "algorithm": ALGORITHM, "version": version, "as_of_date": as_of.isoformat(), "kind": kind,
            "windows": {"recent_days": p.recent_window_days, "baseline_days": p.baseline_window_days,
                        "recent": [recent_start.isoformat(), as_of.isoformat()],
                        "baseline": [(recent_start - timedelta(days=p.baseline_window_days)).isoformat(),
                                     recent_start.isoformat()]},
            "corpus": {"size": len(docs), "recent": n_recent, "baseline": n_base,
                       "definition": f"papers with prescreen_score >= {p.corpus_min_prescreen}",
                       "sources": _source_counts(docs), "strict_as_of": strict_as_of},
            "previous_snapshot_id": prev.id if prev else None,
            "external_available": sorted(k for k, v in ext.items() if v is not None),
            "tags": result,
        }
        existing = db.scalar("SELECT id FROM trend_snapshots WHERE as_of_date=? AND kind=? AND version=?",
                             (as_of.isoformat(), kind, version))
        if existing:
            db.execute("UPDATE trend_snapshots SET corpus_size=?, payload=?, status='valid', created_at=?, run_id=? WHERE id=?",
                       (len(docs), dumps(payload), now, run_id, existing))
            sid = existing
        else:
            sid = db.execute("INSERT INTO trend_snapshots(as_of_date, kind, version, corpus_size, payload, status, created_at,"
                             " run_id) VALUES (?,?,?,?,?,'valid',?,?)",
                             (as_of.isoformat(), kind, version, len(docs), dumps(payload), now, run_id)).lastrowid
        db.execute("DELETE FROM trend_observations WHERE snapshot_id=?", (sid,))
        for tid, r in result.items():
            for w, v in r["counts"]["windows"].items():
                db.execute("INSERT INTO trend_observations(snapshot_id, tag_id, window_days, tagged_count, corpus_count)"
                           " VALUES (?,?,?,?,?)", (sid, tid, int(w), v["tagged"], v["corpus"]))
            wins = r["counts"]["windows"]
            db.execute(
                "UPDATE topic_tags SET current_score=?, initial_score=COALESCE(initial_score, ?), frequency_7d=?,"
                " frequency_30d=?, frequency_90d=?, baseline_frequency=?, growth_signal=?, external_signal=?,"
                " supporting_paper_ids=?, updated_at=? WHERE id=?",
                (r["score"], r["score"], wins.get("7", {}).get("tagged"), wins.get("30", {}).get("tagged"),
                 wins.get("90", {}).get("tagged"), r["p_baseline"], r["growth_signal"], r["external_signal"],
                 dumps(r["supporting_paper_ids"]), now, tid))
    return Snapshot(sid, as_of, kind, version, len(docs), result)


def _source_counts(docs: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for d in docs:
        for s in d["sources"]:
            out[s] = out.get(s, 0) + 1
    return dict(sorted(out.items()))


def is_reassessment_day(settings: Settings, d: date) -> bool:
    return d.strftime("%A").lower() == settings.trends.parameters.reassessment_weekday
