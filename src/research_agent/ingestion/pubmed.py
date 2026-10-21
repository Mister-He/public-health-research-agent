"""PubMed via NCBI E-utilities (esearch + efetch XML). Windows use the Entrez date (EDAT),
i.e. when a record entered PubMed, so delayed indexing is not missed."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterator
from datetime import date

from research_agent.ingestion.base import BaseProvider, Page
from research_agent.ingestion.http import SchemaError
from research_agent.metadata.normalization import normalize_doi, normalize_pmcid, normalize_pmid, strip_markup
from research_agent.metadata.schemas import SourceRecord
from research_agent.retrieval.query_builder import QuerySpec
from research_agent.timeutil import iso_utc

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


class PubMedProvider(BaseProvider):
    name = "pubmed"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        if self.api_key:  # documented: 10 req/s with a key
            self.http.set_rate(self.name, min(self.cfg.min_interval_seconds, 0.11))

    def _common(self) -> dict:
        p = {"tool": "research-agent", "db": "pubmed"}
        if self.email:
            p["email"] = self.email
        if self.api_key:
            p["api_key"] = self.api_key
        return p

    def search_ids(self, term: str, start: date, end: date) -> list[str]:
        ids: list[str] = []
        retstart = 0
        page = int(self.cfg.page_size)
        limit = int(self.cfg.max_records_per_query_window)
        while True:
            data = self.http.get_json(self.name, f"{self.cfg.base_url}/esearch.fcgi", params={
                **self._common(), "term": term, "datetype": self.extra("date_type", "edat"),
                "mindate": start.strftime("%Y/%m/%d"), "maxdate": end.strftime("%Y/%m/%d"),
                "retmode": "json", "retmax": page, "retstart": retstart, "sort": "pub_date"})
            try:
                res = data["esearchresult"]
                count = int(res["count"])
                batch = list(res["idlist"])
            except (KeyError, TypeError, ValueError) as exc:
                if isinstance(data, dict) and "esearchresult" in data and "ERROR" in data["esearchresult"]:
                    raise SchemaError(self.name, str(data["esearchresult"]["ERROR"])) from exc
                raise SchemaError(self.name, f"esearch: {exc}") from exc
            ids.extend(batch)
            retstart += len(batch)
            if not batch or retstart >= count or retstart >= limit:
                break
        if count > limit:
            raise SchemaError(self.name, f"window {start}..{end} has {count} records (> limit {limit}); "
                                         "reduce chunk_days")
        return sorted(set(ids), key=int)

    def fetch_pmids(self, pmids: list[str], spec_id: str = "enrichment", strategy: str = "enrichment") -> list[SourceRecord]:
        if not pmids:
            return []
        xml = self.http.get_text(self.name, f"{self.cfg.base_url}/efetch.fcgi", params={
            **self._common(), "id": ",".join(pmids), "retmode": "xml"})
        return parse_pubmed_xml(xml, query_id=spec_id, strategy=strategy)

    def fetch_window(self, spec: QuerySpec, start: date, end: date, cursor: str | None) -> Iterator[Page]:
        ids = self.search_ids(spec.text, start, end)
        pos = int(cursor or 0)
        bs = int(self.extra("fetch_batch_size", 200))
        if pos >= len(ids):
            yield Page([], None, 0)
            return
        while pos < len(ids):
            chunk = ids[pos:pos + bs]
            recs = self.fetch_pmids(chunk, spec.query_id, spec.strategy)
            pos += len(chunk)
            yield Page(recs, str(pos) if pos < len(ids) else None, raw_count=len(chunk),
                       skipped=len(chunk) - len(recs))

    def find_by_doi(self, doi: str) -> list[SourceRecord]:
        data = self.http.get_json(self.name, f"{self.cfg.base_url}/esearch.fcgi", params={
            **self._common(), "term": f"{doi}[aid]", "retmode": "json", "retmax": 5})
        ids = data.get("esearchresult", {}).get("idlist", [])
        return [r for r in self.fetch_pmids(ids) if r.doi == doi]


def _text(el: ET.Element | None) -> str | None:
    if el is None:
        return None
    return strip_markup("".join(el.itertext()))


def _date(el: ET.Element | None) -> tuple[date | None, str]:
    if el is None:
        return None, "none"
    y = el.findtext("Year")
    if not y or not y.isdigit():
        md = el.findtext("MedlineDate") or ""
        return (date(int(md[:4]), 1, 1), "year") if md[:4].isdigit() else (None, "none")
    m_raw = (el.findtext("Month") or "").strip().lower()
    m = int(m_raw) if m_raw.isdigit() else _MONTHS.get(m_raw[:3])
    if not m:
        return date(int(y), 1, 1), "year"
    d_raw = el.findtext("Day")
    if not d_raw or not d_raw.isdigit():
        return date(int(y), m, 1), "month"
    try:
        return date(int(y), m, int(d_raw)), "day"
    except ValueError:
        return date(int(y), m, 1), "month"


def parse_pubmed_xml(xml: str, *, query_id: str, strategy: str) -> list[SourceRecord]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise SchemaError("pubmed", f"efetch XML parse error: {exc}") from exc
    if root.tag != "PubmedArticleSet":
        raise SchemaError("pubmed", f"unexpected root {root.tag}")
    out = []
    now = iso_utc()
    for art in root.findall("PubmedArticle"):
        mc = art.find("MedlineCitation")
        a = mc.find("Article") if mc is not None else None
        if mc is None or a is None:
            continue
        pmid = normalize_pmid(mc.findtext("PMID"))
        ids = {i.get("IdType"): (i.text or "").strip() for i in art.findall("PubmedData/ArticleIdList/ArticleId")}
        doi = normalize_doi(ids.get("doi")) or normalize_doi(
            next((e.text for e in a.findall("ELocationID") if e.get("EIdType") == "doi"), None))
        if not pmid:
            continue
        abstract_parts = []
        for at in a.findall("Abstract/AbstractText"):
            txt = _text(at)
            if txt:
                label = at.get("Label")
                abstract_parts.append(f"{label}: {txt}" if label else txt)
        authors = []
        for au in a.findall("AuthorList/Author"):
            if au.findtext("CollectiveName"):
                authors.append(au.findtext("CollectiveName").strip())
            elif au.findtext("LastName"):
                fore = au.findtext("ForeName") or au.findtext("Initials") or ""
                authors.append(f"{au.findtext('LastName').strip()}, {fore.strip()}".strip(", "))
        ptypes = [t.text for t in a.findall("PublicationTypeList/PublicationType") if t.text]
        online, online_p = _date(a.find("ArticleDate[@DateType='Electronic']"))
        printed, print_p = _date(a.find("Journal/JournalIssue/PubDate"))
        entrez, _ = _date(art.find("PubmedData/History/PubMedPubDate[@PubStatus='entrez']"))
        revised, _ = _date(mc.find("DateRevised"))
        issns = sorted({(i.text or "").strip() for i in a.findall("Journal/ISSN") if i.text})
        is_preprint = "Preprint" in ptypes
        out.append(SourceRecord(
            provider="pubmed", source_record_id=pmid, query_id=query_id, strategy=strategy, retrieved_at=now,
            doi=doi, pmid=pmid, pmcid=normalize_pmcid(ids.get("pmc")),
            title=_text(a.find("ArticleTitle")),
            authors=authors,
            journal=_text(a.find("Journal/Title")),
            journal_issns=issns,
            publication_type="; ".join(ptypes) or None,
            is_preprint=is_preprint,
            preprint_server="pubmed_preprint" if is_preprint else None,
            is_retracted="Retracted Publication" in ptypes,
            online_date=online, print_date=printed, indexed_date=entrez, modified_date=revised,
            date_precision={"online_date": online_p, "print_date": print_p},
            abstract=" ".join(abstract_parts) or None,
            author_keywords=[k for k in (_text(k) for k in mc.findall("KeywordList/Keyword")) if k],
            mesh_terms=[d.text.strip() for d in mc.findall("MeshHeadingList/MeshHeading/DescriptorName") if d.text],
            source_url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        ))
    return out
