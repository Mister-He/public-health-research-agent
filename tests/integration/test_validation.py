"""Validation fixtures (spec 21.4): identity, URL, metadata, score and Markdown integrity."""

from __future__ import annotations

import httpx
import pytest
from fixtures.synthetic import REPORT_DATE

from research_agent.ingestion.http import ProviderError
from research_agent.metadata.schemas import Paper, SourceRecord
from research_agent.reporting.markdown import ABSTRACT_UNAVAILABLE
from research_agent.validation.links import LinkVerifier
from research_agent.validation.markdown import check_structure, parse
from research_agent.validation.metadata import IdentityVerifier

DOI = "10.5555/fixture.0001"
TITLE = "Bayesian calibration of a lung cancer screening microsimulation model using emulators"
PAPER = Paper(canonical_id=f"doi:{DOI}", doi=DOI, title=TITLE, canonical_url=f"https://doi.org/{DOI}")


class FakeCrossref:
    def __init__(self, title=TITLE, doi=DOI, missing=False, down=False):
        self.title, self.doi, self.missing, self.down = title, doi, missing, down

    def lookup_doi(self, doi):
        if self.down:
            raise ProviderError("crossref", "HTTP 503", status=503)
        if self.missing:
            return None
        return SourceRecord(provider="crossref", source_record_id=self.doi, query_id="v", strategy="enrichment",
                            retrieved_at="x", doi=self.doi, title=self.title)


def test_identity_valid_article_matching_doi():
    assert IdentityVerifier(FakeCrossref()).verify(PAPER).status == "pass"


def test_identity_nonexistent_doi():
    c = IdentityVerifier(FakeCrossref(missing=True)).verify(PAPER)
    assert c.status == "fail" and "not registered" in c.detail


def test_identity_doi_points_to_wrong_paper():
    c = IdentityVerifier(FakeCrossref(title="Prevalence of myopia in adolescents")).verify(PAPER)
    assert c.status == "fail" and "title mismatch" in c.detail


def test_identity_incorrect_title_minor_markup_ok():
    ok = IdentityVerifier(FakeCrossref(title=TITLE.upper() + ".")).verify(PAPER)
    assert ok.status == "pass"


def test_identity_source_temporarily_unavailable_is_warning():
    c = IdentityVerifier(FakeCrossref(down=True)).verify(PAPER)
    assert c.status == "warn"


def _links(handler) -> LinkVerifier:
    return LinkVerifier(httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False), sleep=lambda s: None)


def _router(landing):
    def h(req: httpx.Request):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        if req.url.host == "doi.org":
            return httpx.Response(302, headers={"location": "https://publisher.example/article/1"})
        return landing(req)
    return h


def test_url_verified_by_citation_doi():
    r = _links(_router(lambda req: httpx.Response(200, text=f'<meta name="citation_doi" content="{DOI}">'))).check(
        f"https://doi.org/{DOI}", doi=DOI, title=TITLE, identity_verified=True)
    assert r.status == "verified" and r.final_url == "https://publisher.example/article/1"


def test_url_identity_mismatch():
    r = _links(_router(lambda req: httpx.Response(200, text='<meta name="citation_doi" content="10.5555/other">'))).check(
        f"https://doi.org/{DOI}", doi=DOI, title=TITLE, identity_verified=True)
    assert r.status == "identity_mismatch"


def test_url_bot_protection_is_not_proof_of_absence():
    r = _links(_router(lambda req: httpx.Response(403, text="Just a moment... cf-chl"))).check(
        f"https://doi.org/{DOI}", doi=DOI, title=TITLE, identity_verified=True)
    assert r.status == "verified_via_authoritative_metadata"
    r2 = _links(_router(lambda req: httpx.Response(403))).check(f"https://doi.org/{DOI}", doi=DOI, title=TITLE,
                                                                identity_verified=False)
    assert r2.status == "temporarily_unavailable"


def test_url_broken_and_malformed():
    assert _links(_router(lambda req: httpx.Response(404))).check(
        f"https://doi.org/{DOI}", doi=DOI, title=TITLE, identity_verified=True).status == "invalid"
    assert _links(lambda r: httpx.Response(200)).check("ftp://x", doi=None, title=None,
                                                      identity_verified=True).status == "invalid"


def test_url_timeout_is_temporary():
    def h(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        raise httpx.ConnectTimeout("t", request=req)
    assert _links(h).check(f"https://doi.org/{DOI}", doi=DOI, title=TITLE, identity_verified=True).status == \
        "temporarily_unavailable"


def test_robots_txt_respected():
    fetched = []

    def h(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /")
        fetched.append(str(req.url))
        return httpx.Response(200)
    r = _links(h).check(f"https://doi.org/{DOI}", doi=DOI, title=TITLE, identity_verified=True)
    assert r.status == "verified_via_authoritative_metadata" and fetched == []


GOOD = f"""---
date: '{REPORT_DATE.isoformat()}'
timezone: Asia/Shanghai
generated_at_utc: '2026-10-21T00:00:00Z'
paper_count: 1
pipeline_version: 0.1.0
scoring_version: s
trend_snapshot_version: t
validation_status: pending
---

# Weekly Digest - {REPORT_DATE.isoformat()}

## Weekly overview

{"word " * 160}

## Recommendations

### 1. {TITLE}

| Field | Value |
|---|---|
| Canonical ID | `doi:{DOI}` |
| Rank | 1 (raw-score rank 1) |
| Authors | A |
| Journal / server | J |
| Publication date | 2026-10-19 (first online) |
| DOI | [{DOI}](https://doi.org/{DOI}) |
| PMID | Not available from verified sources |
| URL | <https://doi.org/{DOI}> |
| Peer review / preprint status | Peer-reviewed journal article |
| Journal impact | OpenAlex 2-yr mean citedness 17.0; h-index 154; on your journal list |
| Overall score | **80.00** / 100 |

**Abstract**

{ABSTRACT_UNAVAILABLE}

**Author keywords:** No author keywords in verified sources

**MeSH terms:** No MeSH terms in verified sources

**AI-assigned topic tags** *(AI-generated classification, not author keywords)*: No tags assigned

**Matched emerging trend tags:** No matched trend tags

| Dimension | Weight | Score | Method | Confidence |
|---|---:|---:|---|---:|
| Research alignment | 0.30 | 80.00 | Claude (rubric) | 0.70 |

**Methodological assessment** *(AI interpretation)*: text

**Why it is recommended for you** *(AI interpretation)*: text

**Evidence and confidence limitations:** No limitations reported.
"""


def _check(settings, text, name=None, count=1):
    path = settings.reports_dir / (name or f"{REPORT_DATE.isoformat()}.md")
    _, checks = check_structure(path, text, REPORT_DATE, settings.reports_dir, expected_count=count,
                                enabled_dimensions=["research_alignment"],
                                dimension_names={"research_alignment": "Research alignment"})
    return {c.name for c in checks if c.status == "fail"}


def test_markdown_good_fixture_passes(settings):
    assert _check(settings, GOOD) == set()
    assert parse(GOOD).papers[0].abstract == ABSTRACT_UNAVAILABLE


@pytest.mark.parametrize("mutate,expected", [
    (lambda t: t, {"filename_date"}),                                                        # incorrect report date
    (lambda t: t.replace("date: '2026-10-21'", "date: '2026-10-20'"), {"front_matter_date"}),
    (lambda t: t.replace("timezone: Asia/Shanghai\n", "timezone: [unclosed\n"), {"front_matter_valid"}),
    (lambda t: t.replace("**MeSH terms:** No MeSH terms in verified sources\n", ""), {"label:MeSH terms"}),
    (lambda t: t.replace(ABSTRACT_UNAVAILABLE, ""), {"abstract_present_or_marked"}),
    (lambda t: t.replace("| URL | <https://doi.org/", "| URL | <http//doi.org/"), {"url_format"}),
    (lambda t: t.replace("{{", "").replace("Weekly overview\n\n", "Weekly overview\n\n{{ narrative }} "), {"no_placeholders"}),
    (lambda t: t.replace("| Research alignment | 0.30", "| Freshness | 0.30"), {"score_breakdown_complete"}),
    (lambda t: t.replace("word " * 160, "- a\n- b\n"), {"overview_nonempty", "overview_is_prose"}),
    (lambda t: t.replace("Not available from verified sources", "None"), {"no_placeholders"}),
])
def test_markdown_corruptions_detected(settings, mutate, expected):
    name = "2026-10-22.md" if expected == {"filename_date"} else None
    failed = _check(settings, mutate(GOOD), name=name)
    assert expected <= failed, failed


def test_duplicate_paper_sections_detected(settings):
    section = GOOD[GOOD.index("### 1."):]
    dup = GOOD + "\n" + section.replace("### 1.", "### 2.")
    dup = dup.replace("paper_count: 1", "paper_count: 2")
    failed = _check(settings, dup, count=2)
    assert {"unique_paper_ids", "no_duplicate_sections"} <= failed


def test_wrong_paper_count(settings):
    assert "front_matter_paper_count" in _check(settings, GOOD, count=2)
