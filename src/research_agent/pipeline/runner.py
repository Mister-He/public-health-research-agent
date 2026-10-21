"""Pipeline stages.

ingest   : checkpointed retrieval -> dedup/persist -> prescreen -> enqueue -> lease a bounded
           Claude manifest. (CI persists the DB to the data branch right after, BEFORE Claude.)
analyze  : (CI only) claude-code-action reads the manifest and writes JSON files.
prepare  : deterministic import + trends + selection on a throwaway DB copy, to give Claude the
           exact final selection for the narrative (and weekly tag-proposal clusters).
publish  : import + validate outputs -> trend snapshot -> selection -> narrative -> render to
           staging -> independent gate (+ bounded repair) -> atomic promote -> mark published.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from research_agent.llm import claude_code_action as cca
from research_agent.metadata.deduplication import ingest_record
from research_agent.pipeline.checkpoints import make_http, make_providers, run_ingestion, watermarks
from research_agent.pipeline.state import RunRecord, latest_stage_summary
from research_agent.ranking.selector import run_selection
from research_agent.reporting.renderer import load_items, narrative_manifest, render, set_validation_status
from research_agent.retrieval.screening import prescreen, shortlist_and_enqueue
from research_agent.settings import Settings
from research_agent.storage import analysis_queue as q
from research_agent.storage.compaction import apply_retention, candidates_for_compaction, compact_papers
from research_agent.storage.database import Database, dumps
from research_agent.storage.repositories import load_paper, record_api_usage, save_paper
from research_agent.timeutil import iso_utc, utcnow
from research_agent.trends.clustering import cluster_unmatched
from research_agent.trends.updater import corpus, is_reassessment_day, latest_snapshot, refresh_external, update_trends
from research_agent.validation.gate import persist_results, run_gate
from research_agent.validation.links import LinkVerifier
from research_agent.validation.markdown import parse
from research_agent.validation.metadata import IdentityVerifier
from research_agent.validation.results import GateResult

log = logging.getLogger(__name__)

EXIT_OK, EXIT_BLOCKED, EXIT_CONFIG, EXIT_ANALYSIS_DEFERRED, EXIT_NO_REPORT, EXIT_STALE = 0, 1, 2, 3, 4, 5


# =============================================================================== ingest

def ingest_stage(db: Database, settings: Settings, report_date: date, *, run_id: str, providers=None,
                 skip_llm: bool = False, max_eval: int | None = None, time_budget_s: float | None = None,
                 only: set[str] | None = None) -> dict:
    rec = RunRecord(run_id, "ingest", report_date).start(db, settings)
    http = None
    if providers is None:
        http = make_http(settings)
        providers = make_providers(settings, http)
    landscape_done = db.get_meta("landscape_completed_at")
    w = settings.runtime.windows
    initial = w["initial_baseline_days"] if not landscape_done else w["daily_lookback_days"]
    if not landscape_done:
        # The landscape backfill may need several runs; allow more chunks per run.
        settings = settings.model_copy(deep=True)
        settings.runtime.max_chunks_per_query_per_run = max(settings.runtime.max_chunks_per_query_per_run, 40)
    deadline = time.monotonic() + time_budget_s if time_budget_s else None
    summ = run_ingestion(db, settings, providers, run_id=run_id, end_date=utcnow().date(), initial_days=initial,
                         deadline=deadline, only=only)
    if http is not None:
        record_api_usage(db, run_id, http.usage)
    # Prescreen everything touched + anything never screened (e.g. after a crash).
    unscreened = {r[0] for r in db.query("SELECT id FROM papers WHERE prescreen_score IS NULL")}
    prescreen(db, settings, sorted(summ.touched_paper_ids | unscreened))
    caught_up = all(o.caught_up and o.status != "failed" for o in summ.outcomes)
    landscape = None
    if not landscape_done and caught_up and summ.outcomes:
        snap = update_trends(db, settings, report_date, kind="landscape", run_id=run_id,
                             openalex=providers.get("openalex"))
        from research_agent.reporting.landscape import render_landscape

        db.set_meta("landscape_report_md", render_landscape(settings, snap, db))
        db.set_meta("landscape_completed_at", iso_utc())
        landscape = {"snapshot_id": snap.id, "corpus_size": snap.corpus_size}
    elif landscape_done and is_reassessment_day(settings, report_date) \
            and db.get_meta("external_refreshed_for") != report_date.isoformat() and "openalex" in providers \
            and settings.trends.parameters.external.get("enabled", True):
        with db.transaction():
            refresh_external(db, settings, report_date, providers["openalex"])
            db.set_meta("external_refreshed_for", report_date.isoformat())
    v1_dates = fill_preprint_first_dates(db, settings, report_date, providers.get("medrxiv"))
    screen = shortlist_and_enqueue(db, settings, report_date)
    manifest = None
    if not skip_llm:
        manifest = cca.build_scoring_manifest(db, settings, run_id=run_id, report_date=report_date, limit=max_eval)
    # Brief-info storage: everything not queued for Claude keeps identifiers/title/dates/tags only.
    compacted = compact_papers(db, candidates_for_compaction(db))
    retention = apply_retention(db, today_iso=utcnow().date().isoformat(),
                                keep_days=settings.runtime.windows.get("brief_retention_days", 45),
                                relevance_floor=settings.trends.parameters.corpus_min_prescreen)
    queue = q.status_counts(db)
    rec.summary.update({
        "stage": "ingest", "scheduled_date": report_date.isoformat(), "run_id": run_id,
        "raw_retrieved": summ.raw_records, "unique_papers": len(summ.touched_paper_ids),
        "providers": {o.provider: o.status for o in summ.outcomes if o.status == "failed"} or "all ok",
        "provider_outcomes": [o.__dict__ for o in summ.outcomes],
        "successful_providers": sorted(summ.successful_providers), "failed_providers": sorted(summ.failed_providers),
        "caught_up": caught_up, "landscape": landscape or ("completed" if landscape_done else "in_progress"),
        "prescreen_shortlist_pool": screen.above_threshold, "enqueued": screen.enqueued,
        "compacted_to_brief_info": compacted, "retention": retention, "preprint_first_dates_looked_up": v1_dates,
        "manifest_papers": manifest["paper_count"] if manifest else 0,
        "pending_analysis_count": queue["by_status"].get("pending", 0) + queue["by_status"].get("in_progress", 0),
        "retryable_analysis_count": queue["by_status"].get("retryable_failed", 0),
        "oldest_pending_discovery_date": queue["oldest_pending_discovery"],
        "retrieval_watermarks_by_source_query": watermarks(db),
        "api_calls_by_provider": {k: v.requests for k, v in (http.usage.items() if http else [])},
    })
    rec.finish(db, "completed" if not summ.failed_providers else "partial")
    return rec.summary


def fill_preprint_first_dates(db: Database, settings: Settings, report_date: date, medrxiv, limit: int = 60) -> int:
    """A revised medRxiv preprint seen only as v2+ lacks its first-posting date. Look it up only for
    papers that could actually be shortlisted (relevant, revised within the horizon) — not for
    every revision in the daily dump (the API allows ~1 request/s)."""
    if medrxiv is None:
        return 0
    from research_agent.ingestion.medrxiv import map_medrxiv
    from research_agent.retrieval.screening import MIN_PRESCREEN

    horizon = report_date - timedelta(days=settings.ranking.publication_horizon_days)
    rows = db.query(
        "SELECT id, json_extract(data, '$.doi') AS doi FROM papers WHERE compacted=0 AND is_preprint=1"
        " AND first_publication_date IS NULL AND prescreen_score >= ? AND json_extract(data, '$.preprint_id') LIKE 'medrxiv:%'"
        " AND json_extract(data, '$.latest_revision_date') >= ? ORDER BY prescreen_score DESC LIMIT ?",
        (MIN_PRESCREEN, horizon.isoformat(), limit))
    n = 0
    for r in rows:
        try:
            versions = medrxiv.versions(r["doi"])
        except Exception as exc:  # best effort: the date stays missing (never guessed)
            log.warning("preprint version lookup failed", extra={"doi": r["doi"], "error": str(exc)[:160]})
            continue
        for v in versions:
            if str(v.get("version")) == "1":
                rec = map_medrxiv(v, "enrichment:first_version", "enrichment")
                if rec:
                    ingest_record(db, rec)
                    n += 1
    return n


# =============================================================================== prepare (analysis job)

def prepare_narrative(db: Database, settings: Settings, report_date: date, *, run_id: str) -> dict | None:
    """On a throwaway DB copy: reproduce publish's deterministic steps and write the narrative manifest."""
    status = cca.read_status(settings, "scoring")
    cca.import_scoring_outputs(db, settings, run_id=run_id, status=status)
    kind = "weekly" if is_reassessment_day(settings, report_date) else "daily"
    snap = update_trends(db, settings, report_date, kind=kind, run_id=run_id)
    sel = run_selection(db, settings, report_date, run_id=run_id)
    if not sel.selected:
        return None
    items = load_items(db, settings, report_date)
    payload = narrative_manifest(settings, report_date, items, _snapshot_payload(db, snap.id))
    cca.write_narrative_manifest(settings, payload)
    return payload


def prepare_trend_discovery(db: Database, settings: Settings, report_date: date) -> dict | None:
    if not is_reassessment_day(settings, report_date):
        return None
    p = settings.trends.parameters
    docs = corpus(db, settings, report_date, strict_as_of=False)
    recent = [d for d in docs if d["date"] > report_date - timedelta(days=p.recent_window_days)]
    tagged = {r[0] for r in db.query("SELECT DISTINCT paper_id FROM paper_tags")}
    unmatched = sorted((d for d in recent if d["id"] not in tagged), key=lambda d: d["canonical_id"])
    share = len(unmatched) / len(recent) if recent else 0
    if not recent or share < p.proposals.get("min_unmatched_share", 0.1):
        return None
    clusters = cluster_unmatched(unmatched, min_cluster_size=int(p.proposals.get("min_cluster_size", 8)))
    if not clusters:
        return None
    payload = {"as_of_date": report_date.isoformat(), "max_new": int(p.proposals.get("max_new_per_week", 3)),
               "taxonomy": [{"id": t.id, "name": t.name, "description": t.description, "aliases": t.aliases}
                            for t in settings.trends.tags if t.status != "archived"],
               "sample": {"recent_corpus": len(recent), "unmatched": len(unmatched),
                          "window_days": p.recent_window_days},
               "clusters": clusters}
    cca.write_trend_manifest(settings, payload)
    return payload


# =============================================================================== publish

@dataclass
class PublishResult:
    status: str                                 # published | already_published | blocked | insufficient | dry_run
    report_path: Path | None = None
    gate: GateResult | None = None
    summary: dict = field(default_factory=dict)
    exit_code: int = EXIT_OK


def _snapshot_payload(db: Database, sid: int | None) -> dict | None:
    if sid is None:
        return None
    row = db.one("SELECT payload FROM trend_snapshots WHERE id=?", (sid,))
    return json.loads(row["payload"]) if row else None


def refresh_metadata(db: Database, settings: Settings, paper_id: int, providers: dict) -> bool:
    """Bounded metadata refresh from authoritative providers before excluding a paper."""
    p = load_paper(db, paper_id)
    recs = []
    try:
        if p.doi and p.preprint_id and "medrxiv" in providers and p.preprint_id.startswith("medrxiv"):
            recs = providers["medrxiv"].lookup(p.doi)
        elif p.doi and "crossref" in providers:
            r = providers["crossref"].lookup_doi(p.doi)
            recs = [r] if r else []
        if p.pmid and "pubmed" in providers:
            recs += providers["pubmed"].fetch_pmids([p.pmid])
    except Exception as exc:
        log.warning("metadata refresh failed", extra={"paper": p.canonical_id, "error": str(exc)[:200]})
        return False
    before = p.metadata_version
    for r in recs:
        ingest_record(db, r)
    return load_paper(db, paper_id).metadata_version != before


def publish_stage(db: Database, settings: Settings, report_date: date, *, run_id: str, online: bool = True,
                  identity: IdentityVerifier | None = None, links: LinkVerifier | None = None, providers=None,
                  dry_run: bool = False, force: bool = False, strict_as_of: bool = False) -> PublishResult:
    rec = RunRecord(run_id, "publish" if not dry_run else "dry_run", report_date).start(db, settings)
    d = report_date.isoformat()
    final = settings.reports_dir / f"{d}.md"
    existing = db.one("SELECT * FROM daily_reports WHERE report_date=? AND status='published'", (d,))
    if existing and final.exists() and not force:
        rec.summary.update({"stage": "publish", "scheduled_date": d, "status": "already_published"})
        rec.finish(db, "skipped")
        return PublishResult("already_published", final, summary=rec.summary)

    # 1. Import Claude outputs (if any were leased by this run).
    status = cca.read_status(settings, "scoring")
    imp = cca.import_scoring_outputs(db, settings, run_id=run_id, status=status)
    # 2. Trend snapshot (weekly reassessment on the configured weekday).
    weekly = is_reassessment_day(settings, report_date)
    snap = update_trends(db, settings, report_date, kind="weekly" if weekly else "daily", run_id=run_id,
                         strict_as_of=strict_as_of)
    proposals = None
    tm = settings.analysis_dir / "trends" / "manifest.json"
    if weekly and tm.exists():
        with db.transaction():
            proposals = cca.import_tag_proposals(db, settings, run_id=run_id, manifest=json.loads(tm.read_text()))
    ingest_summary = latest_stage_summary(db, report_date, "ingest")
    providers_status = {p: "ok" for p in ingest_summary.get("successful_providers", [])}
    providers_status.update({p: "unavailable this run" for p in ingest_summary.get("failed_providers", [])})
    if online and identity is None:
        if providers is None:
            providers = make_providers(settings, make_http(settings))
        identity = IdentityVerifier(providers.get("crossref"), providers.get("pubmed"), providers.get("medrxiv"),
                                    title_ratio=settings.validation.title_match_ratio)
        if settings.validation.url_checks and links is None:
            links = LinkVerifier(timeout=settings.validation.url_timeout_seconds,
                                 user_agent=f"research-agent (mailto:{settings.contact_email or 'unset'})")

    excluded: set[int] = set()
    gate = None
    staging = settings.reports_dir / ".staging" / f"{d}.md"
    sel = None
    for round_no in range(settings.validation.max_repair_rounds + 1):
        sel = run_selection(db, settings, report_date, run_id=run_id, force=force or round_no > 0,
                            exclude_paper_ids=excluded)
        n = len(sel.selected)
        if n < settings.ranking.min_verified_papers:
            break
        items = load_items(db, settings, report_date)
        payload = _snapshot_payload(db, snap.id)
        manifest = narrative_manifest(settings, report_date, items, payload)
        narrative, why = cca.read_narrative(settings, report_date=d, sel_hash=manifest["selection_hash"],
                                            manifest=manifest)
        if narrative is None:
            log.info("using structural fallback overview", extra={"reason": why})
        funnel = {"raw_retrieved": ingest_summary.get("raw_retrieved"), "unique_papers": ingest_summary.get("unique_papers"),
                  "prescreen_shortlist_pool": ingest_summary.get("prescreen_shortlist_pool"), **sel.funnel}
        source = ("simulated fixture provider, not Claude" if settings.llm.model == "fixture-simulated"
                  else f"Claude via claude-code-action, model {settings.llm.model}")
        text = render(db, settings, report_date, narrative=narrative, narrative_source=source,
                      funnel=funnel, providers=providers_status, snapshot_payload=payload,
                      snapshot_version=snap.version, run_id=run_id)
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.write_text(text, encoding="utf-8")
        gate = run_gate(db, settings, report_date, staging, text, identity=identity if online else None,
                        links=links if online else None, final_dir=settings.reports_dir)
        persist_results(db, report_date, gate, run_id=run_id, round_no=round_no)
        if gate.passed:
            break
        if gate.report_level_failures():
            log.error("report-level validation failure", extra={"failures": [c.__dict__ for c in gate.report_level_failures()][:10]})
            break
        bad = gate.failing_papers()
        log.warning("excluding papers that failed validation", extra={"papers": sorted(bad)})
        for cid in bad:
            pid = db.scalar("SELECT paper_id FROM recommendations WHERE report_date=? AND canonical_id=?", (d, cid))
            if pid is None:
                continue
            if providers and round_no == 0 and refresh_metadata(db, settings, pid, providers):
                continue  # metadata changed: re-evaluate later, not today (new version needs a new assessment)
            excluded.add(pid)
            p = load_paper(db, pid)
            p.validation_status = "failed"
            save_paper(db, p)

    n = len(sel.selected) if sel else 0
    rec.summary.update({"stage": "publish", "scheduled_date": d, "run_id": run_id, "selected_count": n,
                        "llm_evaluated_count": imp.completed, "analysis_failures": imp.failed,
                        "claude_action_failure_category": status.category if status else None,
                        "trend_snapshot_id": snap.id, "trend_kind": snap.kind, "tag_proposals": proposals,
                        "excluded_by_validation": len(excluded), "funnel": sel.funnel if sel else {},
                        "exclusion_reasons": sel.excluded if sel else {}})
    if n < settings.ranking.min_verified_papers:
        rec.summary["status"] = "insufficient"
        rec.summary["reason"] = (f"{n} verified paper(s) < minimum {settings.ranking.min_verified_papers}"
                                 + (f"; Claude analysis unavailable ({status.category})" if status and status.category else ""))
        rec.finish(db, "no_report")
        staging.unlink(missing_ok=True)
        code = EXIT_ANALYSIS_DEFERRED if status and status.category in q.OUTAGE else EXIT_NO_REPORT
        return PublishResult("insufficient", None, gate, rec.summary, code)
    rec.summary.update({"validation": gate.summary() if gate else None,
                        "warnings": [c.__dict__ for c in gate.warnings][:30] if gate else []})
    if not gate or not gate.passed:
        rec.summary["status"] = "blocked"
        rec.summary["failures"] = [c.__dict__ for c in gate.failures][:30] if gate else []
        rec.finish(db, "blocked")
        return PublishResult("blocked", staging, gate, rec.summary, EXIT_BLOCKED)
    if dry_run:
        rec.summary["status"] = "dry_run"
        rec.finish(db, "dry_run")
        return PublishResult("dry_run", staging, gate, rec.summary)

    # Promote atomically, after a final structural re-check of the exact bytes being published.
    final_text = set_validation_status(staging.read_text(encoding="utf-8"), "passed")
    if parse(final_text).front_matter.get("validation_status") != "passed":  # type: ignore[union-attr]
        raise RuntimeError("could not set validation status")
    staging.write_text(final_text, encoding="utf-8")
    os.replace(staging, final)
    import hashlib

    with db.transaction():
        db.execute("UPDATE recommendations SET status='published' WHERE report_date=? AND status='selected'", (d,))
        db.execute("INSERT INTO daily_reports(report_date, path, sha256, status, validation_status, paper_count, front_matter,"
                   " generated_at, run_id) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(report_date) DO UPDATE SET"
                   " path=excluded.path, sha256=excluded.sha256, status=excluded.status,"
                   " validation_status=excluded.validation_status, paper_count=excluded.paper_count,"
                   " front_matter=excluded.front_matter, generated_at=excluded.generated_at, run_id=excluded.run_id",
                   (d, str(final.relative_to(settings.root)), hashlib.sha256(final_text.encode()).hexdigest(), "published",
                    "passed", n, dumps(parse(final_text).front_matter), iso_utc(), run_id))
        for r in sel.selected:
            p = load_paper(db, r["paper_id"])
            p.validation_status = "verified" if online else p.validation_status
            p.last_verified_at = iso_utc() if online else p.last_verified_at
            save_paper(db, p)
    rec.summary["status"] = "published"
    rec.summary["report"] = str(final.relative_to(settings.root))
    rec.finish(db, "completed")
    return PublishResult("published", final, gate, rec.summary)


def write_landscape_file(db: Database, settings: Settings) -> Path | None:
    md = db.get_meta("landscape_report_md")
    if not md:
        return None
    done = (db.get_meta("landscape_completed_at") or "")[:10]
    path = settings.reports_dir / "landscape" / f"{done}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_text(encoding="utf-8") != md:
        path.write_text(md, encoding="utf-8")
    return path
