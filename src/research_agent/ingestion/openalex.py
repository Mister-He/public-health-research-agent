"""OpenAlex /works with title_and_abstract.search + publication-date windows (cursor paging).

`from_created_date` requires a paid plan (verified 2026-10-21), so windows are on
publication date with the standard overlap. OpenAlex `keywords`/`topics` are machine-generated:
they are stored as external_topics, never as author keywords. Abstracts are reconstructed from
the inverted index and rank last in abstract provenance."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

from research_agent.ingestion.base import BaseProvider, Page, as_list, map_all
from research_agent.ingestion.http import SchemaError
from research_agent.metadata.normalization import (normalize_doi, normalize_openalex, normalize_pmcid,
                                                   normalize_pmid, strip_markup)
from research_agent.metadata.schemas import SourceRecord
from research_agent.retrieval.query_builder import QuerySpec
from research_agent.timeutil import iso_utc, parse_date

SELECT = ("id,doi,title,publication_date,ids,primary_location,best_oa_location,type,authorships,"
          "abstract_inverted_index,topics,updated_date,created_date,is_retracted")


class OpenAlexProvider(BaseProvider):
    name = "openalex"

    def _params(self, **kw) -> dict:
        p = dict(kw)
        if self.api_key:
            p["api_key"] = self.api_key
        if self.email:
            p["mailto"] = self.email
        return p

    def works(self, filt: str, cursor: str = "*", per_page: int | None = None, select: str = SELECT) -> dict:
        """One page. With an API key: cursor paging. Without: OpenAlex refuses cursor paging past
        page 1 (HTTP 401, verified 2026-10-21), so page-number paging is used ("page:N" cursors)."""
        params = dict(filter=filt, per_page=per_page or self.cfg.page_size, select=select)
        if self.api_key:
            params["cursor"] = "*" if cursor.startswith("page:") else cursor
        else:
            params["page"] = int(cursor.split(":", 1)[1]) if cursor.startswith("page:") else 1
            params["sort"] = "publication_date:asc"
        data = self.http.get_json(self.name, f"{self.cfg.base_url}/works", params=self._params(**params))
        if not isinstance(data, dict) or "results" not in data or "meta" not in data:
            raise SchemaError(self.name, "missing results/meta")
        if not self.api_key:
            page = params["page"]
            more = page * params["per_page"] < int(data["meta"].get("count") or 0)
            data["meta"]["next_cursor"] = f"page:{page + 1}" if more and data["results"] else None
        return data

    def count(self, filt: str) -> int:
        data = self.http.get_json(self.name, f"{self.cfg.base_url}/works",
                                  params=self._params(filter=filt, per_page=1, select="id"))
        try:
            return int(data["meta"]["count"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SchemaError(self.name, "missing meta.count") from exc

    def fetch_window(self, spec: QuerySpec, start: date, end: date, cursor: str | None) -> Iterator[Page]:
        filt = (f"title_and_abstract.search:{spec.text},"
                f"from_publication_date:{start.isoformat()},to_publication_date:{end.isoformat()}")
        cur = cursor or "*"
        while True:
            data = self.works(filt, cur)
            total = int(data["meta"].get("count") or 0)
            if total > self.cfg.max_records_per_query_window:
                raise SchemaError(self.name, f"window {start}..{end} has {total} works; reduce chunk_days")
            results = data["results"]
            recs, warns = map_all(map_openalex, results, spec.query_id, spec.strategy)
            nxt = data["meta"].get("next_cursor")
            done = not results or not nxt
            yield Page(recs, None if done else nxt, raw_count=len(results), skipped=len(results) - len(recs),
                       warnings=warns)
            if done:
                return
            cur = nxt


def reconstruct_abstract(inv: dict | None) -> str | None:
    if not inv:
        return None
    pos: list[tuple[int, str]] = []
    for word, idxs in inv.items():
        for i in idxs:
            pos.append((i, word))
    return " ".join(w for _, w in sorted(pos)) or None


def map_openalex(w: dict, query_id: str, strategy: str) -> SourceRecord | None:
    ids = w.get("ids") or {}
    doi = normalize_doi(w.get("doi") or ids.get("doi"))
    oid = normalize_openalex(w.get("id"))
    if not (doi or oid):
        return None
    loc = w.get("primary_location") or {}
    src = loc.get("source") or {}
    typ = w.get("type")
    is_preprint = typ == "preprint" or (src.get("type") == "repository" and typ in ("article", "preprint", None))
    server = (src.get("display_name") or "repository").lower() if is_preprint else None
    pub = parse_date(w.get("publication_date"))
    return SourceRecord(
        provider="openalex", source_record_id=oid or doi, query_id=query_id, strategy=strategy, retrieved_at=iso_utc(),
        doi=doi, pmid=normalize_pmid((ids.get("pmid") or "").rsplit("/", 1)[-1] or None),
        pmcid=normalize_pmcid((ids.get("pmcid") or "").rstrip("/").rsplit("/", 1)[-1] or None),
        openalex_id=oid,
        title=strip_markup(w.get("title")),
        authors=[a.get("author", {}).get("display_name") for a in as_list(w.get("authorships"))
                 if a.get("author", {}).get("display_name")],
        journal=None if is_preprint else src.get("display_name"),
        journal_issns=sorted(set(as_list(src.get("issn")))),
        publisher=src.get("host_organization_name"),
        publication_type=typ,
        is_preprint=is_preprint, preprint_server=server,
        is_retracted=bool(w.get("is_retracted")),
        online_date=None if is_preprint else pub,
        preprint_posted_date=pub if is_preprint else None,
        modified_date=parse_date(w.get("updated_date")),
        abstract=reconstruct_abstract(w.get("abstract_inverted_index")),
        external_topics=[t.get("display_name") for t in as_list(w.get("topics"))[:5] if t.get("display_name")],
        source_url=loc.get("landing_page_url"),
        open_access_url=(w.get("best_oa_location") or {}).get("pdf_url") or (w.get("best_oa_location") or {}).get("landing_page_url"),
        license=loc.get("license"),
    )
