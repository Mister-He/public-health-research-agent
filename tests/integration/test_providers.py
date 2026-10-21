"""Provider adapters against RECORDED real responses (tests/fixtures/recorded, captured 2026-10-22)
and mocked failure modes. No live network."""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest
from conftest import recorded

from research_agent.ingestion.crossref import CrossrefProvider, map_crossref
from research_agent.ingestion.europe_pmc import EuropePMCProvider
from research_agent.ingestion.http import HttpClient, ProviderError, SchemaError
from research_agent.ingestion.medrxiv import MedRxivProvider
from research_agent.ingestion.openalex import OpenAlexProvider, reconstruct_abstract
from research_agent.ingestion.pubmed import PubMedProvider, parse_pubmed_xml
from research_agent.retrieval.query_builder import QuerySpec, build_queries, europe_pmc_query, openalex_query, pubmed_query

W = (date(2026, 10, 14), date(2026, 10, 20))


def client(handler, **kw) -> HttpClient:
    return HttpClient(transport=httpx.MockTransport(handler), sleep=lambda s: None, backoff_base=0.01, **kw)


def provider(cls, settings, handler, **kw):
    name = {PubMedProvider: "pubmed", EuropePMCProvider: "europe_pmc", OpenAlexProvider: "openalex",
            CrossrefProvider: "crossref", MedRxivProvider: "medrxiv"}[cls]
    return cls(client(handler), settings.sources.sources[name], contact_email="t@example.org", **kw)


def pages(p, spec, cursor=None):
    return list(p.fetch_window(spec, *W, cursor))


# ------------------------------------------------------------------ successful retrieval (recorded)

def test_pubmed_recorded(settings):
    ids = json.loads(recorded("pubmed_esearch.json"))["esearchresult"]["idlist"]

    def h(req: httpx.Request):
        if "esearch" in req.url.path:
            return httpx.Response(200, json={"esearchresult": {"count": "3", "idlist": ids}})
        assert req.url.params["tool"] == "research-agent" and req.url.params["email"] == "t@example.org"
        return httpx.Response(200, text=recorded("pubmed_efetch.xml"))
    ps = pages(provider(PubMedProvider, settings, h), QuerySpec("pubmed", "topic:x", "topic", "x"))
    recs = [r for p in ps for r in p.records]
    assert len(recs) == 3 and all(r.pmid and r.title for r in recs)
    assert all(r.indexed_date for r in recs)          # EDAT captured separately from publication dates
    assert ps[-1].next_cursor is None


def test_pubmed_parser_fields():
    recs = parse_pubmed_xml(recorded("pubmed_efetch.xml"), query_id="q", strategy="topic")
    r = recs[0]
    assert r.provider == "pubmed" and r.source_url.startswith("https://pubmed.ncbi.nlm.nih.gov/")
    assert isinstance(r.mesh_terms, list) and isinstance(r.author_keywords, list)
    with pytest.raises(SchemaError):
        parse_pubmed_xml("<not-xml", query_id="q", strategy="topic")


def test_europe_pmc_recorded_and_pagination(settings):
    data = json.loads(recorded("europepmc_search.json"))
    calls = []

    def h(req):
        calls.append(req.url.params["cursorMark"])
        if req.url.params["cursorMark"] == "*":
            return httpx.Response(200, json={**data, "hitCount": 4, "nextCursorMark": "NEXT"})
        return httpx.Response(200, json={**data, "hitCount": 4, "nextCursorMark": "NEXT2"})
    ps = pages(provider(EuropePMCProvider, settings, h), QuerySpec("europe_pmc", "topic:x", "topic", "x"))
    assert calls == ["*", "NEXT"] and len(ps) == 2 and ps[0].next_cursor == "NEXT" and ps[-1].next_cursor is None
    assert all(r.indexed_date for p in ps for r in p.records)


def test_openalex_recorded_keyless_paging(settings):
    data = json.loads(recorded("openalex_works.json"))
    seen = []

    def h(req):
        seen.append(dict(req.url.params))
        page = int(req.url.params["page"])
        return httpx.Response(200, json={"meta": {"count": 250}, "results": data["results"] if page == 1 else data["results"][:1]})
    ps = pages(provider(OpenAlexProvider, settings, h), QuerySpec("openalex", "topic:x", "topic", '"cost-effectiveness"'))
    assert [s["page"] for s in seen] == ["1", "2"] and "cursor" not in seen[0]
    r = ps[0].records[0]
    assert r.openalex_id and r.author_keywords == []          # OpenAlex topics never become author keywords
    assert isinstance(r.external_topics, list)


def test_openalex_cursor_paging_with_key(settings):
    data = json.loads(recorded("openalex_works.json"))

    def h(req):
        assert req.url.params["api_key"] == "k" and "page" not in req.url.params
        nxt = "C2" if req.url.params["cursor"] == "*" else None
        return httpx.Response(200, json={"meta": {"count": 4, "next_cursor": nxt}, "results": data["results"]})
    ps = pages(provider(OpenAlexProvider, settings, h, api_key="k"), QuerySpec("openalex", "topic:x", "topic", "x"))
    assert len(ps) == 2


def test_openalex_abstract_reconstruction():
    assert reconstruct_abstract({"world": [1], "Hello": [0], "again": [2]}) == "Hello world again"
    assert reconstruct_abstract(None) is None


def test_crossref_recorded_and_journal_name_warning(settings):
    data = json.loads(recorded("crossref_works.json"))

    def h(req):
        f = req.url.params["filter"]
        assert "issn:2468-2667" in f and "from-created-date:2026-10-14" in f
        return httpx.Response(200, json=data)
    spec = QuerySpec("crossref", "journal:lph", "journal", "2468-2667",
                     params={"issns": ("2468-2667",), "name": "A Different Journal Name"})
    ps = pages(provider(CrossrefProvider, settings, h), spec)
    assert ps[0].records and all(r.doi for r in ps[0].records)
    assert any("journal name mismatch" in w for w in ps[0].warnings)


def test_crossref_doi_lookup_and_nonexistent(settings):
    good = json.loads(recorded("crossref_doi.json"))

    def h(req):
        if "definitely-not" in req.url.path:
            return httpx.Response(404, text="Resource not found.")
        return httpx.Response(200, json=good)
    p = provider(CrossrefProvider, settings, h)
    rec = p.lookup_doi(good["message"]["DOI"].lower())
    assert rec is not None and rec.title
    assert p.lookup_doi("10.5555/definitely-not-registered") is None


def test_crossref_missing_abstract_and_retraction():
    item = json.loads(recorded("crossref_doi.json"))["message"]
    item = {**item, "abstract": None, "updated-by": [{"type": "retraction", "DOI": "10.5555/r"}]}
    r = map_crossref(item, "q", "journal")
    assert r.abstract is None and r.is_retracted


def test_medrxiv_versions_and_published_link(settings):
    data = json.loads(recorded("medrxiv_details.json"))

    def h(req):
        parts = req.url.path.split("/")
        if len(parts) == 5 or (len(parts) == 6 and "." in parts[-1] and "-" not in parts[-1]):   # /details/medrxiv/<doi>
            doi = "/".join(req.url.path.split("/")[3:])
            return httpx.Response(200, json={"messages": [{"status": "ok"}], "collection": [
                {**x, "version": "1", "date": "2026-09-01"} for x in data["collection"] if x["doi"] == doi][:1]})
        return httpx.Response(200, json=data)
    ps = pages(provider(MedRxivProvider, settings, h), QuerySpec("medrxiv", "window:medrxiv", "window", ""))
    recs = ps[0].records
    assert all(r.is_preprint and r.preprint_version for r in recs)
    v2 = [r for r in recs if r.preprint_version == 2]
    assert v2 and v2[0].source_record_id.endswith("v2") and v2[0].preprint_revision_date
    published = [r for r in recs if r.published_doi]
    assert published and published[0].published_doi.startswith("10.")
    assert any(r.doi.startswith("10.64898/") for r in recs)   # new medRxiv DOI prefix handled


# ------------------------------------------------------------------ failure modes

def test_empty_results(settings):
    h = lambda req: httpx.Response(200, json={"hitCount": 0, "resultList": {"result": []}, "nextCursorMark": "*"})  # noqa: E731
    ps = pages(provider(EuropePMCProvider, settings, h), QuerySpec("europe_pmc", "q", "topic", "x"))
    assert len(ps) == 1 and ps[0].records == [] and ps[0].next_cursor is None


def test_malformed_record_skipped_with_warning(settings):
    data = json.loads(recorded("europepmc_search.json"))
    bad = dict(data["resultList"]["result"][0])
    bad["doi"] = "10.5555/ok"
    bad["firstIndexDate"] = 12345            # wrong type -> skipped, rest of the page kept
    bad["authorList"] = "not-a-dict"
    payload = {**data, "hitCount": 3, "resultList": {"result": [bad] + data["resultList"]["result"]}}
    ps = pages(provider(EuropePMCProvider, settings, lambda r: httpx.Response(200, json=payload)),
               QuerySpec("europe_pmc", "q", "topic", "x"))
    assert len(ps[0].records) == 2 and ps[0].warnings


def test_schema_violation_raises(settings):
    p = provider(OpenAlexProvider, settings, lambda r: httpx.Response(200, json={"unexpected": True}))
    with pytest.raises(SchemaError):
        pages(p, QuerySpec("openalex", "q", "topic", "x"))


def test_429_with_retry_after_then_success(settings):
    calls = {"n": 0}
    slept = []

    def h(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json={"meta": {"count": 0}, "results": []})
    c = HttpClient(transport=httpx.MockTransport(h), sleep=slept.append)
    p = OpenAlexProvider(c, settings.sources.sources["openalex"])
    pages(p, QuerySpec("openalex", "q", "topic", "x"))
    assert calls["n"] == 2 and 7.0 in slept
    assert c.usage["openalex"].rate_limited == 1 and c.usage["openalex"].retries == 1


def test_500_retries_then_gives_up(settings):
    c = client(lambda r: httpx.Response(500), max_retries=2)
    p = CrossrefProvider(c, settings.sources.sources["crossref"])
    with pytest.raises(ProviderError) as ei:
        pages(p, QuerySpec("crossref", "j", "journal", "0000-0000", params={"issns": ("0000-0000",)}))
    assert ei.value.status == 500 and c.usage["crossref"].requests == 3 and c.usage["crossref"].failures == 1


def test_timeout_retried(settings):
    n = {"i": 0}

    def h(req):
        n["i"] += 1
        if n["i"] < 3:
            raise httpx.ReadTimeout("slow", request=req)
        return httpx.Response(200, json={"messages": [{"status": "ok", "total": "0"}], "collection": []})
    p = MedRxivProvider(client(h), settings.sources.sources["medrxiv"])
    ps = pages(p, QuerySpec("medrxiv", "w", "window", ""))
    assert n["i"] == 3 and ps[-1].next_cursor is None


def test_400_is_not_retried(settings):
    c = client(lambda r: httpx.Response(400, json={"error": "bad"}))
    with pytest.raises(ProviderError) as ei:
        c.get_json("pubmed", "https://eutils.ncbi.nlm.nih.gov/x?api_key=SECRET")
    assert not ei.value.retryable and "SECRET" not in str(ei.value)


def test_rate_limiter_spacing():
    t = {"now": 0.0}
    slept = []

    def sleep(s):
        slept.append(s)
        t["now"] += s
    c = HttpClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})), sleep=sleep,
                   clock=lambda: t["now"])
    c.set_rate("p", 1.0)
    for _ in range(3):
        c.get_json("p", "https://example.org/")
    assert len(slept) == 2 and all(abs(s - 1.0) < 1e-9 for s in slept)


def test_query_builder_syntax(settings):
    g = next(g for g in settings.queries.groups if g.id == "biostatistics_inference")
    assert '"Bayesian calibration"[tiab]' in pubmed_query(g) and "epidemiolog*[tiab]" in pubmed_query(g)
    assert 'TITLE_ABS:"Bayesian calibration"' in europe_pmc_query(g)
    assert '"Bayesian calibration"' in openalex_query(g) and "*" not in openalex_query(g)
    specs = build_queries(settings)
    assert {s.provider for s in specs} >= {"pubmed", "europe_pmc", "openalex", "crossref", "medrxiv"}
    assert len({(s.provider, s.query_id) for s in specs}) == len(specs)
    assert {s.strategy for s in specs} >= {"topic", "journal", "window"}


def test_long_retry_after_fails_fast(settings):
    slept = []
    c = HttpClient(transport=httpx.MockTransport(lambda r: httpx.Response(429, headers={"Retry-After": "25511"})),
                   sleep=slept.append)
    with pytest.raises(ProviderError) as ei:
        c.get_json("openalex", "https://api.openalex.org/works")
    assert ei.value.retryable and "25511" in str(ei.value) and slept == [] and c.usage["openalex"].requests == 1
