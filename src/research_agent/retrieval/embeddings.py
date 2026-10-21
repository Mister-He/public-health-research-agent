"""Stage-1b semantic similarity.

Default: dependency-free TF-IDF cosine between each paper and each profile-topic document
(description + keywords). It catches papers that describe a topic in unfamiliar terms
better than exact phrases, but it is a lexical-statistical proxy, not a neural embedding.
Optional: a local Ollama embedding model (local runs only; never assumed in CI)."""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Protocol

import httpx

from research_agent.settings import ResearchProfile

_TOKEN = re.compile(r"[a-z][a-z0-9]{2,}")
_STOP = set("""the and for with that this from are was were have has been their which these those using use
used into between among than also can may our its not but such more most other study studies paper results
based new two one analysis data model models method methods approach approaches health""".split())


def tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.casefold()) if t not in _STOP]


class Embedder(Protocol):
    def similarities(self, docs: list[str], profile: ResearchProfile) -> list[dict[str, float]]: ...


def topic_documents(profile: ResearchProfile) -> dict[str, str]:
    return {t.id: f"{t.name}. {t.description} " + " ".join(t.keywords) for t in profile.topics}


class TfidfEmbedder:
    """IDF is fitted on the screened batch plus topic documents, so results are a deterministic
    function of the batch."""

    def similarities(self, docs: list[str], profile: ResearchProfile) -> list[dict[str, float]]:
        tdocs = topic_documents(profile)
        all_tok = [tokens(d) for d in docs] + [tokens(d) for d in tdocs.values()]
        n = len(all_tok)
        df = Counter(t for toks in all_tok for t in set(toks))
        idf = {t: math.log((1 + n) / (1 + c)) + 1 for t, c in df.items()}

        def vec(toks: list[str]) -> dict[str, float]:
            tf = Counter(toks)
            v = {t: (1 + math.log(c)) * idf[t] for t, c in tf.items()}
            norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
            return {t: x / norm for t, x in v.items()}

        tvecs = {tid: vec(tokens(d)) for tid, d in tdocs.items()}
        out = []
        for toks in all_tok[: len(docs)]:
            v = vec(toks)
            out.append({tid: round(sum(v.get(t, 0) * x for t, x in tv.items()), 4) for tid, tv in tvecs.items()})
        return out


class OllamaEmbedder:
    """Local-only. Requires `ollama serve` on localhost; never exposes the endpoint."""

    def __init__(self, url: str, model: str):
        if not url.startswith(("http://127.0.0.1", "http://localhost")):
            raise ValueError("Ollama embeddings are restricted to a local endpoint")
        self.url = url.rstrip("/")
        self.model = model

    def _embed(self, texts: list[str]) -> list[list[float]]:
        r = httpx.post(f"{self.url}/api/embed", json={"model": self.model, "input": texts}, timeout=120)
        r.raise_for_status()
        return r.json()["embeddings"]

    def similarities(self, docs: list[str], profile: ResearchProfile) -> list[dict[str, float]]:
        tdocs = topic_documents(profile)
        tv = dict(zip(tdocs, self._embed(list(tdocs.values()))))
        out = []
        for e in self._embed(docs):
            out.append({tid: round(_cos(e, v), 4) for tid, v in tv.items()})
        return out


def _cos(a: list[float], b: list[float]) -> float:
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)
