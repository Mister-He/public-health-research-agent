"""Paper-level verification: rendered fields vs persisted canonical records and their stored
provenance (offline), and article identity vs authoritative providers (online)."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from research_agent.ingestion.http import ProviderError
from research_agent.metadata.normalization import normalize_title
from research_agent.metadata.schemas import Paper
from research_agent.reporting.markdown import ABSTRACT_UNAVAILABLE
from research_agent.reporting.renderer import paper_view
from research_agent.storage.database import Database
from research_agent.storage.repositories import source_records_for
from research_agent.validation.markdown import ParsedPaper
from research_agent.validation.results import Check


def _ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def unescape_md(s: str) -> str:
    """Undo only the leading block-marker escape added by reporting.markdown.blockquote."""
    return re.sub(r"^(\s*\d*)\\([#>+\-*=.)])", r"\1\2", s)


def check_rendered_fields(pp: ParsedPaper, paper: Paper, expected_view: dict, rec_row) -> list[Check]:
    """The report must show exactly what the canonical record says (or explicit unavailable markers)."""
    cid = paper.canonical_id
    out: list[Check] = []

    def ok(name: str, cond: bool, detail: str = "") -> None:
        out.append(Check(name, cid, "pass" if cond else "fail", "" if cond else detail[:400]))

    ok("record_exists", rec_row is not None, "no recommendation record for this paper/date")
    ok("title_matches_record", pp.heading == expected_view["title"], f"{pp.heading!r} != {expected_view['title']!r}")
    for field_name, key in (("Authors", "authors_cell"), ("Journal / server", "venue_cell"),
                            ("Publication date", "date_cell"), ("DOI", "doi_cell"), ("PMID", "pmid_cell"),
                            ("URL", "url_cell"), ("Peer review / preprint status", "status_cell"),
                            ("Journal impact", "impact_cell")):
        ok(f"field:{field_name}", pp.fields.get(field_name) == expected_view[key],
           f"{field_name}: {pp.fields.get(field_name)!r} != {expected_view[key]!r}")
    ok("keywords_match_record", pp.labels.get("Author keywords", "").lstrip(":").strip() == expected_view["keywords_cell"],
       "author keywords differ from the verified record")
    ok("mesh_match_record", pp.labels.get("MeSH terms", "").lstrip(":").strip() == expected_view["mesh_cell"],
       "MeSH terms differ from the verified record")
    if paper.abstract:
        shown = _ws(unescape_md(pp.abstract))
        ok("abstract_verbatim", shown == _ws(paper.abstract) or pp.abstract.startswith("Abstract not reproduced"),
           "rendered abstract differs from the stored original abstract")
    else:
        ok("abstract_unavailable_marker", pp.abstract.strip() == ABSTRACT_UNAVAILABLE,
           f"missing abstract must be marked exactly as {ABSTRACT_UNAVAILABLE!r}")
    if rec_row is not None:
        ok("overall_score_display", pp.fields.get("Overall score") == f"**{rec_row['raw_score']:.2f}** / 100",
           f"displayed {pp.fields.get('Overall score')!r} vs stored {rec_row['raw_score']:.2f}")
    return out


def check_provenance(db: Database, paper: Paper) -> list[Check]:
    """Every critical field must be traceable to a stored provider record with the same value."""
    cid = paper.canonical_id
    out: list[Check] = []
    recs = source_records_for(db, paper.id)  # type: ignore[arg-type]
    by_provider = {}
    for r in recs:
        by_provider.setdefault(r.provider, []).append(r)
    for fld, attr in (("title", "title"), ("abstract", "abstract"), ("authors", "authors"), ("journal", "journal")):
        val = getattr(paper, attr)
        if not val:
            continue
        src = paper.field_provenance.get(fld)
        if src is None:
            out.append(Check(f"provenance:{fld}", cid, "fail", f"{fld} has no recorded source"))
            continue
        match = any(getattr(r, attr) == val for r in by_provider.get(src.source, []))
        out.append(Check(f"provenance:{fld}", cid, "pass" if match else "fail",
                         "" if match else f"{fld} not found verbatim in stored {src.source} record (possible fabrication)"))
    ids_ok = True
    for scheme, val in (("doi", paper.doi), ("pmid", paper.pmid)):
        if val and not any(getattr(r, scheme) == val for r in recs):
            ids_ok = False
    out.append(Check("provenance:identifiers", cid, "pass" if ids_ok else "fail",
                     "" if ids_ok else "identifier not present in any stored source record"))
    return out


def title_matches(a: str | None, b: str | None, ratio: float) -> bool:
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return False
    return na == nb or SequenceMatcher(None, na, nb).ratio() >= ratio


class IdentityVerifier:
    """Confirms DOI/PMID/preprint identity against authoritative providers."""

    def __init__(self, crossref=None, pubmed=None, medrxiv=None, *, title_ratio: float = 0.93):
        self.crossref, self.pubmed, self.medrxiv = crossref, pubmed, medrxiv
        self.ratio = title_ratio

    def verify(self, paper: Paper) -> Check:
        cid = paper.canonical_id
        try:
            if paper.doi and paper.preprint_id and paper.preprint_id.startswith(("medrxiv", "biorxiv")) and self.medrxiv:
                recs = self.medrxiv.lookup(paper.doi)
                if not recs:
                    return Check("identity", cid, "fail", f"preprint DOI {paper.doi} not found on the preprint server")
                latest = max(recs, key=lambda r: r.preprint_version or 0)
                return self._compare(cid, paper, latest.title, latest.doi, "medrxiv")
            if paper.doi and self.crossref:
                rec = self.crossref.lookup_doi(paper.doi)
                if rec is None:
                    return Check("identity", cid, "fail", f"DOI {paper.doi} is not registered (Crossref 404)")
                return self._compare(cid, paper, rec.title, rec.doi, "crossref")
            if paper.pmid and self.pubmed:
                recs = self.pubmed.fetch_pmids([paper.pmid])
                if not recs:
                    return Check("identity", cid, "fail", f"PMID {paper.pmid} not found in PubMed")
                return self._compare(cid, paper, recs[0].title, recs[0].doi, "pubmed")
        except ProviderError as exc:
            return Check("identity", cid, "warn", f"authoritative provider temporarily unavailable: {str(exc)[:160]}")
        return Check("identity", cid, "warn", "no online verifier available for this identifier type")

    def _compare(self, cid: str, paper: Paper, title: str | None, doi: str | None, src: str) -> Check:
        if paper.doi and doi and doi != paper.doi:
            return Check("identity", cid, "fail", f"{src} returned DOI {doi} for {paper.doi}")
        if not title_matches(title, paper.title, self.ratio):
            return Check("identity", cid, "fail", f"title mismatch vs {src}: {title!r}")
        return Check("identity", cid, "pass", f"verified via {src}")


def expected_view(item, settings, snapshot_tags):
    return paper_view(item, settings, snapshot_tags)
