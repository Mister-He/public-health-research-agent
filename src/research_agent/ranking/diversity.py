"""Bounded diversity re-ranking (MMR-style). The raw score stays the primary signal:
adjusted = raw - lambda * 100 * max_jaccard(tags, already selected). With lambda = 0.10 a paper
can lose at most 10 points, so diversity can only reorder near-ties, never replace quality."""

from __future__ import annotations


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def penalty(tags: frozenset[str], selected: list[frozenset[str]], lam: float) -> float:
    if not selected or lam <= 0:
        return 0.0
    return lam * 100.0 * max(jaccard(tags, s) for s in selected)
