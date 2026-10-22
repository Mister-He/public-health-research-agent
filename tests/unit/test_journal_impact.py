"""Journal impact: score mapping, trusted list, block list, floor (before Claude and at selection)."""

import json

import httpx
import pytest
from fixtures.synthetic import REPORT_DATE, records

from research_agent.ingestion.http import HttpClient
from research_agent.ingestion.openalex import OpenAlexProvider
from research_agent.metadata.deduplication import ingest_record
from research_agent.retrieval.screening import prescreen, shortlist_and_enqueue
from research_agent.scoring.journal import (describe, fetch_metrics, floor_reason, impact_score, journal_context,
                                            missing_issns)
from research_agent.storage.repositories import load_paper

PARAMS = {"full_score_citedness": 15, "listed_journal_min_score": 85, "preprint_score": 40, "unknown_score": 30}


def j(c=None, listed=False, preprint=False, blocked=False):
    return {"preprint": preprint, "listed": listed, "blocked": blocked, "citedness": c, "h_index": None,
            "metric_source": None}


def test_score_mapping_is_smooth_and_capped():
    assert impact_score(j(17.0), PARAMS) == 100.0                  # e.g. Lancet Public Health
    s86, s26, s2 = impact_score(j(8.6), PARAMS), impact_score(j(2.6), PARAMS), impact_score(j(2.0), PARAMS)
    assert 100 > s86 > s26 > s2 > 0                                 # PLOS Med > J Med Econ > Prenat Diagn
    assert impact_score(j(1.5, listed=True), PARAMS) == 85.0        # trusted list: never below 85
    assert impact_score(j(preprint=True), PARAMS) == 40.0           # preprints: neutral-low
    assert impact_score(j(None), PARAMS) == 30.0                    # no metric available


@pytest.mark.parametrize("ctx,excluded", [
    (j(1.2), True),                     # unlisted, known, below floor 2.0
    (j(2.4), False),
    (j(1.2, listed=True), False),       # trusted list always passes
    (j(None), False),                   # unknown metric is not treated as low quality
    (j(preprint=True), False),          # preprints are scored, not floored
    (j(30.0, blocked=True), True),      # block list always wins
])
def test_floor(settings, ctx, excluded):
    assert (floor_reason(ctx, settings) is not None) == excluded


def test_floor_can_be_disabled(settings):
    s = settings.model_copy(deep=True)
    s.ranking.journal_floor["enabled"] = False
    assert floor_reason(j(0.5), s) is None and floor_reason(j(0.5, blocked=True), s) is not None


def _metric(db, issn, c):
    db.execute("INSERT OR REPLACE INTO journal_metrics(issn, found, display_name, two_year_mean_citedness, h_index,"
               " retrieved_at) VALUES (?,1,'J',?,10,'2099-01-01T00:00:00Z')", (issn, c))


def test_low_impact_journals_never_reach_claude(db, settings):
    ids = [ingest_record(db, r).paper_id for r in records()]
    prescreen(db, settings, ids)
    _metric(db, "0000-0000", 0.8)                                   # all synthetic papers share this ISSN
    st = shortlist_and_enqueue(db, settings, REPORT_DATE)
    assert st.enqueued == 0 and st.excluded_by_journal > 0
    _metric(db, "0000-0000", 6.0)
    st2 = shortlist_and_enqueue(db, settings, REPORT_DATE)
    assert st2.enqueued > 0


def test_listed_journal_context_and_description(db, settings):
    rec = records()[0].model_copy(update={"journal_issns": ["2468-2667"], "doi": "10.5555/listed"})
    pid = ingest_record(db, rec).paper_id
    _metric(db, "2468-2667", 17.0)
    ctx = journal_context(db, settings, load_paper(db, pid))
    assert ctx["listed"] and ctx["citedness"] == 17.0
    assert describe(ctx).startswith("OpenAlex 2-yr mean citedness 17.0") and "on your journal list" in describe(ctx)


def test_metric_lookup_caches_found_and_not_found(db, settings):
    def handler(req):
        if req.url.path.endswith("issn:1111-1111"):
            return httpx.Response(200, json={"id": "https://openalex.org/S1", "display_name": "Good J",
                                             "summary_stats": {"2yr_mean_citedness": 5.5, "h_index": 80},
                                             "is_in_doaj": False, "type": "journal"})
        return httpx.Response(404, json={"error": "not found"})
    oa = OpenAlexProvider(HttpClient(transport=httpx.MockTransport(handler), sleep=lambda s: None),
                          settings.sources.sources["openalex"])
    assert fetch_metrics(db, oa, missing_issns(db, {"1111-1111", "2222-2222"})) == 2
    rows = {r["issn"]: dict(r) for r in db.query("SELECT * FROM journal_metrics")}
    assert rows["1111-1111"]["two_year_mean_citedness"] == 5.5 and rows["1111-1111"]["openalex_source_id"] == "S1"
    assert rows["2222-2222"]["found"] == 0
    assert missing_issns(db, {"1111-1111", "2222-2222"}) == []     # cached (refreshed after 30 days)
