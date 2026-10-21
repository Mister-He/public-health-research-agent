"""Deterministic construction of the canonical Paper from its source records.

The canonical view is a pure function of the stored SourceRecords: every field comes from
the highest-priority source that has it (lower-priority sources only fill gaps), so a less
reliable source can never overwrite better metadata, and rebuilding is idempotent.
"""

from __future__ import annotations

from datetime import date

from research_agent.metadata.schemas import FieldProvenance, Paper, SourceRecord

# Per-field source priority (first wins). Providers missing from a list never supply that field.
_JOURNAL_FIRST = ["crossref", "pubmed", "europe_pmc", "openalex", "medrxiv", "arxiv", "fixture"]
PRIORITY: dict[str, list[str]] = {
    "title": ["crossref", "pubmed", "europe_pmc", "medrxiv", "arxiv", "openalex", "fixture"],
    "authors": ["crossref", "pubmed", "europe_pmc", "medrxiv", "arxiv", "openalex", "fixture"],
    "journal": _JOURNAL_FIRST,
    "journal_issns": ["crossref", "pubmed", "openalex", "europe_pmc", "fixture"],
    "publisher": ["crossref", "openalex", "medrxiv", "fixture"],
    "publication_type": ["pubmed", "crossref", "europe_pmc", "openalex", "medrxiv", "arxiv", "fixture"],
    # Abstracts: curated indexes first; Crossref JATS and OpenAlex reconstruction last.
    "abstract": ["pubmed", "europe_pmc", "medrxiv", "arxiv", "crossref", "openalex", "fixture"],
    "author_keywords": ["pubmed", "europe_pmc", "fixture"],
    "mesh_terms": ["pubmed", "europe_pmc", "fixture"],
    "external_topics": ["openalex"],
    "license": ["crossref", "europe_pmc", "medrxiv", "openalex", "fixture"],
    "open_access_url": ["openalex", "europe_pmc", "medrxiv", "arxiv", "fixture"],
    "online_date": ["crossref", "pubmed", "europe_pmc", "openalex", "fixture"],
    "print_date": ["crossref", "pubmed", "fixture"],
}
# For preprints the server is authoritative for title/abstract/authors.
PREPRINT_OVERRIDE = {"title", "abstract", "authors"}

_ID_FIELDS = ("doi", "pmid", "pmcid", "openalex_id", "arxiv_id")
_ID_AUTHORITY = ["pubmed", "crossref", "medrxiv", "arxiv", "europe_pmc", "openalex", "fixture"]


def _rank(field: str, provider: str, preprint: bool) -> int:
    order = PRIORITY.get(field, _JOURNAL_FIRST)
    if preprint and field in PREPRINT_OVERRIDE and provider in ("medrxiv", "arxiv"):
        return -1
    return order.index(provider) if provider in order else 999


def _empty(v: object) -> bool:
    return v is None or v == [] or v == ""


def canonical_id_for(doi: str | None, pmid: str | None, arxiv: str | None,
                     pmcid: str | None, openalex: str | None) -> str:
    if doi:
        return f"doi:{doi}"
    if pmid:
        return f"pmid:{pmid}"
    if arxiv:
        return f"arxiv:{arxiv}"
    if pmcid:
        return f"pmcid:{pmcid}"
    if openalex:
        return f"openalex:{openalex}"
    raise ValueError("record has no usable identifier")


def build_paper(records: list[SourceRecord], *, canonical_id: str, discovered_at: str,
                paper_id: int | None = None, last_verified_at: str | None = None) -> Paper:
    if not records:
        raise ValueError("cannot build a paper without source records")
    # Latest preprint version first, then most recent retrieval: deterministic tie-break.
    recs = sorted(records, key=lambda r: (r.preprint_version or 0, r.retrieved_at, r.provider,
                                          r.source_record_id), reverse=True)
    preprint = any(r.is_preprint for r in recs)
    prov: dict[str, FieldProvenance] = {}

    def pick(field: str, attr: str | None = None):
        attr = attr or field
        best = None
        best_rank = 10_000
        for r in recs:
            v = getattr(r, attr)
            if _empty(v):
                continue
            rk = _rank(field, r.provider, preprint)
            if rk < best_rank:
                best, best_rank = (v, r), rk
        if best is None:
            return None
        prov[field] = FieldProvenance(source=best[1].provider, retrieved_at=best[1].retrieved_at)
        return best[0]

    ids = {f: None for f in _ID_FIELDS}
    for f in _ID_FIELDS:
        # Deduplication drops conflicting identifiers at ingest; if any slipped through, the
        # value from the most authoritative provider wins deterministically.
        cands = sorted(((_ID_AUTHORITY.index(r.provider) if r.provider in _ID_AUTHORITY else 99, getattr(r, f))
                        for r in recs if getattr(r, f)))
        if cands:
            ids[f] = cands[0][1]

    server = next((r.preprint_server for r in recs if r.preprint_server), None)
    versions = [r.preprint_version for r in recs if r.preprint_version]
    posted = [r.preprint_posted_date for r in recs if r.preprint_posted_date]
    revised = [r.preprint_revision_date for r in recs if r.preprint_revision_date]
    indexed = [r.indexed_date for r in recs if r.indexed_date]
    modified = [r.modified_date for r in recs if r.modified_date]
    published_dois = sorted({r.published_doi for r in recs if r.published_doi})

    online = pick("online_date")
    printed = pick("print_date")
    first_pub = _first_publication(preprint, posted, online, printed, recs)

    doi = ids["doi"]
    preprint_id = f"{server}:{doi or ids['arxiv_id']}" if preprint and server and (doi or ids["arxiv_id"]) else None
    canonical_url = (f"https://doi.org/{doi}" if doi else
                     f"https://pubmed.ncbi.nlm.nih.gov/{ids['pmid']}/" if ids["pmid"] else
                     f"https://arxiv.org/abs/{ids['arxiv_id']}" if ids["arxiv_id"] else None)
    source_urls = sorted({r.source_url for r in recs if r.source_url})

    paper = Paper(
        id=paper_id,
        canonical_id=canonical_id,
        doi=doi,
        pmid=ids["pmid"],
        pmcid=ids["pmcid"],
        openalex_id=ids["openalex_id"],
        arxiv_id=ids["arxiv_id"],
        preprint_id=preprint_id,
        title=pick("title"),
        authors=pick("authors") or [],
        journal=pick("journal"),
        journal_issn=pick("journal_issn", "journal_issns") or [],
        publisher=pick("publisher"),
        publication_type=pick("publication_type"),
        peer_review_status="preprint" if preprint else ("peer_reviewed" if _is_journal(recs) else "unknown"),
        preprint_status=("preprint_published" if preprint and published_dois else
                         "preprint" if preprint else "not_preprint"),
        preprint_version=max(versions) if versions else None,
        first_publication_date=first_pub,
        online_date=online,
        print_date=printed,
        preprint_posted_date=min(posted) if posted else None,
        latest_revision_date=max(revised) if revised else None,
        indexed_date=min(indexed) if indexed else None,
        modified_date=max(modified) if modified else None,
        discovered_at=discovered_at,
        last_verified_at=last_verified_at,
        abstract=pick("abstract"),
        abstract_source=None,
        author_keywords=pick("author_keywords") or [],
        mesh_terms=pick("mesh_terms") or [],
        external_topics=pick("external_topics") or [],
        source_urls=source_urls,
        canonical_url=canonical_url,
        open_access_url=pick("open_access_url"),
        license=pick("license"),
        retrieval_sources=sorted({r.provider for r in recs}),
        retrieval_queries=sorted({f"{r.provider}:{r.query_id}" for r in recs}),
        is_retracted=any(r.is_retracted for r in recs),
        field_provenance=prov,
    )
    if paper.abstract:
        paper.abstract_source = prov["abstract"].source
    paper.metadata_completeness = paper.compute_completeness()
    paper.metadata_version = paper.compute_metadata_version()
    return paper


def published_dois_of(records: list[SourceRecord]) -> list[str]:
    return sorted({r.published_doi for r in records if r.published_doi})


def _is_journal(recs: list[SourceRecord]) -> bool:
    return any(r.journal and not r.is_preprint for r in recs)


def _first_publication(preprint: bool, posted: list[date], online: date | None,
                       printed: date | None, recs: list[SourceRecord]) -> date | None:
    """Best verified first public availability. Preprints: first posting date.
    Journal articles: earliest of online/print with day precision; month/year-precision
    dates are used only when nothing more precise exists."""
    if preprint:
        return min(posted) if posted else None
    exact = []
    coarse = []
    for r in recs:
        for f in ("online_date", "print_date"):
            v = getattr(r, f)
            if v is None:
                continue
            (exact if r.date_precision.get(f, "day") == "day" else coarse).append(v)
    if exact:
        return min(exact)
    if coarse:
        return min(coarse)
    return online or printed
