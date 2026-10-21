"""Pure, reproducible trend statistics.

For each tag, over the topic-filtered corpus (not only recommended papers):
  p_recent   = (c_r + a) / (N_r + 2a)          additive smoothing (no divide-by-zero)
  p_baseline = (c_b + a) / (N_b + 2a)
  frequency  = 1 - exp(-p_recent / s)          popularity, absolute (0..1)
  growth     = logistic(ln(p_recent / p_baseline) / k)   0.5 = no change; UNAVAILABLE when
               c_r + c_b < min_sample or N_b < min_sample (small-sample safeguard)
  external   = logistic(ln(share_recent / share_prior) / k) from OpenAlex counts, or UNAVAILABLE
  raw        = weighted sum over AVAILABLE components, weights renormalized
  score      = ema_alpha * raw + (1 - ema_alpha) * previous_score   (no one-day spike dominates)
Popularity (frequency) and growth are reported separately.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field


def logistic(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def wilson(c: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return 0.0, 1.0
    p = c / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass
class TagCounts:
    tag_id: str
    recent: int
    recent_corpus: int
    baseline: int
    baseline_corpus: int
    windows: dict[int, tuple[int, int]] = field(default_factory=dict)   # days -> (tagged, corpus)
    supporting_ids: list[str] = field(default_factory=list)


@dataclass
class TagTrend:
    tag_id: str
    frequency_signal: float
    growth_signal: float | None
    external_signal: float | None
    raw_score: float
    score: float
    previous_score: float | None
    p_recent: float
    p_baseline: float
    recent_ci95: tuple[float, float]
    counts: dict
    available: list[str]
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return asdict(self)


def compute_trend(c: TagCounts, *, weights: dict[str, float], pseudo_count: float, frequency_scale: float,
                  growth_scale: float, min_sample: int, ema_alpha: float, previous: float | None,
                  external: float | None, manual_override: float | None = None) -> TagTrend:
    a = pseudo_count
    p_r = (c.recent + a) / (c.recent_corpus + 2 * a)
    p_b = (c.baseline + a) / (c.baseline_corpus + 2 * a)
    freq = 1 - math.exp(-p_r / frequency_scale)
    notes = []
    growth = None
    if c.recent + c.baseline >= min_sample and c.baseline_corpus >= min_sample and c.recent_corpus >= min_sample:
        growth = logistic(math.log(p_r / p_b) / growth_scale)
    else:
        notes.append(f"growth unavailable: tagged={c.recent + c.baseline}, baseline corpus={c.baseline_corpus} "
                     f"(< min_sample {min_sample})")
    comps = {"frequency": freq, "growth": growth, "external": external}
    avail = {k: v for k, v in comps.items() if v is not None}
    wsum = sum(weights[k] for k in avail)
    raw = sum(weights[k] * v for k, v in avail.items()) / wsum if wsum > 0 else 0.0
    score = raw if previous is None else ema_alpha * raw + (1 - ema_alpha) * previous
    if manual_override is not None:
        notes.append(f"manual override {manual_override} applied (computed {score:.4f})")
        score = manual_override
    return TagTrend(
        tag_id=c.tag_id, frequency_signal=round(freq, 6), growth_signal=None if growth is None else round(growth, 6),
        external_signal=None if external is None else round(external, 6), raw_score=round(raw, 6),
        score=round(score, 6), previous_score=previous, p_recent=round(p_r, 6), p_baseline=round(p_b, 6),
        recent_ci95=tuple(round(x, 6) for x in wilson(c.recent, c.recent_corpus)),  # type: ignore[arg-type]
        counts={"recent": c.recent, "recent_corpus": c.recent_corpus, "baseline": c.baseline,
                "baseline_corpus": c.baseline_corpus,
                "windows": {str(k): {"tagged": v[0], "corpus": v[1]} for k, v in sorted(c.windows.items())}},
        available=sorted(avail), notes=notes)


def external_signal(recent_count: int, recent_total: int, prior_count: int, prior_total: int, *,
                    pseudo_count: float, growth_scale: float, min_sample: int) -> float | None:
    """Field-wide growth from OpenAlex, normalized by total works in each window (corrects for
    indexing lag in the most recent window)."""
    if recent_total <= 0 or prior_total <= 0 or recent_count + prior_count < min_sample:
        return None
    a = pseudo_count
    s_r = (recent_count + a) / (recent_total + 2 * a)
    s_p = (prior_count + a) / (prior_total + 2 * a)
    return logistic(math.log(s_r / s_p) / growth_scale)
