"""Clusters of recent, profile-relevant papers that match NO existing tag — the input for
weekly Claude tag proposals. Greedy single-pass TF-IDF clustering (deterministic order)."""

from __future__ import annotations

import math
from collections import Counter

from research_agent.retrieval.embeddings import tokens


def cluster_unmatched(docs: list[dict], *, min_cluster_size: int, threshold: float = 0.22,
                      max_clusters: int = 6, max_papers: int = 15) -> list[dict]:
    """docs: [{canonical_id, title, keywords, text}] sorted deterministically by the caller."""
    toks = [tokens(f"{d['title'] or ''} {' '.join(d.get('keywords') or [])} {d['text']}") for d in docs]
    n = len(toks) or 1
    df = Counter(t for ts in toks for t in set(ts))
    idf = {t: math.log((1 + n) / (1 + c)) + 1 for t, c in df.items()}

    def vec(ts):
        tf = Counter(ts)
        v = {t: (1 + math.log(c)) * idf[t] for t, c in tf.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1
        return {t: x / norm for t, x in v.items()}

    vecs = [vec(t) for t in toks]
    clusters: list[dict] = []
    for i, v in enumerate(vecs):
        best, best_sim = None, threshold
        for c in clusters:
            sim = sum(v.get(t, 0) * x for t, x in c["centroid"].items())
            if sim > best_sim:
                best, best_sim = c, sim
        if best is None:
            clusters.append({"members": [i], "centroid": dict(v)})
        else:
            best["members"].append(i)
            k = len(best["members"])
            cen = best["centroid"]
            for t in set(cen) | set(v):
                cen[t] = cen.get(t, 0) * (k - 1) / k + v.get(t, 0) / k
    out = []
    for c in sorted(clusters, key=lambda c: -len(c["members"])):
        if len(c["members"]) < min_cluster_size:
            continue
        top = [t for t, _ in sorted(c["centroid"].items(), key=lambda kv: (-kv[1], kv[0]))[:10]]
        out.append({"size": len(c["members"]), "top_terms": top,
                    "papers": [{"canonical_id": docs[i]["canonical_id"], "title": docs[i]["title"],
                                "keywords": docs[i].get("keywords") or []} for i in c["members"][:max_papers]]})
        if len(out) >= max_clusters:
            break
    return out
