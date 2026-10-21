"""LIVE checks against real APIs. Not part of the default suite: run with `pytest -m live`.
They verify that today's API behaviour still matches the adapters (formats change)."""

from __future__ import annotations

import os
from datetime import timedelta

import pytest

from research_agent.ingestion.http import ProviderError
from research_agent.pipeline.checkpoints import make_http, make_providers
from research_agent.retrieval.query_builder import build_queries
from research_agent.timeutil import utcnow
from research_agent.validation.links import LinkVerifier
from research_agent.validation.metadata import IdentityVerifier

pytestmark = pytest.mark.live


@pytest.fixture
def providers(settings):
    os.environ.setdefault("RESEARCH_AGENT_CONTACT_EMAIL", "research-agent-tests@example.org")
    return make_providers(settings, make_http(settings))


@pytest.mark.parametrize("provider,query_id", [
    ("pubmed", "topic:health_economics_policy"), ("europe_pmc", "topic:health_economics_policy"),
    ("openalex", "topic:ai_exploratory"), ("crossref", "journal:bmc_medicine"), ("medrxiv", "window:medrxiv")])
def test_provider_returns_well_formed_records(settings, providers, provider, query_id):
    spec = next(s for s in build_queries(settings) if s.provider == provider and s.query_id == query_id)
    end = utcnow().date() - timedelta(days=1)
    try:
        page = next(iter(providers[provider].fetch_window(spec, end - timedelta(days=2), end, None)))
    except ProviderError as exc:
        if exc.status == 429:
            pytest.skip(f"{provider} rate-limited/budget exhausted: {exc}")
        raise
    assert page.raw_count >= 0
    for r in page.records[:20]:
        assert r.identity_keys() and r.title


def test_live_identity_and_url(settings, providers):
    spec = next(s for s in build_queries(settings) if s.query_id == "journal:plos_medicine")
    end = utcnow().date() - timedelta(days=1)
    recs = [r for p in providers["crossref"].fetch_window(spec, end - timedelta(days=20), end, None) for r in p.records]
    if not recs:
        pytest.skip("no PLOS Medicine records in the window")
    from research_agent.metadata.schemas import Paper
    r = recs[0]
    paper = Paper(canonical_id=f"doi:{r.doi}", doi=r.doi, title=r.title, canonical_url=f"https://doi.org/{r.doi}")
    idv = IdentityVerifier(providers["crossref"], providers["pubmed"], providers["medrxiv"]).verify(paper)
    assert idv.status == "pass"
    lr = LinkVerifier().check(paper.canonical_url, doi=r.doi, title=r.title, identity_verified=True)
    assert lr.status in ("verified", "verified_via_authoritative_metadata")
