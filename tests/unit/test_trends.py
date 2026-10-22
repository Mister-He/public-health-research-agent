import math
from datetime import date, timedelta

import pytest
from fixtures.synthetic import REPORT_DATE, records

from research_agent.metadata.deduplication import ingest_record
from research_agent.retrieval.screening import prescreen
from research_agent.trends import taxonomy as tx
from research_agent.trends.statistics import TagCounts, compute_trend, external_signal, wilson
from research_agent.trends.updater import latest_snapshot, update_trends

KW = dict(weights={"frequency": 0.5, "growth": 0.3, "external": 0.2}, pseudo_count=1.0, frequency_scale=0.05,
          growth_scale=0.7, min_sample=5, ema_alpha=0.3)


def test_smoothing_avoids_division_by_zero():
    t = compute_trend(TagCounts("t", 0, 0, 0, 0), previous=None, external=None, **KW)
    assert 0 <= t.score <= 1 and t.growth_signal is None and "growth unavailable" in t.notes[0]


def test_growth_detects_increase_and_small_sample_guard():
    up = compute_trend(TagCounts("t", 30, 300, 20, 1800), previous=None, external=None, **KW)
    flat = compute_trend(TagCounts("t", 10, 300, 60, 1800), previous=None, external=None, **KW)
    # Equal raw shares -> ~0.5 (additive smoothing nudges the smaller sample slightly upward).
    assert up.growth_signal > 0.9 and abs(flat.growth_signal - 0.5) < 0.05
    tiny = compute_trend(TagCounts("t", 2, 300, 1, 1800), previous=None, external=None, **KW)
    assert tiny.growth_signal is None   # below min_sample: not reported as growth


def test_unavailable_components_are_renormalized_not_invented():
    t = compute_trend(TagCounts("t", 3, 100, 0, 0), previous=None, external=None, **KW)
    assert t.available == ["frequency"] and t.raw_score == pytest.approx(t.frequency_signal)
    t2 = compute_trend(TagCounts("t", 30, 300, 20, 1800), previous=None, external=0.8, **KW)
    exp = (0.5 * t2.frequency_signal + 0.3 * t2.growth_signal + 0.2 * 0.8)
    assert t2.raw_score == pytest.approx(exp, abs=1e-6)


def test_ema_dampens_one_day_spike():
    spike = compute_trend(TagCounts("t", 200, 300, 60, 1800), previous=0.1, external=None, **KW)
    assert spike.score == pytest.approx(0.3 * spike.raw_score + 0.7 * 0.1, abs=1e-6)
    assert spike.score < spike.raw_score


def test_manual_override_and_popularity_vs_growth():
    t = compute_trend(TagCounts("t", 1, 300, 1, 1800), previous=None, external=None, manual_override=0.9, **KW)
    assert t.score == 0.9
    popular_stable = compute_trend(TagCounts("p", 60, 300, 360, 1800), previous=None, external=None, **KW)
    rare_growing = compute_trend(TagCounts("g", 6, 300, 6, 1800), previous=None, external=None, **KW)
    assert popular_stable.frequency_signal > rare_growing.frequency_signal
    assert rare_growing.growth_signal > popular_stable.growth_signal


def test_wilson_and_external():
    lo, hi = wilson(5, 100)
    assert 0 < lo < 0.05 < hi < 0.15
    assert external_signal(0, 0, 0, 0, pseudo_count=1, growth_scale=0.7, min_sample=5) is None
    assert external_signal(200, 10000, 100, 10000, pseudo_count=1, growth_scale=0.7, min_sample=5) > 0.5


def _seed(db, settings):
    ids = [ingest_record(db, r).paper_id for r in records()]
    prescreen(db, settings, ids)


def test_snapshot_reproducible_and_idempotent(db, settings):
    _seed(db, settings)
    s1 = update_trends(db, settings, REPORT_DATE, kind="daily")
    s2 = update_trends(db, settings, REPORT_DATE, kind="daily")
    assert s1.id == s2.id and db.scalar("SELECT COUNT(*) FROM trend_snapshots") == 1
    assert {k: v["raw_score"] for k, v in s1.tags.items()} == {k: v["raw_score"] for k, v in s2.tags.items()}
    # Topic frequency counts come from the whole topic-filtered corpus, not the Top 10.
    assert s1.corpus_size >= 13
    abm = s1.tags["abm_public_health"]["counts"]["recent"]
    assert abm >= 1
    # Next day's snapshot uses the previous score (EMA) and does not double-count papers.
    s3 = update_trends(db, settings, REPORT_DATE + timedelta(days=1), kind="daily")
    assert s3.tags["abm_public_health"]["previous_score"] == pytest.approx(s1.tags["abm_public_health"]["score"])
    assert db.scalar("SELECT COUNT(*) FROM paper_tags WHERE method='lexical' AND tag_id='abm_public_health'") == abm
    assert latest_snapshot(db, on_or_before=REPORT_DATE).id == s1.id


def test_strict_as_of_excludes_future_discoveries(db, settings):
    _seed(db, settings)
    # Fixed discovery times (not the wall clock), so the test does not depend on today's date.
    db.execute("UPDATE papers SET discovered_at=?", (f"{REPORT_DATE.isoformat()}T01:00:00Z",))
    db.execute("UPDATE papers SET discovered_at='2026-12-01T00:00:00Z' WHERE canonical_id='doi:10.5555/fixture.0004'")
    s = update_trends(db, settings, REPORT_DATE, kind="daily", strict_as_of=True)
    full = update_trends(db, settings, REPORT_DATE + timedelta(days=1), kind="daily")
    assert s.corpus_size == full.corpus_size - 1


def test_tag_management_roundtrip(settings):
    tid = tx.add_tag(settings, "Microsimulation emulators", "Emulators for microsimulation",
                     [["emulator", "microsimulation"]])
    from research_agent.settings import load_settings
    s = load_settings(settings.root)
    assert s.tag(tid).status == "active"
    tx.update_tag(s, tid, name="Emulators for microsimulation")
    tx.update_tag(s, tid, pinned=True)
    tx.update_tag(s, tid, manual_override=0.7)
    tx.merge_tags(s, "digital_twins_population", tid)
    tx.update_tag(s, "rl_adaptive_interventions", status="archived")
    s = load_settings(settings.root)
    t = s.tag(tid)
    assert t.name == "Emulators for microsimulation" and t.pinned and t.manual_override == 0.7
    assert "Digital twins in population health" in t.aliases and ["digital twin"] in t.match
    assert s.tag("digital_twins_population").status == "archived"
    assert s.tag("rl_adaptive_interventions").status == "archived"
    tx.update_tag(s, "rl_adaptive_interventions", status="active")
    assert load_settings(settings.root).tag("rl_adaptive_interventions").status == "active"
    assert load_settings(settings.root).trends.taxonomy_version > settings.trends.taxonomy_version
    with pytest.raises(ValueError):
        tx.add_tag(s, "dup", "d", [["x"]], tag_id=tid)


def test_sync_respects_config_and_archives_removed(db, settings):
    tx.sync_tags(db, settings)
    n = db.scalar("SELECT COUNT(*) FROM topic_tags WHERE status='active'")
    s2 = settings.model_copy(deep=True)
    s2.trends.tags = [t for t in s2.trends.tags if t.id != "genai_health_policy"]
    tx.sync_tags(db, s2)
    assert db.scalar("SELECT status FROM topic_tags WHERE id='genai_health_policy'") == "archived"
    assert db.scalar("SELECT COUNT(*) FROM topic_tags WHERE status='active'") == n - 1
    assert math.isfinite(n)
