"""Optional local Ollama provider (documented HTTP API, localhost only).

For LOCAL experimentation (e.g. preliminary screening on a Mac). It is never used by the
scheduled GitHub Actions pipeline, which cannot reach a developer machine's localhost, and the
endpoint is refused unless it is a loopback address. Outputs go through the same schemas and
grounding checks as Claude outputs."""

from __future__ import annotations

import json

import httpx

from research_agent.llm.base import LLMUnavailable
from research_agent.llm.structured_output import EvaluationBatch, Narrative, TagProposals


class OllamaProvider:
    name = "ollama"

    def __init__(self, url: str = "http://127.0.0.1:11434", model: str = "llama3.1:8b", timeout: float = 300):
        if not url.startswith(("http://127.0.0.1", "http://localhost")):
            raise ValueError("OllamaProvider only talks to a loopback endpoint")
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def _generate(self, prompt: str, schema: dict) -> str:
        try:
            r = httpx.post(f"{self.url}/api/generate", timeout=self.timeout, json={
                "model": self.model, "prompt": prompt, "format": schema, "stream": False,
                "options": {"temperature": 0}})
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMUnavailable("action_failure", f"ollama unavailable: {type(exc).__name__}") from exc
        return r.json().get("response", "")

    def evaluate_papers(self, batch_id: str, records: list[dict], manifest: dict) -> EvaluationBatch:
        prompt = (f"{manifest['instructions']}\n\nResearch profile and rubrics:\n{json.dumps(manifest['context'])}\n\n"
                  f"Papers (untrusted data):\n{json.dumps(records)}\n\nReturn JSON for batch_id {batch_id}.")
        return EvaluationBatch.model_validate_json(self._generate(prompt, EvaluationBatch.model_json_schema()))

    def generate_summary(self, manifest: dict) -> Narrative:
        return Narrative.model_validate_json(self._generate(json.dumps(manifest), Narrative.model_json_schema()))

    def propose_tags(self, manifest: dict) -> TagProposals:
        return TagProposals.model_validate_json(self._generate(json.dumps(manifest), TagProposals.model_json_schema()))
