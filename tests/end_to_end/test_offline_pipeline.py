"""Complete offline pipeline on SYNTHETIC fixture records (no network, no credentials)."""

from __future__ import annotations

import re

import pytest
from fixtures.synthetic import REPORT_DATE, records

from research_agent.llm import claude_code_action as cca
from research_agent.llm.fixture_provider import as_fixture, simulate_narrative, simulate_scoring
from research_agent.metadata.deduplication import ingest_record
from research_agent.pipeline.runner import prepare_narrative, publish_stage
from research_agent.retrieval.screening import prescreen, shortlist_and_enqueue
from research_agent.storage.database import open_db
from research_agent.validation.gate import run_gate

RUN = "e2e-run"


@pytest.fixture
def settings(settings):
    return as_fixture(settings)


def _ingest_and_score(db, settings, *, narrative: bool = True):
    ids = {ingest_record(db, r, run_id=RUN).paper_id for r in records()}
    prescreen(db, settings, sorted(ids))
    shortlist_and_enqueue(db, settings, REPORT_DATE)
    m = cca.build_scoring_manifest(db, settings, run_id=RUN, report_date=REPORT_DATE)
    simulate_scoring(settings)
    cca.write_status(settings, cca.ActionStatus(stage="scoring", outcome="success"))
    if narrative:
        # The analysis job prepares the narrative on a throwaway copy of the DB.
        import shutil
        copy = settings.db_path.with_name("copy.sqlite3")
        shutil.copy(settings.db_path, copy)
        cdb = open_db(copy)
        prepare_narrative(cdb, settings, REPORT_DATE, run_id=RUN)
        cdb.close()
        simulate_narrative(settings)
    return m


def test_full_offline_pipeline(db, settings):
    m = _ingest_and_score(db, settings)
    # 2. normalized + deduplicated: 15 distinct DOIs + 3 special records; the duplicate merged.
    assert db.scalar("SELECT COUNT(*) FROM papers") == 18
    assert db.scalar("SELECT times_seen FROM paper_discoveries WHERE query_id='journal:fixture'") == 1
    assert db.scalar("SELECT COUNT(DISTINCT query_id) FROM paper_discoveries pd JOIN papers p ON p.id=pd.paper_id"
                     " WHERE p.canonical_id='doi:10.5555/fixture.0001'") == 2
    assert m["paper_count"] > 0
    res = publish_stage(db, settings, REPORT_DATE, run_id=RUN, online=False)
    assert res.status == "published", res.summary
    path = settings.reports_dir / f"{REPORT_DATE.isoformat()}.md"
    assert path.exists() and res.report_path == path
    text = path.read_text()
    assert text.startswith("---\n") and "validation_status: passed" in text
    # The simulated narrative matched the final selection and is labelled as simulated.
    assert "narrative_source: simulated fixture provider, not Claude" in text
    assert "model: fixture-simulated" in text
    # Irrelevant, retracted and out-of-horizon records are never recommended.
    for bad in ("Crystal structure", "knee arthroplasty", "retracted", "(older)"):
        assert bad not in text
    n = int(re.search(r"paper_count: (\d+)", text)[1])
    assert 3 <= n <= 10
    assert text.count("\n### ") == n
    expl = db.scalar("SELECT COUNT(*) FROM recommendations WHERE report_date=? AND json_extract(reason,'$.category')"
                     "='exploratory'", (REPORT_DATE.isoformat(),))
    assert expl <= settings.ranking.max_exploratory
    assert db.scalar("SELECT COUNT(*) FROM recommendations WHERE status='published'") == n
    # Raw scores are in descending order on the page.
    scores = [float(x) for x in re.findall(r"\| Overall score \| \*\*([\d.]+)\*\*", text)]
    assert scores == sorted(scores, reverse=True)


def test_gate_blocks_corrupted_artifacts(db, settings):
    _ingest_and_score(db, settings)
    res = publish_stage(db, settings, REPORT_DATE, run_id=RUN, online=False)
    assert res.status == "published"
    path = res.report_path
    good = path.read_text()
    assert run_gate(db, settings, REPORT_DATE, path, good).passed

    first_title = re.search(r"\n### 1\. (.+)\n", good)[1]
    corruptions = {
        "wrong title": good.replace(f"### 1. {first_title}", "### 1. A Completely Different Title", 1),
        "fabricated abstract": good.replace("SYNTHETIC TEST RECORD, not a real publication.", "Fabricated text.", 1),
        "wrong score": re.sub(r"\| Overall score \| \*\*[\d.]+\*\*", "| Overall score | **99.99**", good, count=1),
        "bad date": good.replace(f"date: '{REPORT_DATE.isoformat()}'", "date: '2026-01-01'", 1)
        .replace(f"date: {REPORT_DATE.isoformat()}", "date: 2026-01-01", 1),
        "broken front matter": good.replace("---\n", "---\n: : bad: [\n", 1),
        "missing section": re.sub(r"\*\*MeSH terms:\*\*[^\n]*\n", "", good, count=1),
        "placeholder": good.replace("## Daily overview\n", "## Daily overview\n\n{{ narrative }}\n", 1),
        "fabricated citation": good.replace("## Methodology and coverage",
                                            "### 11. Invented paper\n\n| Field | Value |\n|---|---|\n| Canonical ID | `doi:10.9999/fake` |\n\n## Methodology and coverage", 1),
        "duplicate section": good.replace("## Methodology and coverage",
                                          good[good.index("### 1."):good.index("### 2.") if "### 2." in good else good.index("## Methodology")]
                                          + "## Methodology and coverage", 1),
        "wrong doi": re.sub(r"\[10\.5555/fixture\.(\d+)\]\(https://doi\.org/10\.5555/fixture\.\d+\)",
                            "[10.5555/other.1](https://doi.org/10.5555/other.1)", good, count=1),
    }
    for name, bad in corruptions.items():
        assert bad != good, name
        r = run_gate(db, settings, REPORT_DATE, path, bad)
        assert not r.passed, f"gate accepted corrupted artifact: {name}"


def test_rerun_same_date_is_idempotent(db, settings):
    _ingest_and_score(db, settings)
    first = publish_stage(db, settings, REPORT_DATE, run_id=RUN, online=False)
    text1 = first.report_path.read_text()
    counts = {t: db.scalar(f"SELECT COUNT(*) FROM {t}") for t in
              ("papers", "paper_identifiers", "recommendations", "evaluations", "trend_snapshots", "analysis_jobs")}
    # Re-ingest everything and re-run the publication for the same date.
    for r in records():
        ingest_record(db, r, run_id="again")
    shortlist_and_enqueue(db, settings, REPORT_DATE)
    second = publish_stage(db, settings, REPORT_DATE, run_id="again", online=False)
    assert second.status == "already_published"
    assert first.report_path.read_text() == text1
    after = {t: db.scalar(f"SELECT COUNT(*) FROM {t}") for t in counts}
    assert after == counts
    # Forced regeneration keeps the same recommendation order (deterministic).
    order1 = [r[0] for r in db.query("SELECT canonical_id FROM recommendations WHERE report_date=? ORDER BY position",
                                     (REPORT_DATE.isoformat(),))]
    third = publish_stage(db, settings, REPORT_DATE, run_id="again2", online=False, force=True)
    assert third.status == "published"
    order2 = [r[0] for r in db.query("SELECT canonical_id FROM recommendations WHERE report_date=? ORDER BY position",
                                     (REPORT_DATE.isoformat(),))]
    assert order1 == order2
    assert db.scalar("SELECT COUNT(*) FROM trend_snapshots") == counts["trend_snapshots"]


def test_claude_outage_preserves_backlog(db, settings):
    ids = {ingest_record(db, r, run_id=RUN).paper_id for r in records()}
    prescreen(db, settings, sorted(ids))
    shortlist_and_enqueue(db, settings, REPORT_DATE)
    m = cca.build_scoring_manifest(db, settings, run_id=RUN, report_date=REPORT_DATE)
    # The action failed with an expired OAuth token: no outputs at all.
    cca.write_status(settings, cca.ActionStatus(stage="scoring", outcome="failure", category="auth"))
    res = publish_stage(db, settings, REPORT_DATE, run_id=RUN, online=False)
    assert res.status == "insufficient" and res.exit_code == 3
    assert not (settings.reports_dir / f"{REPORT_DATE.isoformat()}.md").exists()
    rows = db.query("SELECT status, failure_category, format_failures FROM analysis_jobs")
    assert len(rows) == m["paper_count"]
    assert all(r["status"] == "retryable_failed" and r["failure_category"] == "auth" and r["format_failures"] == 0
               for r in rows)
    assert db.scalar("SELECT COUNT(*) FROM evaluations") == 0
    # Access restored on a later run: the same jobs are leased again (after retry_after) and complete.
    from datetime import datetime, timedelta, timezone

    from research_agent.storage import analysis_queue as q
    later = datetime.now(timezone.utc) + timedelta(days=2)
    jobs = q.lease(db, run_id="next", limit=100, policy=q.QueuePolicy(), now=later)
    assert len(jobs) == m["paper_count"]


@pytest.mark.parametrize("bad", ["malformed", "ungrounded"])
def test_bad_model_output_is_rejected_not_coerced(db, settings, bad):
    ids = {ingest_record(db, r, run_id=RUN).paper_id for r in records()}
    prescreen(db, settings, sorted(ids))
    shortlist_and_enqueue(db, settings, REPORT_DATE)
    m = cca.build_scoring_manifest(db, settings, run_id=RUN, report_date=REPORT_DATE)
    first_batch = m["batches"][0]["batch_id"]
    if bad == "malformed":
        simulate_scoring(settings, malformed_batches={first_batch})
    else:
        import json
        batch = json.loads((settings.root / m["batches"][0]["file"]).read_text())
        simulate_scoring(settings, ungrounded_ids={p["canonical_id"] for p in batch["papers"]})
    imp = cca.import_scoring_outputs(db, settings, run_id=RUN, status=cca.ActionStatus("scoring", "success"))
    n_first = len(m["batches"][0]["job_ids"])
    assert sum(imp.failed.values()) == n_first
    assert imp.completed == m["paper_count"] - n_first
    assert db.scalar("SELECT COUNT(*) FROM analysis_jobs WHERE status='retryable_failed' AND format_failures=1") == n_first
