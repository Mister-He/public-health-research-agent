"""Registry of non-LLM dimension evaluators.

Adding a dimension = (1) add it to config/scoring.yaml, (2) register an evaluator here (for
deterministic/hybrid) or add a rubric prompt (for llm), (3) its output is a DimensionScore,
(4) add a test. The aggregator never changes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from research_agent.llm.structured_output import PaperEvaluation
from research_agent.metadata.schemas import Paper


@dataclass
class ScoreContext:
    paper: Paper
    report_date: date
    evaluation: PaperEvaluation | None
    params: dict[str, Any]
    trend_scores: dict[str, float] = field(default_factory=dict)       # tag_id -> 0..1 (latest snapshot)
    lexical_tags: dict[str, float] = field(default_factory=dict)       # tag_id -> confidence (lexical match)
    trend_snapshot_id: int | None = None
    journal: dict | None = None                                        # scoring.journal.journal_context()


@dataclass
class DimensionScore:
    score: float | None                    # 0..100, None = missing
    method: str
    details: dict[str, Any] = field(default_factory=dict)
    confidence: float | None = None


Evaluator = Callable[[ScoreContext], DimensionScore]
_REGISTRY: dict[str, Evaluator] = {}


def register(name: str) -> Callable[[Evaluator], Evaluator]:
    def deco(fn: Evaluator) -> Evaluator:
        if name in _REGISTRY:
            raise ValueError(f"evaluator {name!r} already registered")
        _REGISTRY[name] = fn
        return fn
    return deco


def get_evaluator(name: str) -> Evaluator:
    from research_agent.scoring import evaluators  # noqa: F401  (registers built-ins)

    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise KeyError(f"no evaluator registered as {name!r}; known: {sorted(_REGISTRY)}") from exc


def registered() -> list[str]:
    from research_agent.scoring import evaluators  # noqa: F401

    return sorted(_REGISTRY)
