"""Crossref REST /works — journal-driven discovery by ISSN, plus DOI lookups for validation.

Windows use `created` (first DOI registration). `indexed` changes whenever Crossref re-indexes a
record (verified: a 2016 article appears under a 2026 index window) so it is not used."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import date
from urllib.parse import quote

from research_agent.ingestion.base import BaseProvider, Page, as_list, map_all
from research_agent.ingestion.http import ProviderError, SchemaError, WindowTooLarge
from research_agent.metadata.normalization import normalize_doi, normalize_title, strip_markup
from research_agent.metadata.schemas import SourceRecord
from research_agent.retrieval.query_builder import QuerySpec
from research_agent.timeutil import iso_utc, parse_partial_date

log = logging.getLogger(__name__)
KEEP_TYPES = {"journal-article", "posted-content", "proceedings-article", "report"}


class CrossrefProvider(BaseProvider):
    name = "crossref"

    def _params(self, **kw) -> dict:
        if self.email:
            kw["mailto"] = self.email
        return kw

    def fetch_window(self, spec: QuerySpec, start: date, end: date, cursor: str | None) -> Iterator[Page]:
        issns = spec.params.get("issns") or tuple(spec.text.split(","))
        filt = ",".join([f"issn:{i}" for i in issns] +
                        [f"from-created-date:{start.isoformat()}", f"until-created-date:{end.isoformat()}"])
        cur = cursor or "*"
        seen = 0
        expected_name = normalize_title(spec.params.get("name"))
        while True:
            data = self.http.get_json(self.name, f"{self.cfg.base_url}/works", params=self._params(
                filter=filt, rows=self.cfg.page_size, cursor=cur))
            try:
                msg = data["message"]
                items = msg["items"]
                total = int(msg["total-results"])
            except (KeyError, TypeError, ValueError) as exc:
                raise SchemaError(self.name, f"works: {exc}") from exc
            if total > self.cfg.max_records_per_query_window:
                raise WindowTooLarge(self.name, total, self.cfg.max_records_per_query_window)
            recs, warnings = map_all(map_crossref, items, spec.query_id, spec.strategy)
            for r in recs:
                if expected_name and r.journal and normalize_title(r.journal) != expected_name:
                    warnings.append(f"journal name mismatch for {r.doi}: {r.journal!r}")
            seen += len(items)
            nxt = msg.get("next-cursor")
            done = not items or not nxt or seen >= total
            yield Page(recs, None if done else nxt, raw_count=len(items), skipped=len(items) - len(recs),
                       warnings=warnings)
            if done:
                return
            cur = nxt

    def lookup_doi(self, doi: str) -> SourceRecord | None:
        """Authoritative DOI metadata (None if the DOI is not registered with Crossref)."""
        try:
            data = self.http.get_json(self.name, f"{self.cfg.base_url}/works/{quote(doi, safe='/')}",
                                      params=self._params(), retry_statuses={429, 500, 502, 503, 504})
        except ProviderError as exc:
            if exc.status == 404:
                return None
            raise
        try:
            return map_crossref(data["message"], "validation", "enrichment", keep_all_types=True)
        except (KeyError, TypeError) as exc:
            raise SchemaError(self.name, f"works/{doi}: {exc}") from exc


def _dp(obj: dict | None) -> tuple[date | None, str]:
    if not obj:
        return None, "none"
    return parse_partial_date((obj.get("date-parts") or [[None]])[0])


def map_crossref(it: dict, query_id: str, strategy: str, keep_all_types: bool = False) -> SourceRecord | None:
    doi = normalize_doi(it.get("DOI"))
    typ = it.get("type")
    if not doi or (not keep_all_types and typ not in KEEP_TYPES):
        return None
    authors = []
    for a in as_list(it.get("author")):
        if a.get("family"):
            authors.append(f"{a['family']}, {a.get('given', '')}".strip(", "))
        elif a.get("name"):
            authors.append(a["name"])
    online, op = _dp(it.get("published-online"))
    printed, pp = _dp(it.get("published-print"))
    if online is None and printed is None:
        # `issued` is the earliest of online/print when neither is given separately.
        online, op = _dp(it.get("issued"))
    posted, _ = _dp(it.get("posted"))
    created, _ = _dp(it.get("created"))
    deposited, _ = _dp(it.get("deposited"))
    is_preprint = typ == "posted-content" and it.get("subtype") == "preprint"
    rel = it.get("relation") or {}
    published = None
    if is_preprint:
        for r in as_list(rel.get("is-preprint-of")):
            if r.get("id-type") == "doi":
                published = normalize_doi(r.get("id"))
                break
    licenses = as_list(it.get("license"))
    lic = next((l.get("URL") for l in licenses if l.get("content-version") == "vor"), None) or \
        (licenses[0].get("URL") if licenses else None)
    # The original article carries `updated-by` pointing at its retraction notice.
    retracted = any(u.get("type") in ("retraction", "removal", "withdrawal") for u in as_list(it.get("updated-by")))
    titles = as_list(it.get("title"))
    return SourceRecord(
        provider="crossref", source_record_id=doi, query_id=query_id, strategy=strategy, retrieved_at=iso_utc(),
        doi=doi,
        title=strip_markup(titles[0]) if titles else None,
        authors=authors,
        journal=None if is_preprint else (as_list(it.get("container-title")) or [None])[0],
        journal_issns=sorted(set(as_list(it.get("ISSN")))),
        publisher=it.get("publisher"),
        publication_type=typ,
        is_preprint=is_preprint,
        preprint_server=(it.get("institution") or [{}])[0].get("name", "preprint").lower() if is_preprint else None,
        published_doi=published,
        is_retracted=retracted,
        online_date=None if is_preprint else online,
        print_date=None if is_preprint else printed,
        preprint_posted_date=posted if is_preprint else None,
        indexed_date=created,
        modified_date=deposited,
        date_precision={"online_date": op, "print_date": pp},
        abstract=strip_markup(it.get("abstract")),
        source_url=it.get("URL"),
        license=lic,
    )
