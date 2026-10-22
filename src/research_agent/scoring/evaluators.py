"""Built-in deterministic and hybrid evaluators."""

from __future__ import annotations

import math

from research_agent.scoring.registry import DimensionScore, ScoreContext, register


@register("freshness")
def freshness(ctx: ScoreContext) -> DimensionScore:
    """100 * 0.5 ** (age / half_life) on the verified first-publication date (smooth decay,
    no cutoffs). Future-dated papers (e.g. issue dates ahead of online release) count as age 0."""
    d = ctx.paper.first_publication_date
    if d is None:
        return DimensionScore(None, "deterministic", {"reason": "no verified publication date"})
    half_life = float(ctx.params.get("half_life_days", 14))
    age = max(0, (ctx.report_date - d).days)
    score = 100.0 * math.pow(0.5, age / half_life)
    return DimensionScore(round(score, 6), "deterministic",
                          {"age_days": age, "half_life_days": half_life, "date_used": d.isoformat()}, 1.0)


@register("trend_relevance")
def trend_relevance(ctx: ScoreContext) -> DimensionScore:
    """Hybrid: tag membership from Claude (confidence-weighted) and deterministic lexical tag
    matches; strength from the trend snapshot. Combined as a probabilistic OR so several weak
    trends cannot exceed one strong one by much: 100 * (1 - prod(1 - c_t * s_t))."""
    if ctx.trend_snapshot_id is None:
        return DimensionScore(None, "hybrid", {"reason": "no valid trend snapshot"})
    member: dict[str, float] = dict(ctx.lexical_tags)
    if ctx.evaluation:
        for t in ctx.evaluation.topic_tags:
            member[t.tag_id] = max(member.get(t.tag_id, 0.0), t.confidence)
    contributions = {}
    prod = 1.0
    for tag, conf in sorted(member.items()):
        s = ctx.trend_scores.get(tag)
        if s is None:
            continue
        c = conf * s
        contributions[tag] = round(c, 6)
        prod *= 1 - c
    score = 100.0 * (1 - prod)
    return DimensionScore(round(score, 6), "hybrid", {"tags": contributions, "snapshot_id": ctx.trend_snapshot_id},
                          None)


@register("journal_impact")
def journal_impact(ctx: ScoreContext) -> DimensionScore:
    """Open impact-factor analogue (see scoring/journal.py); never mixed into methodological rigor."""
    from research_agent.scoring.journal import describe, impact_score

    if ctx.journal is None:
        return DimensionScore(None, "deterministic", {"reason": "no journal context"})
    return DimensionScore(impact_score(ctx.journal, ctx.params), "deterministic",
                          {**ctx.journal, "description": describe(ctx.journal)}, 1.0)
