"""Deterministic composite scoring. Claude never computes the weighted score.

overall = sum(weight_i * score_i) over enabled dimensions, with each dimension's
missing_data_policy: reject (paper unrankable), zero, or exclude_and_renormalize."""

from __future__ import annotations

from dataclasses import dataclass, field

from research_agent.scoring.registry import DimensionScore, ScoreContext, get_evaluator
from research_agent.settings import ScoringConfig


class Unscorable(ValueError):
    pass


@dataclass
class CompositeScore:
    overall: float
    dimensions: dict[str, dict] = field(default_factory=dict)
    scoring_version: str = ""


def score_dimensions(cfg: ScoringConfig, ctx: ScoreContext) -> dict[str, DimensionScore]:
    out: dict[str, DimensionScore] = {}
    for d in cfg.enabled:
        if d.method == "llm":
            a = ctx.evaluation.dimensions.get(d.id) if ctx.evaluation else None
            out[d.id] = DimensionScore(a.score if a else None, "llm",
                                       {"rubric_version": d.rubric_version}, a.confidence if a else None)
        else:
            ev = get_evaluator(d.evaluator)  # type: ignore[arg-type]
            out[d.id] = ev(ScoreContext(**{**ctx.__dict__, "params": d.params}))
        s = out[d.id].score
        lo, hi = d.score_range
        if s is not None and not (lo <= s <= hi):
            raise Unscorable(f"{d.id} score {s} outside {d.score_range}")
    return out


def combine(cfg: ScoringConfig, dims: dict[str, DimensionScore]) -> CompositeScore:
    total_w = 0.0
    acc = 0.0
    detail: dict[str, dict] = {}
    for d in cfg.enabled:
        ds = dims.get(d.id)
        s = ds.score if ds else None
        if s is None:
            if d.missing_data_policy == "reject":
                raise Unscorable(f"missing required dimension {d.id}")
            if d.missing_data_policy == "zero":
                s = 0.0
            else:  # exclude_and_renormalize
                detail[d.id] = {"score": None, "weight": d.weight, "method": d.method, "excluded": True}
                continue
        acc += d.weight * s
        total_w += d.weight
        detail[d.id] = {"score": round(s, 6), "weight": d.weight, "method": d.method,
                        "confidence": ds.confidence if ds else None, "details": ds.details if ds else {}}
    if total_w <= 0:
        raise Unscorable("no dimensions available")
    overall = acc / total_w if abs(total_w - 1.0) > 1e-12 else acc
    return CompositeScore(round(overall, 6), detail, cfg.version())


def recompute_overall(cfg_weights: dict[str, float], stored: dict[str, dict]) -> float:
    """Independent recomputation used by the validator (from stored per-dimension scores)."""
    acc = 0.0
    tw = 0.0
    for dim_id, w in cfg_weights.items():
        s = stored.get(dim_id, {}).get("score")
        if s is None:
            if stored.get(dim_id, {}).get("excluded"):
                continue
            raise Unscorable(f"missing {dim_id}")
        acc += w * s
        tw += w
    return acc / tw if abs(tw - 1.0) > 1e-12 else acc
