"""Claude Code Action file adapter: manifests, mocked action outputs and failure statuses."""

from __future__ import annotations

import json

import pytest
from fixtures.synthetic import REPORT_DATE, records

from research_agent.llm import claude_code_action as cca
from research_agent.llm.fixture_provider import simulate_scoring
from research_agent.metadata.deduplication import ingest_record
from research_agent.retrieval.screening import prescreen, shortlist_and_enqueue
from research_agent.storage import analysis_queue as q


def _setup(db, settings, limit=None):
    ids = [ingest_record(db, r).paper_id for r in records()]
    prescreen(db, settings, ids)
    shortlist_and_enqueue(db, settings, REPORT_DATE)
    return cca.build_scoring_manifest(db, settings, run_id="run1", report_date=REPORT_DATE, limit=limit)


def exec_file(tmp_path, msgs):
    p = tmp_path / "execution.json"
    p.write_text(json.dumps(msgs))
    return p


def test_manifest_is_bounded_batched_and_leased(db, settings):
    m = _setup(db, settings, limit=5)
    assert m["paper_count"] == 5 and len(m["batches"]) == 1
    assert db.scalar("SELECT COUNT(*) FROM analysis_jobs WHERE status='in_progress' AND leased_by_run='run1'") == 5
    batch = json.loads((settings.root / m["batches"][0]["file"]).read_text())
    assert "untrusted" in batch["note"]
    p = batch["papers"][0]
    assert set(p) >= {"canonical_id", "metadata_version", "title", "abstract", "author_keywords", "mesh_terms"}
    schema = json.loads((settings.analysis_dir / "scoring" / "evaluation_batch.schema.json").read_text())
    assert "PaperEvaluation" in json.dumps(schema)
    assert {d["id"] for d in m["dimensions"]} == {d.id for d in settings.scoring.llm_dimensions}


def test_import_valid_outputs_and_cache(db, settings):
    m = _setup(db, settings)
    simulate_scoring(settings)
    imp = cca.import_scoring_outputs(db, settings, run_id="run1", status=cca.ActionStatus("scoring", "success"))
    assert imp.completed == m["paper_count"] and not imp.failed
    # Cached: the same paper/version/model/prompt/rubric is never enqueued again.
    shortlist_and_enqueue(db, settings, REPORT_DATE)
    assert db.scalar("SELECT COUNT(*) FROM analysis_jobs WHERE status='pending'") == 0
    # A prompt-version change invalidates the cache key (new jobs).
    s2 = settings.model_copy(deep=True)
    s2.llm.prompt_version = "1.1"
    st = shortlist_and_enqueue(db, s2, REPORT_DATE)
    assert st.enqueued > 0
    assert db.scalar("SELECT COUNT(*) FROM llm_usage WHERE stage='scoring'") == 1


def test_partial_outputs_then_quota_exhaustion(db, settings):
    m = _setup(db, settings)
    first = m["batches"][0]["batch_id"]
    simulate_scoring(settings, fail_batches={b["batch_id"] for b in m["batches"]} - {first})
    st = cca.ActionStatus("scoring", "failure", category="quota", subscription_limit_event=True)
    imp = cca.import_scoring_outputs(db, settings, run_id="run1", status=st)
    n1 = len(m["batches"][0]["job_ids"])
    assert imp.completed == n1 and imp.failed == {"quota": m["paper_count"] - n1}
    assert db.scalar("SELECT COUNT(*) FROM analysis_jobs WHERE status='completed'") == n1
    assert db.scalar("SELECT subscription_limit_event FROM llm_usage") == 1


def test_out_of_range_and_wrong_version_rejected(db, settings):
    m = _setup(db, settings)
    simulate_scoring(settings)
    out = settings.root / m["batches"][0]["output"]
    data = json.loads(out.read_text())
    data["evaluations"][0]["dimensions"]["research_alignment"]["score"] = 140
    out.write_text(json.dumps(data))
    data2 = json.loads((settings.root / m["batches"][1]["output"]).read_text()) if len(m["batches"]) > 1 else None
    if data2:
        data2["evaluations"][0]["metadata_version"] = "stale"
        (settings.root / m["batches"][1]["output"]).write_text(json.dumps(data2))
    imp = cca.import_scoring_outputs(db, settings, run_id="run1", status=cca.ActionStatus("scoring", "success"))
    assert imp.failed.get("malformed", 0) >= len(m["batches"][0]["job_ids"])   # whole file invalid
    if data2:
        assert imp.failed.get("identity_mismatch") == 1


@pytest.mark.parametrize("msgs,outcome,category", [
    ([{"type": "result", "subtype": "success", "is_error": False, "num_turns": 7,
       "usage": {"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 10}, "total_cost_usd": 0.1}],
     "success", None),
    ([{"type": "result", "subtype": "error_during_execution", "is_error": True,
       "result": "OAuth token has expired. Please run /login"}], "failure", "auth"),
    ([{"type": "result", "subtype": "success", "is_error": True,
       "result": "Claude AI usage limit reached|1792000000"}], "failure", "quota"),
    ([{"type": "result", "subtype": "error_max_turns", "is_error": True, "num_turns": 30}], "failure", "max_turns"),
    ([{"type": "system", "subtype": "init"}], "cancelled", "timeout"),
    ([{"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "API Error: 401 invalid bearer"}],
     "failure", "auth"),
    ([{"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "429 rate_limit_error"}],
     "failure", "quota"),
])
def test_classify_execution(tmp_path, msgs, outcome, category):
    st = cca.classify_execution("scoring", outcome, exec_file(tmp_path, msgs))
    assert st.category == category
    if category is None:
        assert st.input_tokens == 1000 and st.output_tokens == 500 and st.num_turns == 7


def test_classify_missing_execution_file():
    st = cca.classify_execution("scoring", "failure", None)
    assert st.category == "action_failure" and st.outcome == "failure"


def test_status_file_contains_no_message_content(tmp_path, settings):
    secret_like = "Paper abstract text that must not be copied"
    st = cca.classify_execution("scoring", "failure", exec_file(tmp_path, [
        {"type": "assistant", "message": {"content": secret_like}},
        {"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "authentication_error"}]))
    path = cca.write_status(settings, st)
    assert secret_like not in path.read_text() and st.category == "auth"


def test_claude_args_minimal_tools(settings):
    args = cca.claude_args(settings, "scoring")
    assert "--max-turns 30" in args and "--model sonnet" in args
    assert "Write(./analysis/output/**)" in args and "Bash" in args.split("--disallowedTools", 1)[1]
    assert "WebFetch" in args.split("--disallowedTools", 1)[1]
    prompt = cca.action_prompt(settings, "scoring")
    assert "untrusted" in prompt and "Do not publish" in prompt


def test_interrupted_action_recovery_and_no_reenqueue(db, settings):
    _setup(db, settings, limit=3)
    from datetime import UTC, datetime, timedelta
    later = datetime.now(UTC) + timedelta(hours=5)
    # Publish job never ran (runner cancelled): leases expire and the same jobs are leased again.
    jobs = q.lease(db, run_id="run2", limit=10, policy=q.QueuePolicy(lease_minutes=240), now=later)
    assert len(jobs) >= 3
    n_jobs = db.scalar("SELECT COUNT(*) FROM analysis_jobs")
    shortlist_and_enqueue(db, settings, REPORT_DATE)                     # re-discovery does not duplicate jobs
    assert db.scalar("SELECT COUNT(*) FROM analysis_jobs") == n_jobs
