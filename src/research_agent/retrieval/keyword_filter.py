"""Stage-1 lexical screening: phrase matches against the research profile.

Not a keyword count: each topic's evidence saturates (1 - exp(-w)), title matches weigh more
than abstract matches, and controlled-vocabulary (MeSH) agreement adds independent evidence."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from research_agent.metadata.normalization import normalize_text_for_match, phrase_in
from research_agent.metadata.schemas import Paper
from research_agent.settings import ResearchProfile

LOW_VALUE_TYPES = ("comment", "editorial", "letter", "news", "erratum", "correction", "retraction of publication")


@dataclass
class LexicalResult:
    score: float                               # 0..1, profile-level
    topic_scores: dict[str, float] = field(default_factory=dict)
    matched: dict[str, list[str]] = field(default_factory=dict)
    excluded_reason: str | None = None


def lexical_screen(paper: Paper, profile: ResearchProfile) -> LexicalResult:
    title = normalize_text_for_match(paper.title)
    body = normalize_text_for_match(" ".join([paper.abstract or "", " ".join(paper.author_keywords)]))
    everything = title + body
    for ex in profile.global_exclusions:
        if phrase_in(ex, title):
            return LexicalResult(0.0, excluded_reason=f"global exclusion: {ex}")
    mesh = {m.casefold() for m in paper.mesh_terms}
    res = LexicalResult(0.0)
    for t in profile.topics:
        if any(phrase_in(x, everything) for x in t.exclude):
            continue
        w = 0.0
        hits = []
        for kw in t.keywords:
            if phrase_in(kw, title):
                w += 1.0
                hits.append(kw)
            elif phrase_in(kw, body):
                w += 0.5
                hits.append(kw)
        mesh_hits = [m for m in t.mesh_terms if m.casefold() in mesh]
        w += 0.5 * len(mesh_hits)
        if w == 0:
            continue
        s = (1 - math.exp(-w / 1.2)) * t.importance
        res.topic_scores[t.id] = round(s, 4)
        res.matched[t.id] = hits + [f"MeSH:{m}" for m in mesh_hits]
    if res.topic_scores:
        ordered = sorted(res.topic_scores.values(), reverse=True)
        # Best topic dominates; a second distinct topic adds a little (methods x application).
        res.score = min(1.0, ordered[0] + 0.25 * (ordered[1] if len(ordered) > 1 else 0))
    ptype = (paper.publication_type or "").casefold()
    if any(x in ptype for x in LOW_VALUE_TYPES):
        res.score *= 0.3
    return res


def profile_category(topic_scores: dict[str, float], profile: ResearchProfile) -> str:
    """'core' if the strongest topic is core, else 'exploratory' ('none' without matches)."""
    if not topic_scores:
        return "none"
    best = max(sorted(topic_scores), key=lambda k: topic_scores[k])
    return profile.topic(best).kind
