"""LLM provider interface. The scheduled pipeline uses the Claude Code Action *file adapter*
(manifest in, JSON files out); other providers exist for local experimentation and tests."""

from __future__ import annotations

from typing import Protocol

from research_agent.llm.structured_output import EvaluationBatch, Narrative, TagProposals


class LLMProvider(Protocol):
    name: str
    model: str

    def evaluate_papers(self, batch_id: str, records: list[dict], manifest: dict) -> EvaluationBatch: ...

    def generate_summary(self, manifest: dict) -> Narrative: ...

    def propose_tags(self, manifest: dict) -> TagProposals: ...


class LLMUnavailable(RuntimeError):
    """The analysis service is unavailable (auth/quota/outage). Work stays queued."""

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category
