"""Europe PMC REST search (resultType=core, cursorMark pagination). Windows use FIRST_IDATE
(first index date). Covers MEDLINE, PMC and preprints (SRC:PPR)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

from research_agent.ingestion.base import BaseProvider, Page, as_list, map_all
from research_agent.ingestion.http import SchemaError, WindowTooLarge
from research_agent.metadata.normalization import normalize_doi, normalize_pmcid, normalize_pmid, strip_markup
from research_agent.metadata.schemas import SourceRecord
from research_agent.retrieval.query_builder import QuerySpec
from research_agent.timeutil import iso_utc, parse_date


class EuropePMCProvider(BaseProvider):
    name = "europe_pmc"

    def search(self, query: str, cursor: str = "*", page_size: int | None = None) -> dict:
        data = self.http.get_json(self.name, f"{self.cfg.base_url}/search", params={
            "query": query, "format": "json", "resultType": "core",
            "pageSize": page_size or self.cfg.page_size, "cursorMark": cursor, "sort": "FIRST_IDATE_D asc"})
        if not isinstance(data, dict) or "resultList" not in data or "hitCount" not in data:
            raise SchemaError(self.name, "missing resultList/hitCount")
        return data

    def fetch_window(self, spec: QuerySpec, start: date, end: date, cursor: str | None) -> Iterator[Page]:
        q = f"({spec.text}) AND FIRST_IDATE:[{start.isoformat()} TO {end.isoformat()}]"
        cur = cursor or "*"
        seen = 0
        while True:
            data = self.search(q, cur)
            if int(data["hitCount"]) > self.cfg.max_records_per_query_window:
                raise WindowTooLarge(self.name, int(data["hitCount"]), self.cfg.max_records_per_query_window)
            results = as_list(data["resultList"].get("result"))
            recs, warns = map_all(map_epmc, results, spec.query_id, spec.strategy)
            seen += len(results)
            nxt = data.get("nextCursorMark")
            done = not results or nxt in (None, cur) or seen >= int(data["hitCount"])
            yield Page(recs, None if done else nxt, raw_count=len(results), skipped=len(results) - len(recs),
                       warnings=warns)
            if done:
                return
            cur = nxt

    def find_by_doi(self, doi: str) -> list[SourceRecord]:
        data = self.search(f'DOI:"{doi}"', page_size=5)
        return [r for r in (map_epmc(x, "enrichment", "enrichment") for x in as_list(data["resultList"].get("result")))
                if r and r.doi == doi]


def map_epmc(x: dict, query_id: str, strategy: str) -> SourceRecord | None:
    src = x.get("source")
    doi = normalize_doi(x.get("doi"))
    pmid = normalize_pmid(x.get("pmid"))
    pmcid = normalize_pmcid(x.get("pmcid"))
    if not (doi or pmid or pmcid):
        return None
    ji = x.get("journalInfo") or {}
    jr = ji.get("journal") or {}
    authors = []
    for a in as_list((x.get("authorList") or {}).get("author")):
        if a.get("lastName"):
            authors.append(f"{a['lastName']}, {a.get('firstName') or a.get('initials') or ''}".strip(", "))
        elif a.get("collectiveName"):
            authors.append(a["collectiveName"])
        elif a.get("fullName"):
            authors.append(a["fullName"])
    ptypes = as_list((x.get("pubTypeList") or {}).get("pubType"))
    is_preprint = src == "PPR" or "Preprint" in ptypes or "preprint" in ptypes
    server = None
    if is_preprint:
        server = ((x.get("bookOrReportDetails") or {}).get("publisher") or "preprint").lower()
    mesh = [m.get("descriptorName") for m in as_list((x.get("meshHeadingList") or {}).get("meshHeading"))
            if m.get("descriptorName")]
    urls = as_list((x.get("fullTextUrlList") or {}).get("fullTextUrl"))
    oa = next((u.get("url") for u in urls if u.get("availabilityCode") in ("OA", "F")), None)
    return SourceRecord(
        provider="europe_pmc", source_record_id=f"{src}:{x.get('id')}", query_id=query_id, strategy=strategy,
        retrieved_at=iso_utc(), doi=doi, pmid=pmid, pmcid=pmcid,
        title=strip_markup(x.get("title")),
        authors=authors,
        journal=None if is_preprint else jr.get("title"),
        journal_issns=sorted({i for i in (jr.get("issn"), jr.get("essn")) if i}),
        publication_type="; ".join(ptypes) or None,
        is_preprint=is_preprint, preprint_server=server,
        is_retracted="Retracted Publication" in ptypes,
        online_date=parse_date(x.get("electronicPublicationDate")),
        print_date=parse_date(ji.get("printPublicationDate")),
        preprint_posted_date=parse_date(x.get("firstPublicationDate")) if is_preprint else None,
        indexed_date=parse_date(x.get("firstIndexDate")),
        modified_date=parse_date(x.get("dateOfRevision")),
        abstract=strip_markup(x.get("abstractText")),
        author_keywords=[k for k in as_list((x.get("keywordList") or {}).get("keyword")) if isinstance(k, str)],
        mesh_terms=mesh,
        source_url=f"https://europepmc.org/article/{src}/{x.get('id')}",
        open_access_url=oa,
        license=x.get("license"),
    )
