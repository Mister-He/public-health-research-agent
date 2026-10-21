"""Durable, per-(provider, query) incremental retrieval.

Invariants
- The watermark is the last UTC calendar date whose window was FULLY stored. It only moves
  forward, and only in the same transaction that stores the final page of a chunk.
- Each run starts at (watermark + 1 - overlap) — never at "today - lookback" — so failed or
  missed runs are caught up in bounded chunks over the following runs.
- A failing provider/query keeps its watermark; others continue.
- Durability on CI comes from publishing the DB snapshot to the data branch: an unpushed
  local watermark is lost with the runner, so the next run safely replays the range.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

from research_agent.ingestion.base import Provider
from research_agent.ingestion.http import HttpClient, ProviderError, WindowTooLarge
from research_agent.metadata.deduplication import ingest_record
from research_agent.retrieval.query_builder import QuerySpec, build_queries
from research_agent.settings import Settings
from research_agent.storage.database import Database
from research_agent.timeutil import date_chunks, iso_utc

log = logging.getLogger(__name__)


@dataclass
class QueryOutcome:
    provider: str
    query_id: str
    windows: list[tuple[str, str]] = field(default_factory=list)
    records: int = 0
    new_papers: int = 0
    pages: int = 0
    status: str = "ok"            # ok | partial (caught up some chunks) | failed | skipped
    error: str | None = None
    watermark: str | None = None
    caught_up: bool = True


@dataclass
class IngestSummary:
    outcomes: list[QueryOutcome] = field(default_factory=list)
    touched_paper_ids: set[int] = field(default_factory=set)
    raw_records: int = 0

    @property
    def failed_providers(self) -> set[str]:
        return {o.provider for o in self.outcomes if o.status == "failed"}

    @property
    def successful_providers(self) -> set[str]:
        ok = {o.provider for o in self.outcomes if o.status in ("ok", "partial")}
        return ok - self.failed_providers


def make_providers(settings: Settings, http: HttpClient) -> dict[str, Provider]:
    from research_agent.ingestion.arxiv import ArxivProvider
    from research_agent.ingestion.crossref import CrossrefProvider
    from research_agent.ingestion.europe_pmc import EuropePMCProvider
    from research_agent.ingestion.medrxiv import MedRxivProvider
    from research_agent.ingestion.openalex import OpenAlexProvider
    from research_agent.ingestion.pubmed import PubMedProvider

    email = settings.contact_email
    classes = {"pubmed": (PubMedProvider, "NCBI_API_KEY"), "europe_pmc": (EuropePMCProvider, None),
               "openalex": (OpenAlexProvider, "OPENALEX_API_KEY"), "crossref": (CrossrefProvider, None),
               "medrxiv": (MedRxivProvider, None), "arxiv": (ArxivProvider, None)}
    out: dict[str, Provider] = {}
    for name, (cls, key_env) in classes.items():
        cfg = settings.sources.sources.get(name)
        if cfg and cfg.enabled:
            out[name] = cls(http, cfg, contact_email=email, api_key=os.environ.get(key_env, "") if key_env else "")
    return out


def make_http(settings: Settings, transport=None) -> HttpClient:
    h = settings.sources.http
    return HttpClient(contact_email=settings.contact_email, connect_timeout=h.connect_timeout_seconds,
                      read_timeout=h.read_timeout_seconds, max_retries=h.max_retries,
                      backoff_base=h.backoff_base_seconds, backoff_max=h.backoff_max_seconds,
                      user_agent=h.user_agent, transport=transport)


def _register_query(db: Database, spec: QuerySpec) -> None:
    now = iso_utc()
    db.execute("INSERT INTO search_queries(query_id, provider, strategy, query_text, query_hash, created_at, last_used_at)"
               " VALUES (?,?,?,?,?,?,?) ON CONFLICT(provider, query_id) DO UPDATE SET query_text=excluded.query_text,"
               " query_hash=excluded.query_hash, last_used_at=excluded.last_used_at",
               (spec.query_id, spec.provider, spec.strategy, spec.text, spec.query_hash, now, now))


def get_checkpoint(db: Database, provider: str, query_id: str):
    return db.one("SELECT * FROM retrieval_checkpoints WHERE provider=? AND query_id=?", (provider, query_id))


def plan_windows(watermark: date | None, end: date, *, overlap_days: int, initial_days: int,
                 chunk_days: int, max_chunks: int) -> list[tuple[date, date]]:
    """Chunks to fetch this run. Starts from the durable watermark (minus overlap); a missing
    watermark starts `initial_days` back."""
    if watermark is None:
        start = end - timedelta(days=initial_days - 1)
    else:
        start = watermark + timedelta(days=1) - timedelta(days=overlap_days)
    return date_chunks(start, end, chunk_days)[:max_chunks]


def run_ingestion(db: Database, settings: Settings, providers: dict[str, Provider], *, run_id: str,
                  end_date: date, initial_days: int, only: set[str] | None = None,
                  deadline: float | None = None, clock: Callable[[], float] = time.monotonic,
                  specs: list[QuerySpec] | None = None) -> IngestSummary:
    summary = IngestSummary()
    overlap = settings.runtime.windows["daily_lookback_days"]
    max_chunks = settings.runtime.max_chunks_per_query_per_run
    for spec in specs if specs is not None else build_queries(settings):
        if only and spec.provider not in only:
            continue
        prov = providers.get(spec.provider)
        if prov is None:
            continue
        out = QueryOutcome(spec.provider, spec.query_id)
        summary.outcomes.append(out)
        _register_query(db, spec)
        cp = get_checkpoint(db, spec.provider, spec.query_id)
        wm = date.fromisoformat(cp["watermark_date"]) if cp and cp["watermark_date"] else None
        if cp and cp["query_hash"] != spec.query_hash:
            log.warning("query changed; restarting watermark", extra={"provider": spec.provider, "query": spec.query_id})
            wm = None
        chunk_days = settings.sources.sources[spec.provider].chunk_days
        windows = plan_windows(wm, end_date, overlap_days=overlap, initial_days=initial_days,
                               chunk_days=chunk_days, max_chunks=max_chunks)
        all_windows = plan_windows(wm, end_date, overlap_days=overlap, initial_days=initial_days,
                                   chunk_days=chunk_days, max_chunks=10_000)
        out.caught_up = len(windows) == len(all_windows)
        queue = list(windows)
        while queue:
            start, end = queue.pop(0)
            if deadline is not None and clock() > deadline:
                out.status, out.caught_up = "partial", False
                break
            cursor = None
            if cp and cp["cursor"] and cp["window_start"] == start.isoformat() and cp["window_end"] == end.isoformat() \
                    and cp["query_hash"] == spec.query_hash:
                cursor = cp["cursor"]
            ok = _fetch_chunk(db, prov, spec, start, end, cursor, run_id, out, summary, wm)
            if ok == "split":
                # Too many records for one window: fetch the two halves in order instead.
                mid = start + (end - start) // 2
                queue[:0] = [(start, mid), (mid + timedelta(days=1), end)]
                log.info("window split", extra={"provider": spec.provider, "query": spec.query_id,
                                                "window": f"{start}..{end}"})
                continue
            if not ok:
                break
            wm = max(wm, end) if wm else end
            cp = get_checkpoint(db, spec.provider, spec.query_id)
        out.watermark = wm.isoformat() if wm else None
        if out.status == "ok" and not out.caught_up:
            out.status = "partial"
    return summary


def _fetch_chunk(db: Database, prov: Provider, spec: QuerySpec, start: date, end: date, cursor: str | None,
                 run_id: str, out: QueryOutcome, summary: IngestSummary, wm: date | None) -> bool | str:
    started = iso_utc()
    pages = records = new = 0
    try:
        for page in prov.fetch_window(spec, start, end, cursor):
            # One transaction per page: records + cursor (+ watermark on the final page).
            with db.transaction():
                for rec in page.records:
                    res = ingest_record(db, rec, run_id=run_id)
                    summary.touched_paper_ids.add(res.paper_id)
                    new += res.created
                records += len(page.records)
                pages += 1
                summary.raw_records += page.raw_count
                final = page.next_cursor is None
                new_wm = (max(wm, end) if wm else end) if final else wm
                _save_checkpoint(db, spec, new_wm, status="ok" if final else "partial",
                                 window=(start, end), cursor=None if final else page.next_cursor,
                                 pages_done=pages, run_id=run_id, error=None, success=final)
                for w in page.warnings:
                    log.warning(w, extra={"provider": spec.provider, "query": spec.query_id})
    except WindowTooLarge:
        if pages == 0 and end > start:
            return "split"
        exc = ProviderError(spec.provider, f"single-day window {start} exceeds max_records_per_query_window")
        _save_checkpoint(db, spec, wm, status="failed", window=(start, end), cursor=None, pages_done=pages,
                         run_id=run_id, error=str(exc), success=False)
        _log_run(db, run_id, spec, start, end, "failed", records, new, pages, started, str(exc))
        out.status, out.error = "failed", str(exc)
        return False
    except Exception as exc:  # isolate any provider/query failure; the watermark is not advanced
        if not isinstance(exc, ProviderError):
            exc = ProviderError(spec.provider, f"{type(exc).__name__}: {exc}", retryable=True)
        _save_checkpoint(db, spec, wm, status="failed", window=(start, end),
                         cursor=(get_checkpoint(db, spec.provider, spec.query_id) or {"cursor": None})["cursor"],
                         pages_done=pages, run_id=run_id, error=str(exc)[:500], success=False)
        _log_run(db, run_id, spec, start, end, "failed", records, new, pages, started, str(exc))
        out.status, out.error = "failed", str(exc)[:300]
        out.records += records
        out.pages += pages
        log.error("ingestion_failed", extra={"provider": spec.provider, "query": spec.query_id,
                                             "window": f"{start}..{end}", "error": str(exc)[:300]})
        return False
    _log_run(db, run_id, spec, start, end, "completed", records, new, pages, started, None)
    out.windows.append((start.isoformat(), end.isoformat()))
    out.records += records
    out.new_papers += new
    out.pages += pages
    return True


def _save_checkpoint(db: Database, spec: QuerySpec, wm: date | None, *, status: str, window, cursor, pages_done,
                     run_id, error, success) -> None:
    now = iso_utc()
    db.execute(
        "INSERT INTO retrieval_checkpoints(provider, query_id, query_hash, watermark_date, status, window_start, window_end,"
        " cursor, pages_done, last_success_at, last_error, last_run_id, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(provider, query_id) DO UPDATE SET query_hash=excluded.query_hash,"
        " watermark_date=excluded.watermark_date, status=excluded.status, window_start=excluded.window_start,"
        " window_end=excluded.window_end, cursor=excluded.cursor, pages_done=excluded.pages_done,"
        " last_success_at=COALESCE(excluded.last_success_at, retrieval_checkpoints.last_success_at),"
        " last_error=excluded.last_error, last_run_id=excluded.last_run_id, updated_at=excluded.updated_at",
        (spec.provider, spec.query_id, spec.query_hash, wm.isoformat() if wm else None, status,
         window[0].isoformat(), window[1].isoformat(), cursor, pages_done, now if success else None, error, run_id, now))


def _log_run(db, run_id, spec, start, end, status, records, new, pages, started, error) -> None:
    db.execute("INSERT INTO ingestion_runs(run_id, provider, query_id, window_start, window_end, status, records,"
               " new_papers, pages, started_at, finished_at, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
               (run_id, spec.provider, spec.query_id, start.isoformat(), end.isoformat(), status, records, new, pages,
                started, iso_utc(), error))


def watermarks(db: Database) -> dict[str, str | None]:
    return {f"{r['provider']}:{r['query_id']}": r["watermark_date"]
            for r in db.query("SELECT provider, query_id, watermark_date FROM retrieval_checkpoints ORDER BY 1,2")}
