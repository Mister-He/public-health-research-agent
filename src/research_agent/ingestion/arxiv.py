"""Optional arXiv adapter (Atom API) for computational-methods discovery."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterator
from datetime import date

from research_agent.ingestion.base import BaseProvider, Page
from research_agent.ingestion.http import SchemaError
from research_agent.metadata.normalization import normalize_arxiv, normalize_doi, strip_markup
from research_agent.metadata.schemas import SourceRecord
from research_agent.retrieval.query_builder import QuerySpec
from research_agent.timeutil import iso_utc, parse_date

NS = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom",
      "os": "http://a9.com/-/spec/opensearch/1.1/"}


class ArxivProvider(BaseProvider):
    name = "arxiv"

    def fetch_window(self, spec: QuerySpec, start: date, end: date, cursor: str | None) -> Iterator[Page]:
        pos = int(cursor or 0)
        q = f"({spec.text}) AND submittedDate:[{start:%Y%m%d}0000 TO {end:%Y%m%d}2359]"
        while True:
            xml = self.http.get_text(self.name, self.cfg.base_url, params={
                "search_query": q, "start": pos, "max_results": self.cfg.page_size,
                "sortBy": "submittedDate", "sortOrder": "ascending"})
            recs, total, n = parse_arxiv(xml, spec.query_id, spec.strategy)
            pos += n
            done = n == 0 or pos >= total or pos >= self.cfg.max_records_per_query_window
            yield Page(recs, None if done else str(pos), raw_count=n, skipped=n - len(recs))
            if done:
                return


def parse_arxiv(xml: str, query_id: str, strategy: str) -> tuple[list[SourceRecord], int, int]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise SchemaError("arxiv", f"Atom parse error: {exc}") from exc
    total = int(root.findtext("os:totalResults", "0", NS))
    out = []
    entries = root.findall("a:entry", NS)
    for e in entries:
        aid, ver = normalize_arxiv(e.findtext("a:id", "", NS))
        if not aid:
            continue
        cats = [c.get("term") for c in e.findall("a:category", NS) if c.get("term")]
        out.append(SourceRecord(
            provider="arxiv", source_record_id=f"{aid}v{ver or 1}", query_id=query_id, strategy=strategy,
            retrieved_at=iso_utc(), arxiv_id=aid, doi=normalize_doi(e.findtext("arxiv:doi", None, NS)),
            preprint_server="arxiv", preprint_version=ver or 1, is_preprint=True,
            title=strip_markup(e.findtext("a:title", None, NS)),
            authors=[a.findtext("a:name", "", NS).strip() for a in e.findall("a:author", NS)],
            publication_type="preprint",
            preprint_posted_date=parse_date(e.findtext("a:published", None, NS)),
            preprint_revision_date=parse_date(e.findtext("a:updated", None, NS)),
            indexed_date=parse_date(e.findtext("a:published", None, NS)),
            abstract=strip_markup(e.findtext("a:summary", None, NS)),
            external_topics=cats,
            source_url=f"https://arxiv.org/abs/{aid}v{ver or 1}",
            open_access_url=f"https://arxiv.org/pdf/{aid}v{ver or 1}",
        ))
    return out, total, len(entries)
