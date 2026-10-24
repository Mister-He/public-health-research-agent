from datetime import UTC, date, datetime, timedelta

import pytest

from research_agent.llm.structured_output import (DimensionAssessment, Narrative, PaperEvaluation, check_grounding,
                                                  quote_grounded)
from research_agent.metadata.deduplication import ingest_record
from research_agent.metadata.normalization import normalize_text_for_match
from research_agent.metadata.schemas import SourceRecord
from research_agent.storage import analysis_queue as q

T0 = datetime(2026, 10, 21, tzinfo=UTC)


def _paper(db, i=1):
    r = SourceRecord(provider="fixture", source_record_id=str(i), query_id="q", strategy="fixture",
                     retrieved_at="2026-10-21T00:00:00Z", doi=f"10.5555/q{i}", title=f"Title number {i} for queue tests")
    return ingest_record(db, r)


def _enqueue(db, res, mv="m1"):
    return q.enqueue(db, paper_id=res.paper_id, canonical_id=res.canonical_id, metadata_version=mv, rubric_version="r",
                     prompt_version="p", model="sonnet", priority=0.5, intended_report_date=date(2026, 10, 21))


def test_enqueue_is_deduplicated(db):
    res = _paper(db)
    assert _enqueue(db, res) is True
    assert _enqueue(db, res) is False            # identical job (any status) is never re-enqueued
    assert db.scalar("SELECT COUNT(*) FROM analysis_jobs") == 1
    assert _enqueue(db, res, mv="m2") is True    # materially revised metadata -> new job, old superseded
    assert db.scalar("SELECT status FROM analysis_jobs WHERE metadata_version='m1'") == "superseded"


def test_lease_recovery_after_interrupted_action(db):
    pol = q.QueuePolicy(lease_minutes=60)
    _enqueue(db, _paper(db))
    assert len(q.lease(db, run_id="r1", limit=10, policy=pol, now=T0)) == 1
    assert q.lease(db, run_id="r2", limit=10, policy=pol, now=T0 + timedelta(minutes=30)) == []
    again = q.lease(db, run_id="r3", limit=10, policy=pol, now=T0 + timedelta(minutes=61))
    assert len(again) == 1 and again[0]["failure_category"] == "lease_expired"


@pytest.mark.parametrize("cat", ["auth", "quota", "timeout", "action_failure", "max_turns"])
def test_outages_keep_jobs_without_consuming_attempts(db, cat):
    pol = q.QueuePolicy(quota_retry_hours=20)
    _enqueue(db, _paper(db))
    job = q.lease(db, run_id="r1", limit=1, policy=pol, now=T0)[0]
    for _ in range(5):
        assert q.fail(db, job["id"], cat, "x", pol, now=T0) == "retryable_failed"
    row = db.one("SELECT * FROM analysis_jobs")
    assert row["format_failures"] == 0 and row["outage_failures"] == 5 and row["status"] == "retryable_failed"
    assert q.lease(db, run_id="r2", limit=1, policy=pol, now=T0 + timedelta(hours=1)) == []
    assert len(q.lease(db, run_id="r3", limit=1, policy=pol, now=T0 + timedelta(days=5))) == 1


def test_malformed_output_backoff_then_permanent(db):
    pol = q.QueuePolicy(max_format_attempts=3, backoff_base_hours=6)
    _enqueue(db, _paper(db))
    job = q.lease(db, run_id="r1", limit=1, policy=pol, now=T0)[0]
    assert q.fail(db, job["id"], "malformed", "bad json", pol, now=T0) == "retryable_failed"
    ra = db.scalar("SELECT retry_after FROM analysis_jobs")
    assert ra == "2026-10-21T06:00:00Z"
    q.fail(db, job["id"], "ungrounded", "x", pol, now=T0)
    assert q.fail(db, job["id"], "malformed", "x", pol, now=T0) == "permanent_failed"
    assert q.release_for_retry(db) == 0                         # permanent failures are not auto-retried
    assert q.release_for_retry(db, only_eligible=False) == 1


def test_catch_up_order_oldest_first(db):
    a, b = _paper(db, 1), _paper(db, 2)
    q.enqueue(db, paper_id=a.paper_id, canonical_id=a.canonical_id, metadata_version="m", rubric_version="r",
              prompt_version="p", model="s", priority=0.1, intended_report_date=date(2026, 10, 18))
    q.enqueue(db, paper_id=b.paper_id, canonical_id=b.canonical_id, metadata_version="m", rubric_version="r",
              prompt_version="p", model="s", priority=0.9, intended_report_date=date(2026, 10, 21))
    jobs = q.lease(db, run_id="r", limit=1, policy=q.QueuePolicy(), now=T0)
    assert jobs[0]["paper_id"] == a.paper_id


RECORD = {"title": "Bayesian calibration of a microsimulation model", "abstract":
          "We calibrated a microsimulation model of 1200 individuals using approximate Bayesian computation.",
          "author_keywords": ["calibration"], "mesh_terms": []}


def _ev(evidence=("calibrated a microsimulation model",), rationale="Strong fit with calibration methods.", conf=0.8):
    dims = {d: DimensionAssessment(score=70, confidence=conf, rationale=rationale, evidence=list(evidence))
            for d in ("research_alignment", "methodological_innovation")}
    return PaperEvaluation(canonical_id="c", metadata_version="m", dimensions=dims, profile_category="core",
                           novelty_class="new_application", novelty_basis="abstract_only",
                           methodology_assessment="Applies ABC calibration.", why_useful="Relevant to calibration.")


def test_grounding_accepts_verbatim_quotes():
    ev, rep = check_grounding(_ev(), RECORD, llm_dimensions=["research_alignment", "methodological_innovation"],
                              taxonomy_ids=set())
    assert rep.ok, rep.errors


def test_grounding_caps_confidence_when_no_evidence_quoted():
    ev, rep = check_grounding(_ev(evidence=(), conf=0.6), RECORD,
                              llm_dimensions=["research_alignment", "methodological_innovation"], taxonomy_ids=set())
    assert rep.ok, rep.errors
    assert all(d.confidence == 0.4 for d in ev.dimensions.values())


def test_grounding_rejects_fabricated_evidence_and_numbers():
    _, rep = check_grounding(_ev(evidence=("validated in 14 countries",)), RECORD,
                             llm_dimensions=["research_alignment", "methodological_innovation"], taxonomy_ids=set())
    assert not rep.ok and any("no grounded evidence" in e for e in rep.errors)
    _, rep2 = check_grounding(_ev(rationale="The ICER of 23,500 per QALY is favourable."), RECORD,
                              llm_dimensions=["research_alignment", "methodological_innovation"], taxonomy_ids=set())
    assert not rep2.ok and any("numbers not present" in e for e in rep2.errors)
    _, rep3 = check_grounding(_ev(rationale="Uses 1200 simulated individuals."), RECORD,
                              llm_dimensions=["research_alignment", "methodological_innovation"], taxonomy_ids=set())
    assert rep3.ok   # numbers that ARE in the record are fine


def test_grounding_missing_dimension_and_no_abstract():
    _, rep = check_grounding(_ev(), RECORD, llm_dimensions=["research_alignment", "methodological_innovation",
                                                            "methodological_rigor"], taxonomy_ids=set())
    assert not rep.ok and "missing dimensions" in rep.errors[0]
    _, rep2 = check_grounding(_ev(evidence=("Bayesian calibration",)), {**RECORD, "abstract": None},
                              llm_dimensions=["research_alignment", "methodological_innovation"], taxonomy_ids=set())
    assert not rep2.ok   # high confidence without an abstract


def test_schema_rejects_out_of_range_and_list_narrative():
    with pytest.raises(ValueError):
        DimensionAssessment(score=120, confidence=0.5, rationale="xxxxxxxxxxxx")
    with pytest.raises(ValueError):
        Narrative(schema_version="1.0", report_date="2026-10-21", selection_hash="h",
                  narrative="- first paper does x\n- second paper does y " + "word " * 60)


def test_quote_grounding_handles_ellipses():
    src = normalize_text_for_match(RECORD["abstract"])
    assert quote_grounded("We calibrated ... approximate Bayesian computation", src)
    assert not quote_grounded("We calibrated ... a neural network", src)
