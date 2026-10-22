"""DeepSeek provider against a mocked OpenAI-compatible API (no network, no charges)."""

from __future__ import annotations

import json

import httpx
import pytest
import yaml
from fixtures.synthetic import REPORT_DATE, records

from research_agent.llm import claude_code_action as cca
from research_agent.llm.deepseek_provider import run_stage
from research_agent.llm.fixture_provider import _evaluate
from research_agent.metadata.deduplication import ingest_record
from research_agent.retrieval.screening import prescreen, shortlist_and_enqueue
from research_agent.settings import ConfigError, load_settings


@pytest.fixture
def ds(settings, monkeypatch):
    s = settings.model_copy(deep=True)
    s.llm.provider, s.llm.auth = "deepseek", "api_key"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek-key")
    return s


def _queue(db, s, limit=None):
    ids = [ingest_record(db, r).paper_id for r in records()]
    prescreen(db, s, ids)
    shortlist_and_enqueue(db, s, REPORT_DATE)
    return cca.build_scoring_manifest(db, s, run_id="run1", report_date=REPORT_DATE, limit=limit)


def _api(s, *, status=200, content=None, seen=None):
    """Mock /chat/completions: answers each batch with schema-valid, grounded evaluations."""
    taxonomy = {t.id: t for t in s.trends.tags if t.status in ("active", "watchlist")}

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        if seen is not None:
            seen.append({"auth": req.headers.get("authorization"), "body": body, "path": req.url.path})
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "mock error"}})
        if content is not None:
            text = content
        else:
            user = body["messages"][1]["content"]
            batch = json.loads(user.split("(untrusted paper records, json):\n", 1)[1].split("\n\nReturn the json", 1)[0])
            text = json.dumps({"evaluations": [_evaluate(p, s, taxonomy) for p in batch["papers"]]})
        return httpx.Response(200, json={"choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 1000, "completion_tokens": 500,
                                                   "total_tokens": 1500, "prompt_cache_hit_tokens": 200}})
    return httpx.MockTransport(handler)


def test_success_goes_through_the_same_validation_and_import(db, ds):
    m = _queue(db, ds)
    seen = []
    st = run_stage(ds, "scoring", transport=_api(ds, seen=seen), sleep=lambda s: None)
    assert st.category is None and st.num_turns == len(m["batches"])
    assert st.input_tokens == 1000 * len(m["batches"]) and st.output_tokens == 500 * len(m["batches"])
    req = seen[0]
    assert req["path"] == "/chat/completions" and req["auth"] == "Bearer sk-test-deepseek-key"
    assert req["body"]["model"] == "deepseek-flash" and req["body"]["response_format"] == {"type": "json_object"}
    assert "json" in req["body"]["messages"][0]["content"]          # JSON mode requires the word in the prompt
    imp = cca.import_scoring_outputs(db, ds, run_id="run1", status=cca.read_status(ds, "scoring"))
    assert imp.completed == m["paper_count"] and not imp.failed
    assert db.scalar("SELECT DISTINCT model FROM evaluations") == "deepseek-flash"   # separate cache key from Claude


@pytest.mark.parametrize("code,category", [(401, "auth"), (402, "quota")])
def test_auth_and_balance_errors_keep_the_backlog(db, ds, code, category):
    m = _queue(db, ds)
    st = run_stage(ds, "scoring", transport=_api(ds, status=code), sleep=lambda s: None)
    assert st.category == category and st.error_message
    imp = cca.import_scoring_outputs(db, ds, run_id="run1", status=cca.read_status(ds, "scoring"))
    assert imp.failed == {category: m["paper_count"]} and imp.completed == 0
    assert db.scalar("SELECT COUNT(*) FROM analysis_jobs WHERE status='retryable_failed' AND format_failures=0") \
        == m["paper_count"]


def test_malformed_output_is_rejected_not_coerced(db, ds):
    m = _queue(db, ds)
    st = run_stage(ds, "scoring", transport=_api(ds, content="Sure! Here are the scores: 85, 90"), sleep=lambda s: None)
    assert st.category is None                                           # the API worked…
    imp = cca.import_scoring_outputs(db, ds, run_id="run1", status=st)
    assert imp.completed == 0 and imp.failed.get("malformed") == m["paper_count"]   # …but nothing is accepted


def test_token_budget_stops_cleanly(db, ds):
    m = _queue(db, ds)
    assert len(m["batches"]) >= 2
    ds.llm.deepseek.max_tokens_per_run = 1500                           # one request's worth
    st = run_stage(ds, "scoring", transport=_api(ds), sleep=lambda s: None)
    assert st.category == "budget" and st.num_turns == 1
    imp = cca.import_scoring_outputs(db, ds, run_id="run1", status=st)
    first = len(m["batches"][0]["job_ids"])
    assert imp.completed == first and imp.failed == {"budget": m["paper_count"] - first}


def test_missing_key_is_reported_as_auth(db, ds, monkeypatch):
    _queue(db, ds)
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    st = run_stage(ds, "scoring", transport=_api(ds), sleep=lambda s: None)
    assert st.category == "auth" and "DEEPSEEK_API_KEY" in st.error_message and st.num_turns == 0


def test_narrative_echo_fields_come_from_the_manifest(ds):
    cca.write_narrative_manifest(ds, {"report_date": "2026-10-21", "selection_hash": "abc", "papers": []})
    text = json.dumps({"report_date": "1999-01-01", "selection_hash": "wrong", "narrative": "word " * 200})
    run_stage(ds, "narrative", transport=_api(ds, content=text), sleep=lambda s: None)
    out = json.loads((ds.analysis_dir / "output" / "narrative.json").read_text())
    assert out["report_date"] == "2026-10-21" and out["selection_hash"] == "abc" and out["schema_version"] == "1.0"


def test_config_requires_explicit_api_key_auth(project):
    p = project / "config" / "llm.yaml"
    data = yaml.safe_load(p.read_text())
    data["llm"]["provider"] = "deepseek"                               # auth still oauth_subscription
    p.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="api_key"):
        load_settings(project)
    data["llm"]["auth"] = "api_key"
    p.write_text(yaml.safe_dump(data))
    s = load_settings(project)
    assert s.llm.active_model == "deepseek-flash" and s.llm.allow_api_key_fallback is False
