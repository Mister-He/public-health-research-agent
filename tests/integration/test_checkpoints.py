"""Durable watermarks, outage catch-up, page-level resume, delayed indexing and replay safety."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from research_agent.ingestion.base import Page
from research_agent.ingestion.http import ProviderError
from research_agent.metadata.schemas import SourceRecord
from research_agent.pipeline.checkpoints import get_checkpoint, plan_windows, run_ingestion
from research_agent.retrieval.query_builder import QuerySpec

SPEC = QuerySpec("pubmed", "topic:test", "topic", "x")
END = date(2026, 10, 21)


def rec(i: int, day: date) -> SourceRecord:
    return SourceRecord(provider="pubmed", source_record_id=str(999990000 + i), query_id=SPEC.query_id,
                        strategy="topic", retrieved_at="2026-10-21T00:00:00Z", pmid=str(999990000 + i),
                        title=f"Record {i} on microsimulation", indexed_date=day)


class FakeProvider:
    """Serves records by index date; can fail on a given window or page."""
    name = "pubmed"

    def __init__(self, by_day: dict[date, list[SourceRecord]], fail_on: set[date] = frozenset(), page_size: int = 2,
                 fail_page: int | None = None):
        self.by_day, self.fail_on, self.page_size, self.fail_page = by_day, set(fail_on), page_size, fail_page
        self.calls: list[tuple[date, date, str | None]] = []

    def fetch_window(self, spec, start, end, cursor):
        self.calls.append((start, end, cursor))
        if any(start <= d <= end for d in self.fail_on):
            raise ProviderError("pubmed", "HTTP 500", status=500)
        items = [r for d, rs in sorted(self.by_day.items()) if start <= d <= end for r in rs]
        pos = int(cursor or 0)
        page_no = 0
        while True:
            if self.fail_page is not None and page_no == self.fail_page:
                self.fail_page = None
                raise ProviderError("pubmed", "timeout mid-window")
            chunk = items[pos:pos + self.page_size]
            pos += len(chunk)
            nxt = str(pos) if pos < len(items) else None
            yield Page(chunk, nxt, raw_count=len(chunk))
            page_no += 1
            if nxt is None:
                return


def run(db, settings, prov, end=END, initial=7):
    return run_ingestion(db, settings, {"pubmed": prov}, run_id="r", end_date=end, initial_days=initial, specs=[SPEC])


def wm(db):
    cp = get_checkpoint(db, "pubmed", SPEC.query_id)
    return cp["watermark_date"] if cp else None


def test_plan_windows_start_from_watermark_with_overlap():
    w = plan_windows(date(2026, 10, 10), END, overlap_days=7, initial_days=180, chunk_days=14, max_chunks=6)
    assert w[0][0] == date(2026, 10, 4) and w[-1][1] == END
    first = plan_windows(None, END, overlap_days=7, initial_days=180, chunk_days=30, max_chunks=100)
    assert first[0][0] == END - timedelta(days=179) and len(first) == 6


def test_watermark_advances_only_after_success(db, settings):
    prov = FakeProvider({END - timedelta(days=1): [rec(1, END)]})
    run(db, settings, prov)
    assert wm(db) == END.isoformat()
    bad = FakeProvider({END + timedelta(days=1): [rec(2, END)]}, fail_on={END + timedelta(days=1)})
    s = run(db, settings, bad, end=END + timedelta(days=1))
    assert s.outcomes[0].status == "failed" and wm(db) == END.isoformat()   # not advanced
    assert get_checkpoint(db, "pubmed", SPEC.query_id)["status"] == "failed"


def test_missed_runs_are_caught_up_in_bounded_chunks(db, settings):
    settings = settings.model_copy(deep=True)
    settings.runtime.max_chunks_per_query_per_run = 2
    settings.sources.sources["pubmed"].chunk_days = 7
    days = {END - timedelta(days=i): [rec(i, END - timedelta(days=i))] for i in range(0, 40)}
    prov = FakeProvider(days)
    run(db, settings, prov, end=END - timedelta(days=30))                    # last successful run 30 days ago
    assert wm(db) == (END - timedelta(days=30)).isoformat()
    s = run(db, settings, prov)                                             # outage of 30 days, chunk-limited
    assert not s.outcomes[0].caught_up and s.outcomes[0].status == "partial"
    first_start = prov.calls[-2][0]
    assert first_start == END - timedelta(days=30) + timedelta(days=1) - timedelta(days=7)   # from watermark, not today
    for _ in range(5):
        s = run(db, settings, prov)
        if s.outcomes[0].caught_up:
            break
    assert wm(db) == END.isoformat()
    covered = {r[0] for r in db.query("SELECT value FROM paper_identifiers WHERE scheme='pmid'")}
    # Everything from the first run's initial 7-day window (days 36..30 ago) up to today is present:
    # nothing published during the 30-day outage was lost. Older records were never in scope.
    assert covered == {str(999990000 + i) for i in range(0, 37)}


def test_page_resume_from_cursor(db, settings):
    day = END - timedelta(days=2)
    prov = FakeProvider({day: [rec(i, day) for i in range(6)]}, page_size=2, fail_page=1)
    s = run(db, settings, prov)
    assert s.outcomes[0].status == "failed" and wm(db) is None
    cp = get_checkpoint(db, "pubmed", SPEC.query_id)
    assert cp["cursor"] == "2"                                              # first page durably stored
    assert db.scalar("SELECT COUNT(*) FROM papers") == 2
    run(db, settings, prov)
    assert prov.calls[-1][2] == "2" and db.scalar("SELECT COUNT(*) FROM papers") == 6 and wm(db) == END.isoformat()


def test_delayed_indexing_caught_by_overlap(db, settings):
    late_day = END - timedelta(days=3)
    prov = FakeProvider({})
    run(db, settings, prov)                                                 # nothing indexed yet
    prov.by_day[late_day] = [rec(7, late_day)]                              # indexed late, dated 3 days ago
    run(db, settings, prov, end=END + timedelta(days=1))
    assert db.scalar("SELECT COUNT(*) FROM papers") == 1


def test_replay_does_not_duplicate(db, settings):
    day = END - timedelta(days=1)
    prov = FakeProvider({day: [rec(1, day), rec(2, day)]})
    for _ in range(3):
        run(db, settings, prov)
    assert db.scalar("SELECT COUNT(*) FROM papers") == 2
    assert db.scalar("SELECT COUNT(*) FROM ingestion_runs WHERE status='completed'") == 3


def test_changed_query_restarts_its_watermark(db, settings):
    prov = FakeProvider({})
    run(db, settings, prov)
    changed = QuerySpec("pubmed", SPEC.query_id, "topic", "y")
    n = len(prov.calls)
    run_ingestion(db, settings, {"pubmed": prov}, run_id="r", end_date=END, initial_days=30, specs=[changed])
    assert prov.calls[n][0] == END - timedelta(days=29)          # restarted from the initial window, not the old watermark


@pytest.mark.parametrize("deadline_hit", [True])
def test_time_budget_stops_cleanly(db, settings, deadline_hit):
    prov = FakeProvider({})
    s = run_ingestion(db, settings, {"pubmed": prov}, run_id="r", end_date=END, initial_days=180, specs=[SPEC],
                      deadline=0.0, clock=lambda: 1.0)
    assert s.outcomes[0].status == "partial" and not s.outcomes[0].caught_up and wm(db) is None


class TooBigProvider(FakeProvider):
    """Refuses windows longer than `max_days`, like an API whose result count exceeds the limit."""

    def __init__(self, by_day, max_days):
        super().__init__(by_day)
        self.max_days = max_days

    def fetch_window(self, spec, start, end, cursor):
        from research_agent.ingestion.http import WindowTooLarge
        if (end - start).days + 1 > self.max_days:
            self.calls.append((start, end, "too-big"))
            raise WindowTooLarge("pubmed", 9999, 5000)
        yield from super().fetch_window(spec, start, end, cursor)


def test_oversized_window_is_split_not_failed(db, settings):
    days = {END - timedelta(days=i): [rec(i, END - timedelta(days=i))] for i in range(0, 30)}
    prov = TooBigProvider(days, max_days=4)
    s = run_ingestion(db, settings, {"pubmed": prov}, run_id="r", end_date=END, initial_days=14, specs=[SPEC])
    assert s.outcomes[0].status == "ok" and wm(db) == END.isoformat()
    assert db.scalar("SELECT COUNT(*) FROM papers") == 14                  # every day of the window fetched
    fetched = sorted((a, b) for a, b, c in prov.calls if c != "too-big")
    assert all((b - a).days + 1 <= 4 for a, b in fetched)
    assert fetched[0][0] == END - timedelta(days=13) and fetched[-1][1] == END   # contiguous coverage
