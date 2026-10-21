"""Provider-specific query construction from search_queries.yaml and journals.yaml.

Each QuerySpec has a stable id and a hash of its effective query text: the checkpoint for a
(provider, query_id) is reset only when the query text itself changes.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from research_agent.settings import QueryGroup, Settings


@dataclass(frozen=True)
class QuerySpec:
    provider: str
    query_id: str
    strategy: str                       # topic | journal | window
    text: str                           # provider-native query string ("" for window dumps)
    params: dict = field(default_factory=dict, hash=False, compare=False)

    @property
    def query_hash(self) -> str:
        return hashlib.sha256(f"{self.provider}|{self.text}|{sorted(self.params.items())}".encode()).hexdigest()[:12]


def _q(term: str) -> str:
    return term.replace('"', "").strip()


def _strip_star(t: str) -> str:
    return t.rstrip("*")


def pubmed_query(g: QueryGroup) -> str:
    terms = " OR ".join(f'"{_q(t)}"[tiab]' for t in g.terms)
    core = f"({terms})"
    if g.context:
        ctx = " OR ".join(f"{_q(c)}[tiab]" if c.endswith("*") else f'"{_q(c)}"[tiab]' for c in g.context)
        core = f"({core} AND ({ctx}))"
    if g.mesh:
        mesh = " OR ".join(f'"{_q(m)}"[mh]' for m in g.mesh)
        part = f"({mesh})"
        if g.mesh_anchor:
            anchor = " OR ".join(f'"{_q(m)}"[mh]' for m in g.mesh_anchor)
            part = f"({part} AND ({anchor}))"
        core = f"({core} OR {part})"
    return core


def europe_pmc_query(g: QueryGroup) -> str:
    terms = " OR ".join(f'TITLE_ABS:"{_q(t)}"' for t in g.terms)
    core = f"({terms})"
    if g.context:
        ctx = " OR ".join(f"TITLE_ABS:{_q(c)}" if c.endswith("*") else f'TITLE_ABS:"{_q(c)}"' for c in g.context)
        core = f"({core} AND ({ctx}))"
    if g.mesh:
        mesh = " OR ".join(f'MESH:"{_q(m)}"' for m in g.mesh)
        part = f"({mesh})"
        if g.mesh_anchor:
            part = f"({part} AND ({' OR '.join(f'MESH:{chr(34)}{_q(m)}{chr(34)}' for m in g.mesh_anchor)}))"
        core = f"({core} OR {part})"
    return core


def openalex_query(g: QueryGroup) -> str:
    """Value for the `title_and_abstract.search` filter (supports quoted phrases + OR/AND)."""
    terms = " OR ".join(f'"{_q(t)}"' for t in g.terms)
    core = f"({terms})"
    if g.context:
        ctx = " OR ".join(_q(_strip_star(c)) if c.endswith("*") else f'"{_q(c)}"' for c in g.context)
        core = f"{core} AND ({ctx})"
    return core


def build_queries(settings: Settings) -> list[QuerySpec]:
    specs: list[QuerySpec] = []
    src = settings.sources.sources
    # Strategy A: topic-driven
    for g in settings.queries.groups:
        for prov in g.providers or settings.queries.providers:
            if not src.get(prov) or not src[prov].enabled:
                continue
            text = {"pubmed": pubmed_query, "europe_pmc": europe_pmc_query, "openalex": openalex_query}[prov](g)
            specs.append(QuerySpec(prov, f"topic:{g.id}", "topic", text))
    # medRxiv has no search API: full window dump, screened locally.
    if src["medrxiv"].enabled:
        specs.append(QuerySpec("medrxiv", "window:medrxiv", "window", ""))
    if src.get("arxiv") and src["arxiv"].enabled:
        cats = getattr(src["arxiv"], "categories", None) or src["arxiv"].model_extra.get("categories", [])
        terms = [t for g in settings.queries.groups if g.id in ("mathematical_modelling", "ai_exploratory")
                 for t in g.terms]
        cat = " OR ".join(f"cat:{c}" for c in cats)
        txt = " OR ".join(f'abs:"{_q(t)}"' for t in terms)
        specs.append(QuerySpec("arxiv", "topic:arxiv_methods", "topic", f"({cat}) AND ({txt})"))
    # Strategy B: journal-driven (Crossref, by ISSN)
    if src["crossref"].enabled:
        for j in settings.journals.journals:
            if j.enabled:
                specs.append(QuerySpec("crossref", f"journal:{j.id}", "journal", ",".join(j.issns),
                                       params={"issns": tuple(j.issns), "name": j.name}))
    return specs
