from datetime import date

import pytest

from research_agent.metadata.deduplication import ingest_record
from research_agent.metadata.schemas import SourceRecord
from research_agent.storage.repositories import load_paper

TITLE = "A microsimulation model of hepatitis C elimination in prisons"


def rec(provider="crossref", sid=None, **kw) -> SourceRecord:
    base = dict(provider=provider, source_record_id=sid or kw.get("doi") or kw.get("pmid") or "x", query_id="q",
                strategy="topic", retrieved_at="2026-10-21T00:00:00Z", title=TITLE, authors=["Smith, Jane", "Lee, K"],
                online_date=date(2026, 10, 10))
    base.update(kw)
    return SourceRecord(**base)


def test_same_doi_from_two_providers_merges(db):
    a = ingest_record(db, rec("crossref", doi="10.5555/a", abstract=None))
    b = ingest_record(db, rec("pubmed", sid="111", doi="10.5555/a", pmid="999990001", abstract="Original abstract.",
                              mesh_terms=["Hepatitis C"]))
    assert a.paper_id == b.paper_id and a.created and not b.created
    p = load_paper(db, a.paper_id)
    assert p.pmid == "999990001" and p.abstract == "Original abstract." and p.abstract_source == "pubmed"
    assert p.field_provenance["title"].source == "crossref" and p.mesh_terms == ["Hepatitis C"]
    assert db.scalar("SELECT COUNT(*) FROM papers") == 1


def test_repeated_ingestion_is_idempotent(db):
    r = rec(doi="10.5555/a")
    for _ in range(3):
        ingest_record(db, r)
    assert db.scalar("SELECT COUNT(*) FROM papers") == 1
    assert db.scalar("SELECT COUNT(*) FROM source_records") == 1
    assert db.scalar("SELECT times_seen FROM paper_discoveries") == 3


def test_lower_priority_source_never_overwrites(db):
    ingest_record(db, rec("pubmed", sid="1", pmid="999990001", doi="10.5555/a", abstract="PubMed abstract."))
    ingest_record(db, rec("openalex", sid="W1", openalex_id="W1", doi="10.5555/a", abstract="Reconstructed words.",
                          title="A Microsimulation Model Of Hepatitis C Elimination In Prisons (OpenAlex)"))
    p = load_paper(db, 1)
    assert p.abstract == "PubMed abstract." and p.title == TITLE


def test_pmid_only_record_upgrades_to_doi_canonical_id(db):
    a = ingest_record(db, rec("pubmed", sid="1", pmid="999990001", doi=None))
    assert a.canonical_id == "pmid:999990001"
    b = ingest_record(db, rec("europe_pmc", sid="MED:1", pmid="999990001", doi="10.5555/a"))
    assert b.paper_id == a.paper_id and b.canonical_id == "doi:10.5555/a"
    assert db.scalar("SELECT paper_id FROM paper_identifiers WHERE scheme='canonical_alias' AND value='pmid:999990001'") == a.paper_id


def test_bridging_record_merges_two_partial_papers(db):
    a = ingest_record(db, rec("crossref", doi="10.5555/a"))
    b = ingest_record(db, rec("pubmed", sid="1", pmid="999990001", doi=None, title="Different title so no fallback"))
    assert a.paper_id != b.paper_id
    c = ingest_record(db, rec("europe_pmc", sid="MED:1", doi="10.5555/a", pmid="999990001"))
    assert db.scalar("SELECT COUNT(*) FROM papers") == 1 and c.paper_id == a.paper_id
    assert db.scalar("SELECT COUNT(*) FROM source_records WHERE paper_id=?", (a.paper_id,)) == 3


def test_conflicting_identifiers_never_merge(db):
    a = ingest_record(db, rec("crossref", doi="10.5555/a"))
    b = ingest_record(db, rec("crossref", doi="10.5555/b"))          # same title, different DOI
    assert a.paper_id != b.paper_id
    c = ingest_record(db, rec("pubmed", sid="1", pmid="999990001", doi="10.5555/a"))
    d = ingest_record(db, rec("pubmed", sid="2", pmid="999990001", doi="10.5555/b"))   # PMID owned by a
    assert c.paper_id == a.paper_id and d.paper_id == b.paper_id and d.conflict
    assert db.scalar("SELECT COUNT(*) FROM papers") == 2
    assert db.scalar("SELECT COUNT(*) FROM identity_conflicts") >= 1 or d.conflict


def test_title_fallback_is_conservative(db):
    a = ingest_record(db, rec("crossref", doi="10.5555/a"))
    same = ingest_record(db, rec("pubmed", sid="1", pmid="999990001", doi=None))
    assert same.paper_id == a.paper_id                                # same title, author, year, no conflict
    other_author = ingest_record(db, rec("pubmed", sid="2", pmid="999990002", doi=None, authors=["Jones, A"]))
    assert other_author.paper_id != a.paper_id
    short = ingest_record(db, rec("pubmed", sid="3", pmid="999990003", doi=None, title="Editorial"))
    short2 = ingest_record(db, rec("pubmed", sid="4", pmid="999990004", doi=None, title="Editorial"))
    assert short.paper_id != short2.paper_id                          # short titles never match by title


def test_preprint_versions_and_publication_link(db):
    v1 = rec("medrxiv", sid="10.1101/2026.01.01.1v1", doi="10.1101/2026.01.01.1", is_preprint=True,
             preprint_server="medrxiv", preprint_version=1, preprint_posted_date=date(2026, 9, 1),
             preprint_revision_date=date(2026, 9, 1), abstract="Version one abstract.", online_date=None)
    p1 = ingest_record(db, v1)
    v2 = v1.model_copy(update={"source_record_id": "10.1101/2026.01.01.1v2", "preprint_version": 2,
                               "preprint_posted_date": None, "preprint_revision_date": date(2026, 10, 1),
                               "abstract": "Version two abstract, materially revised.",
                               "published_doi": "10.5555/journal.1"})
    p2 = ingest_record(db, v2)
    assert p1.paper_id == p2.paper_id and p2.metadata_changed
    paper = load_paper(db, p1.paper_id)
    assert paper.preprint_version == 2 and paper.abstract.startswith("Version two")
    assert paper.first_publication_date == date(2026, 9, 1)            # first posting, not the revision
    assert paper.latest_revision_date == date(2026, 10, 1)
    assert paper.preprint_status == "preprint_published" and paper.peer_review_status == "preprint"
    assert db.scalar("SELECT COUNT(*) FROM paper_versions WHERE paper_id=?", (p1.paper_id,)) == 2
    assert db.scalar("SELECT COUNT(*) FROM metadata_revisions WHERE field='abstract'") == 1
    j = ingest_record(db, rec("crossref", doi="10.5555/journal.1", title=TITLE))
    assert j.paper_id != p1.paper_id                                   # journal version is a separate paper
    assert db.scalar("SELECT COUNT(*) FROM paper_relations WHERE from_paper_id=? AND to_paper_id=? AND"
                     " relation='published_as'", (p1.paper_id, j.paper_id)) == 1


def test_record_without_identifiers_is_refused(db):
    with pytest.raises(ValueError):
        ingest_record(db, rec(doi=None))


def test_conflicting_identifier_from_weaker_source_is_dropped_not_fatal(db):
    """Live-observed case: an OpenAlex work matched by DOI but listing a different PMID."""
    a = ingest_record(db, rec("pubmed", sid="1", pmid="999990001", doi="10.5555/a"))
    b = ingest_record(db, rec("openalex", sid="W9", openalex_id="W9", doi="10.5555/a", pmid="999990002"))
    assert b.paper_id == a.paper_id
    p = load_paper(db, a.paper_id)
    assert p.pmid == "999990001" and p.openalex_id == "W9"
    assert db.scalar("SELECT COUNT(*) FROM paper_identifiers WHERE value='999990002'") == 0
    assert "dropped conflicting pmid" in db.scalar("SELECT resolution FROM identity_conflicts")
