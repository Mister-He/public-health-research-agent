from datetime import date

import pytest

from research_agent.llm.structured_output import DimensionAssessment, PaperEvaluation, TagAssignment
from research_agent.metadata.schemas import Paper
from research_agent.ranking.diversity import jaccard, penalty
from research_agent.ranking.eligibility import Candidate, check
from research_agent.ranking.selector import select
from research_agent.scoring import registry
from research_agent.scoring.aggregator import Unscorable, combine, recompute_overall, score_dimensions
from research_agent.scoring.registry import DimensionScore, ScoreContext

D = date(2026, 10, 21)


def ev(cid="doi:10.5555/x", align=80.0, cat="core", tags=(), **dims) -> PaperEvaluation:
    base = {"research_alignment": align, "methodological_innovation": 60, "methodological_rigor": 50,
            "cross_disciplinary_transferability": 40, **dims}
    return PaperEvaluation(
        canonical_id=cid, metadata_version="m1",
        dimensions={k: DimensionAssessment(score=v, confidence=0.7, rationale="grounded rationale text",
                                           evidence=["x"]) for k, v in base.items()},
        profile_category=cat, novelty_class="new_application", novelty_basis="abstract_only",
        methodology_assessment="assessment text", why_useful="useful because",
        topic_tags=[TagAssignment(tag_id=t, confidence=0.9, evidence="quote") for t in tags])


def paper(cid="doi:10.5555/x", days_ago=3, **kw) -> Paper:
    from datetime import timedelta
    kw.setdefault("metadata_completeness", 0.9)
    return Paper(id=kw.pop("id", 1), canonical_id=cid, doi=cid.removeprefix("doi:"), title="T", authors=["A"],
                 journal="J", canonical_url=f"https://doi.org/{cid.removeprefix('doi:')}",
                 first_publication_date=D - timedelta(days=days_ago), **kw)


def ctx(p=None, e=None, **kw):
    return ScoreContext(paper=p or paper(), report_date=D, evaluation=e or ev(), params={}, **kw)


def test_freshness_smooth_decay(settings):
    f = registry.get_evaluator("freshness")
    s0 = f(ScoreContext(paper(days_ago=0), D, None, {"half_life_days": 14})).score
    s14 = f(ScoreContext(paper(days_ago=14), D, None, {"half_life_days": 14})).score
    s15 = f(ScoreContext(paper(days_ago=15), D, None, {"half_life_days": 14})).score
    assert s0 == 100 and abs(s14 - 50) < 1e-9 and 0 < s15 < s14
    assert f(ScoreContext(paper(days_ago=-5), D, None, {})).score == 100   # future date counts as age 0
    assert f(ScoreContext(paper().model_copy(update={"first_publication_date": None}), D, None, {})).score is None


def test_trend_relevance_probabilistic_or():
    f = registry.get_evaluator("trend_relevance")
    c = ScoreContext(paper(), D, ev(tags=("a", "b")), {}, trend_scores={"a": 0.5, "b": 0.5}, trend_snapshot_id=1)
    r = f(c)
    assert r.score == pytest.approx(100 * (1 - (1 - 0.45) ** 2))
    assert f(ScoreContext(paper(), D, ev(), {}, trend_scores={}, trend_snapshot_id=None)).score is None


def test_weighted_score_and_independent_recompute(settings):
    dims = score_dimensions(settings.scoring, ctx(trend_scores={}, trend_snapshot_id=1))
    comp = combine(settings.scoring, dims)
    manual = sum(d.weight * (dims[d.id].score or 0) for d in settings.scoring.enabled)
    assert comp.overall == pytest.approx(manual)
    weights = {d.id: d.weight for d in settings.scoring.enabled}
    assert recompute_overall(weights, comp.dimensions) == pytest.approx(comp.overall)


def test_missing_llm_dimension_is_rejected_not_defaulted(settings):
    e = ev()
    del e.dimensions["methodological_innovation"]
    dims = score_dimensions(settings.scoring, ctx(e=e, trend_snapshot_id=1))
    with pytest.raises(Unscorable):
        combine(settings.scoring, dims)


def test_out_of_range_dimension(settings):
    @registry.register("test_out_of_range")
    def bad(_):  # pragma: no cover - trivial
        return DimensionScore(150, "deterministic")
    cfg = settings.scoring.model_copy(deep=True)
    cfg.dimensions[3].evaluator = "test_out_of_range"
    with pytest.raises(Unscorable):
        score_dimensions(cfg, ctx(trend_snapshot_id=1))


def test_custom_dimension_registration(project):
    """A new dimension = config + registered evaluator; the aggregator is unchanged."""
    import yaml

    from research_agent.settings import load_settings

    @registry.register("open_access_bonus")
    def oa(c: ScoreContext) -> DimensionScore:
        return DimensionScore(100.0 if c.paper.open_access_url else 0.0, "deterministic")

    p = project / "config" / "scoring.yaml"
    data = yaml.safe_load(p.read_text())
    data["scoring"]["dimensions"][0]["weight"] -= 0.05   # make room for the new 0.05 dimension
    data["scoring"]["dimensions"].append({"id": "open_access", "name": "Open access", "description": "OA bonus",
                                          "enabled": True, "weight": 0.05, "method": "deterministic",
                                          "evaluator": "open_access_bonus", "rubric_version": "1.0",
                                          "missing_data_policy": "zero"})
    p.write_text(yaml.safe_dump(data))
    s = load_settings(project)
    dims = score_dimensions(s.scoring, ScoreContext(paper(open_access_url="https://x.org/a.pdf"), D, ev(), {},
                                                    trend_snapshot_id=1))
    assert dims["open_access"].score == 100.0
    assert "open_access" in combine(s.scoring, dims).dimensions


def test_eligibility_rules(settings):
    cfg = settings.ranking
    assert check(Candidate(paper(), 1, ev(align=80), "core"), cfg, D) is None
    assert "threshold" in check(Candidate(paper(), 1, ev(align=40), "core"), cfg, D)
    assert "threshold" in check(Candidate(paper(), 1, ev(align=58, cat="exploratory"), "exploratory"), cfg, D)
    assert "horizon" in check(Candidate(paper(days_ago=45), 1, ev(), "core"), cfg, D)
    assert "look-ahead" in check(Candidate(paper(days_ago=-2), 1, ev(), "core"), cfg, D)
    assert check(Candidate(paper(is_retracted=True), 1, ev(), "core"), cfg, D) == "retracted"
    assert "completeness" in check(Candidate(paper(metadata_completeness=0.2), 1, ev(), "core"), cfg, D)


def _cands(spec):
    out = []
    for i, (score, cat, tags) in enumerate(spec):
        c = Candidate(paper(f"doi:10.5555/c{i}", id=i + 1), i + 1, ev(f"doi:10.5555/c{i}", cat=cat), cat)
        c.raw_score, c.tags = score, frozenset(tags)
        out.append(c)
    return out


def test_selection_caps_and_bounded_diversity(settings):
    spec = [(90, "core", {"a"}), (89.5, "core", {"a"}), (89, "core", {"b"}), (88, "exploratory", {"x"}),
            (87, "exploratory", {"y"}), (86, "exploratory", {"z"})] + [(70 - i, "core", {f"t{i}"}) for i in range(10)]
    chosen = select(_cands(spec), settings)
    assert len(chosen) == 10
    assert sum(1 for c, *_ in chosen if c.category == "exploratory") == settings.ranking.max_exploratory
    # Diversity penalty is bounded by lambda*100 and never applied to the first pick.
    lam = settings.ranking.diversity["lambda"]
    assert chosen[0][2] == 0 and all(pen <= lam * 100 + 1e-9 for _, _, pen, _ in chosen)
    # A near-duplicate-topic paper (89.5, tag a) is reordered behind a diverse one but still selected.
    picked = [c.raw_score for c, *_ in chosen]
    assert 89.5 in picked and picked.index(89) < picked.index(89.5)


def test_selection_ranking_stable_for_ties(settings):
    spec = [(80, "core", {"a"})] * 4
    a = [c.paper.canonical_id for c, *_ in select(_cands(spec), settings)]
    b = [c.paper.canonical_id for c, *_ in select(_cands(spec), settings)]
    assert a == b


def test_core_majority_enforced(settings):
    chosen = select(_cands([(95, "exploratory", {"x"}), (60, "core", {"a"})]), settings)
    assert [c.category for c, *_ in chosen] == ["core"]


def test_no_padding_when_few_candidates(settings):
    assert len(select(_cands([(80, "core", {"a"})]), settings)) == 1
    assert select([], settings) == []


def test_jaccard_penalty():
    assert jaccard(frozenset(), frozenset({"a"})) == 0
    assert penalty(frozenset({"a", "b"}), [frozenset({"a"})], 0.1) == pytest.approx(5.0)
