"""medRxiv via api.medrxiv.org /details (date-range windows, 100 records per page).

There is no server-side search, so the whole window is ingested and screened locally.
Each version is a separate source record (source_record_id = DOI + 'v' + version); the canonical
paper tracks the latest version. medRxiv DOIs use 10.1101/ and (since 2026) 10.64898/."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

from research_agent.ingestion.base import BaseProvider, Page, map_all
from research_agent.ingestion.http import SchemaError
from research_agent.metadata.normalization import normalize_doi, strip_markup
from research_agent.metadata.schemas import SourceRecord
from research_agent.retrieval.query_builder import QuerySpec
from research_agent.timeutil import iso_utc, parse_date

LICENSES = {"cc_by": "CC BY", "cc_by_nc": "CC BY-NC", "cc_by_nd": "CC BY-ND", "cc_by_nc_nd": "CC BY-NC-ND",
            "cc0": "CC0", "cc_no": "No reuse without permission"}


class MedRxivProvider(BaseProvider):
    name = "medrxiv"
    server = "medrxiv"

    def _details(self, path: str) -> dict:
        data = self.http.get_json(self.name, f"{self.cfg.base_url}/details/{self.server}/{path}")
        if not isinstance(data, dict) or "messages" not in data:
            raise SchemaError(self.name, "missing messages")
        return data

    def fetch_window(self, spec: QuerySpec, start: date, end: date, cursor: str | None) -> Iterator[Page]:
        pos = int(cursor or 0)
        while True:
            data = self._details(f"{start.isoformat()}/{end.isoformat()}/{pos}")
            msg = (data.get("messages") or [{}])[0]
            if msg.get("status") != "ok":
                # "no posts found" is a valid empty window
                if "no posts" in str(msg.get("status", "")).lower():
                    yield Page([], None)
                    return
                raise SchemaError(self.name, f"status {msg.get('status')!r}")
            coll = data.get("collection") or []
            total = int(msg.get("total") or 0)
            if total > self.cfg.max_records_per_query_window:
                raise SchemaError(self.name, f"window has {total} records; reduce chunk_days")
            # First-version dates of revisions are looked up later, only for relevant papers
            # (pipeline.runner.fill_preprint_first_dates).
            recs, warns = map_all(map_medrxiv, coll, spec.query_id, spec.strategy, self.server)
            pos += len(coll)
            done = not coll or pos >= total
            yield Page(recs, None if done else str(pos), raw_count=len(coll), skipped=len(coll) - len(recs),
                       warnings=warns)
            if done:
                return

    def versions(self, doi: str) -> list[dict]:
        return self._details(doi).get("collection") or []

    def lookup(self, doi: str) -> list[SourceRecord]:
        return [r for r in (map_medrxiv(x, "validation", "enrichment", self.server) for x in self.versions(doi)) if r]


def map_medrxiv(x: dict, query_id: str, strategy: str, server: str = "medrxiv") -> SourceRecord | None:
    doi = normalize_doi(x.get("doi"))
    if not doi:
        return None
    try:
        version = int(x.get("version") or 1)
    except ValueError:
        version = 1
    d = parse_date(x.get("date"))
    published = x.get("published")
    authors = [a.strip() for a in (x.get("authors") or "").split(";") if a.strip()]
    return SourceRecord(
        provider="medrxiv", source_record_id=f"{doi}v{version}", query_id=query_id, strategy=strategy,
        retrieved_at=iso_utc(), doi=doi,
        preprint_server=(x.get("server") or server).lower(), preprint_version=version,
        published_doi=normalize_doi(published) if published and published != "NA" else None,
        title=strip_markup(x.get("title")),
        authors=authors,
        journal=None,
        publisher="Cold Spring Harbor Laboratory",
        publication_type=f"preprint ({x.get('type')})" if x.get("type") else "preprint",
        is_preprint=True,
        preprint_posted_date=d if version == 1 else None,
        preprint_revision_date=d,
        indexed_date=d,
        abstract=strip_markup(x.get("abstract")),
        external_topics=[x["category"]] if x.get("category") else [],
        source_url=f"https://www.medrxiv.org/content/{doi}v{version}",
        license=LICENSES.get((x.get("license") or "").lower(), x.get("license")),
    )
