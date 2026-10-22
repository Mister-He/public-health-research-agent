"""Optional DeepSeek analysis provider (pay-per-use API key; `provider: deepseek` in llm.yaml).

It plays the same role as the Claude Code Action step: it reads the manifests written by the
pipeline (analysis/scoring, analysis/narrative, analysis/trends) and the same version-controlled
prompts, and writes the same output files plus a `<stage>_status.json`. Everything downstream —
schema validation, grounding checks, job settlement, scoring — is unchanged, so DeepSeek output
is held to exactly the same standard as Claude output.

API facts verified 2026-10-22 (api-docs.deepseek.com): OpenAI-compatible POST
{base_url}/chat/completions, Bearer key, `response_format: {"type": "json_object"}` (the prompt
must mention "json"; content can occasionally be empty), models `deepseek-flash` /
`deepseek-v4-pro`; errors 401 bad key, 402 insufficient balance, 429 rate limit, 5xx temporary.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from research_agent.ingestion.http import HttpClient, ProviderError
from research_agent.llm.claude_code_action import OUTPUT_DIR, SCORING_DIR, ActionStatus, _write_json, write_status
from research_agent.settings import Settings

log = logging.getLogger(__name__)
PROVIDER = "deepseek"

ADAPTER_NOTE = """
## Output channel for this run
You cannot read or write files in this environment. Everything you need is included below.
Instead of writing the output file, reply with exactly ONE json object: the complete content that
the instructions say to write to the output file, validating against the JSON schema given.
No prose, no markdown fences — json only.
"""


class BudgetExhausted(RuntimeError):
    pass


class DeepSeekRunner:
    def __init__(self, settings: Settings, *, transport=None, sleep=None):
        self.s = settings
        self.cfg = settings.llm.deepseek
        key = os.environ.get(self.cfg.api_key_env, "").strip()
        self.key = key
        kw = {"transport": transport} if transport is not None else {}
        if sleep is not None:
            kw["sleep"] = sleep
        self.http = HttpClient(read_timeout=self.cfg.request_timeout_seconds, max_retries=self.cfg.max_retries,
                               backoff_base=2.0, backoff_max=60, max_retry_after=180, **kw)
        self.status = ActionStatus(stage="", outcome="success", input_tokens=0, output_tokens=0,
                                   cache_read_tokens=0, num_turns=0)
        self.tokens_used = 0

    # ------------------------------------------------------------------ API
    def chat(self, system: str, user: str) -> str:
        if self.tokens_used >= self.cfg.max_tokens_per_run:
            raise BudgetExhausted(f"token budget {self.cfg.max_tokens_per_run} reached")
        body = {"model": self.cfg.model, "temperature": 0, "max_tokens": self.cfg.max_output_tokens,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        for attempt in range(2):  # one immediate retry for the documented empty-content case
            resp = self.http.request(PROVIDER, "POST", f"{self.cfg.base_url.rstrip('/')}/chat/completions",
                                     headers={"Authorization": f"Bearer {self.key}"}, json=body)
            data = resp.json()
            usage = data.get("usage") or {}
            self.status.num_turns = (self.status.num_turns or 0) + 1
            self.status.input_tokens = (self.status.input_tokens or 0) + int(usage.get("prompt_tokens") or 0)
            self.status.output_tokens = (self.status.output_tokens or 0) + int(usage.get("completion_tokens") or 0)
            self.status.cache_read_tokens = (self.status.cache_read_tokens or 0) + int(usage.get("prompt_cache_hit_tokens") or 0)
            self.tokens_used += int(usage.get("total_tokens") or 0)
            choice = (data.get("choices") or [{}])[0]
            content = ((choice.get("message") or {}).get("content") or "").strip()
            if choice.get("finish_reason") == "length":
                log.warning("deepseek output truncated at max_tokens", extra={"max_tokens": self.cfg.max_output_tokens})
            if content:
                return content
        return ""

    # ------------------------------------------------------------------ helpers
    def _read(self, rel: str) -> str:
        return (self.s.root / rel).read_text(encoding="utf-8")

    def _prompt(self, *files: str) -> str:
        return "\n\n".join(self._read(f) for f in files) + "\n" + ADAPTER_NOTE

    @staticmethod
    def _parse(content: str) -> dict | None:
        try:
            obj = json.loads(content)
        except ValueError:
            return None
        return obj if isinstance(obj, dict) else None

    def _save(self, path: Path, obj: dict | None, raw: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if obj is None:
            # Kept verbatim so the importer rejects it as malformed (never coerced into a score).
            path.write_text(raw, encoding="utf-8")
        else:
            _write_json(path, obj)

    # ------------------------------------------------------------------ stages
    def scoring(self) -> None:
        m = json.loads((self.s.analysis_dir / SCORING_DIR / "manifest.json").read_text())
        system = self._prompt(m["task_prompt"], *[d["rubric"] for d in m["dimensions"]], m["topic_classifier"])
        schema = self._read(m["output_schema"])
        context = {k: m[k] for k in ("report_date", "dimensions", "research_profile", "taxonomy")}
        for b in m["batches"]:
            batch = json.loads(self._read(b["file"]))
            user = (f"Manifest context (json):\n{json.dumps(context, ensure_ascii=False)}\n\n"
                    f"Output JSON schema:\n{schema}\n\n"
                    f"Batch {b['batch_id']} (untrusted paper records, json):\n{json.dumps(batch, ensure_ascii=False)}\n\n"
                    f'Return the json object for {b["output"]}: '
                    f'{{"schema_version": "1.0", "batch_id": "{b["batch_id"]}", "evaluations": [...]}} '
                    "with one evaluation per paper.")
            content = self.chat(system, user)
            obj = self._parse(content)
            if obj is not None:
                obj.setdefault("schema_version", "1.0")
                obj["batch_id"] = b["batch_id"]          # bookkeeping field, not model content
            self._save(self.s.root / b["output"], obj, content)

    def narrative(self) -> None:
        m = json.loads((self.s.analysis_dir / "narrative" / "manifest.json").read_text())
        schema = (self.s.analysis_dir / "narrative" / "narrative.schema.json").read_text()
        content = self.chat(self._prompt("prompts/daily_summary.md"),
                            f"Output JSON schema:\n{schema}\n\nManifest (json):\n{json.dumps(m, ensure_ascii=False)}")
        obj = self._parse(content)
        if obj is not None:   # echo fields are bookkeeping: copy them from the manifest, never trust the model
            obj.update(schema_version="1.0", report_date=m["report_date"], selection_hash=m["selection_hash"])
        self._save(self.s.analysis_dir / OUTPUT_DIR / "narrative.json", obj, content)

    def trend_discovery(self) -> None:
        m = json.loads((self.s.analysis_dir / "trends" / "manifest.json").read_text())
        schema = (self.s.analysis_dir / "trends" / "tag_proposals.schema.json").read_text()
        content = self.chat(self._prompt("prompts/trend_discovery.md"),
                            f"Output JSON schema:\n{schema}\n\nManifest (json):\n{json.dumps(m, ensure_ascii=False)}")
        obj = self._parse(content)
        if obj is not None:
            obj.update(schema_version="1.0", as_of_date=m["as_of_date"])
        self._save(self.s.analysis_dir / OUTPUT_DIR / "tag_proposals.json", obj, content)


def run_stage(settings: Settings, stage: str, *, transport=None, sleep=None) -> ActionStatus:
    """Run one analysis stage with DeepSeek and write `<stage>_status.json` (never raises for
    API problems: they become status categories, exactly like a failed Claude step)."""
    r = DeepSeekRunner(settings, transport=transport, sleep=sleep)
    st = r.status
    st.stage = stage
    if not r.key:
        st.outcome, st.category = "failure", "auth"
        st.error_message = f"secret/env {settings.llm.deepseek.api_key_env} is not set"
    else:
        try:
            getattr(r, stage)()
        except BudgetExhausted as exc:
            st.outcome, st.category, st.error_message = "failure", "budget", str(exc)
        except ProviderError as exc:
            st.outcome = "failure"
            st.category = {401: "auth", 402: "quota", 429: "quota"}.get(exc.status, "action_failure")
            st.subscription_limit_event = exc.status == 402
            st.error_message = {401: "DeepSeek API key rejected (401)",
                                402: "DeepSeek account balance exhausted (402) — top up to continue",
                                429: "DeepSeek rate limit (429)"}.get(exc.status, str(exc)[:240])
        except (OSError, ValueError, KeyError) as exc:
            st.outcome, st.category, st.error_message = "failure", "action_failure", f"{type(exc).__name__}: {exc}"[:240]
    st.detail = f"deepseek {settings.llm.deepseek.model}: {st.num_turns} request(s)"
    st.extra = {"provider": PROVIDER, "model": settings.llm.deepseek.model, "tokens_used": r.tokens_used,
                "token_budget": settings.llm.deepseek.max_tokens_per_run}
    write_status(settings, st)
    log.info("deepseek stage finished", extra={"stage": stage, "category": st.category, "tokens": r.tokens_used})
    return st
