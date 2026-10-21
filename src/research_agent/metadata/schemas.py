"""Canonical metadata models.

SourceRecord: what ONE provider said about ONE work in ONE response (immutable provenance).
Paper: the merged canonical view, with per-field provenance. Missing = None, never guessed.

Three keyword kinds are deliberately separate fields and never conflated:
  author_keywords (from the authors), mesh_terms (controlled vocabulary),
  ai_topic_tags (Claude/taxonomy assignments, stored in paper_tags, not here).
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Provider = Literal["pubmed", "europe_pmc", "openalex", "crossref", "medrxiv", "arxiv", "semantic_scholar", "fixture"]
PeerReview = Literal["peer_reviewed", "preprint", "unknown"]

# Fields whose change produces a new metadata version (re-evaluation trigger).
CRITICAL_FIELDS = ("title", "abstract", "authors", "journal", "publication_type",
                   "first_publication_date", "preprint_version")


class SourceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Provider
    source_record_id: str                       # provider-native id (PMID, OpenAlex W-id, DOI+version ...)
    query_id: str                               # which query / journal window found it
    strategy: Literal["topic", "journal", "window", "landscape", "enrichment", "fixture"]
    retrieved_at: str                           # UTC ISO timestamp

    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    openalex_id: str | None = None
    arxiv_id: str | None = None
    preprint_server: str | None = None          # medrxiv | biorxiv | arxiv | ssrn ...
    preprint_version: int | None = None
    published_doi: str | None = None            # preprint -> journal publication link

    title: str | None = None
    authors: list[str] = []
    journal: str | None = None
    journal_issns: list[str] = []
    publisher: str | None = None
    publication_type: str | None = None
    is_preprint: bool = False
    is_retracted: bool = False

    online_date: date | None = None             # first online / electronic publication
    print_date: date | None = None
    preprint_posted_date: date | None = None    # first version posted
    preprint_revision_date: date | None = None  # this version posted
    indexed_date: date | None = None            # entered this index (PubMed EDAT, EPMC FIRST_IDATE)
    modified_date: date | None = None           # provider metadata modification
    date_precision: dict[str, str] = {}

    abstract: str | None = None
    abstract_sha256: str | None = None          # set when the abstract text was dropped (brief-info storage)
    author_keywords: list[str] = []
    mesh_terms: list[str] = []
    external_topics: list[str] = []             # provider classifications (e.g. OpenAlex topics)
    source_url: str | None = None
    open_access_url: str | None = None
    license: str | None = None

    def identity_keys(self) -> list[tuple[str, str]]:
        """(scheme, value) identifiers usable for persistent identity mapping."""
        keys: list[tuple[str, str]] = []
        if self.doi:
            keys.append(("doi", self.doi))
        if self.pmid:
            keys.append(("pmid", self.pmid))
        if self.pmcid:
            keys.append(("pmcid", self.pmcid))
        if self.openalex_id:
            keys.append(("openalex", self.openalex_id))
        if self.arxiv_id:
            keys.append(("arxiv", self.arxiv_id))
        return keys

    def content_hash(self) -> str:
        data = self.model_dump(mode="json", exclude={"retrieved_at", "query_id", "strategy"})
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


class FieldProvenance(BaseModel):
    source: str
    retrieved_at: str


class Paper(BaseModel):
    """Canonical paper (spec section 6). Every attribute is either verified from a source
    (see field_provenance) or None/empty."""

    model_config = ConfigDict(extra="forbid")

    id: int | None = None                       # internal surrogate key
    canonical_id: str
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    openalex_id: str | None = None
    arxiv_id: str | None = None
    preprint_id: str | None = None              # source-qualified, e.g. "medrxiv:10.64898/..."
    title: str | None = None
    authors: list[str] = []
    journal: str | None = None
    journal_issn: list[str] = []
    publisher: str | None = None
    publication_type: str | None = None
    peer_review_status: PeerReview = "unknown"
    preprint_status: Literal["not_preprint", "preprint", "preprint_published"] = "not_preprint"
    preprint_version: int | None = None
    first_publication_date: date | None = None  # best verified first public availability
    online_date: date | None = None
    print_date: date | None = None
    preprint_posted_date: date | None = None
    latest_revision_date: date | None = None
    indexed_date: date | None = None
    modified_date: date | None = None
    discovered_at: str | None = None            # UTC ISO, first time this system saw it
    last_verified_at: str | None = None
    abstract: str | None = None
    abstract_source: str | None = None
    author_keywords: list[str] = []
    mesh_terms: list[str] = []
    external_topics: list[str] = []
    ai_topic_tags: list[str] = []               # populated from paper_tags at read time only
    source_urls: list[str] = []
    canonical_url: str | None = None
    open_access_url: str | None = None
    license: str | None = None
    retrieval_sources: list[str] = []
    retrieval_queries: list[str] = []
    metadata_completeness: float = Field(default=0.0, ge=0, le=1)
    metadata_version: str = ""
    validation_status: Literal["unverified", "verified", "warning", "failed"] = "unverified"
    is_retracted: bool = False
    field_provenance: dict[str, FieldProvenance] = {}
    prescreen_score: float | None = None
    prescreen_topics: list[str] = []

    def compute_metadata_version(self) -> str:
        data = {f: getattr(self, f) for f in CRITICAL_FIELDS}
        return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:12]

    def compute_completeness(self) -> float:
        checks = [
            bool(self.title),
            bool(self.authors),
            bool(self.journal),
            bool(self.doi or self.pmid or self.preprint_id),
            self.first_publication_date is not None,
            bool(self.abstract),
            bool(self.canonical_url),
            bool(self.author_keywords or self.mesh_terms),
        ]
        return round(sum(checks) / len(checks), 4)

    @property
    def is_preprint(self) -> bool:
        return self.preprint_status in ("preprint", "preprint_published")
