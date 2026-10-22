"""Independent score validation: recompute everything from stored components."""

from __future__ import annotations

import json
from datetime import date

from research_agent.llm.structured_output import PaperEvaluation
from research_agent.ranking.eligibility import Candidate, check
from research_agent.scoring.aggregator import Unscorable, recompute_overall
from research_agent.scoring.journal import journal_context
from research_agent.scoring.registry import ScoreContext, get_evaluator
from research_agent.settings import Settings
from research_agent.storage.database import Database
from research_agent.storage.repositories import load_paper
from research_agent.validation.results import Check


def validate_scores(db: Database, settings: Settings, report_date: date) -> list[Check]:
    out: list[Check] = []
    cfg = settings.scoring
    tol = cfg.tolerance
    weights = {d.id: d.weight for d in cfg.enabled}
    total = sum(weights.values())
    out.append(Check("weights_sum_to_one", "report", "pass" if abs(total - 1) <= 1e-9 else "fail", f"sum={total}"))
    rows = db.query("SELECT * FROM recommendations WHERE report_date=? AND status IN ('selected','published')"
                    " ORDER BY position", (report_date.isoformat(),))
    seen_ids, seen_pids = set(), set()
    prev_key = None
    for r in rows:
        cid = r["canonical_id"]

        def ok(name: str, cond: bool, detail: str = "") -> None:
            out.append(Check(name, cid, "pass" if cond else "fail", "" if cond else detail))

        ok("scoring_version_current", r["scoring_version"] == cfg.version(),
           f"{r['scoring_version']} != {cfg.version()}")
        reason = json.loads(r["reason"])
        dims = reason.get("dimensions") or {}
        missing = [d for d in weights if d not in dims]
        ok("all_dimensions_present", not missing, f"missing {missing}")
        bad = [d for d, v in dims.items() if v.get("score") is not None and not (0 <= v["score"] <= 100)]
        ok("scores_in_range", not bad, f"out of range: {bad}")
        try:
            recomputed = recompute_overall(weights, dims)
            ok("composite_recomputed", abs(recomputed - r["raw_score"]) <= tol,
               f"recomputed {recomputed:.6f} != stored {r['raw_score']:.6f}")
        except Unscorable as exc:
            ok("composite_recomputed", False, str(exc))
        ev = PaperEvaluation.model_validate_json(db.scalar("SELECT payload FROM evaluations WHERE id=?", (r["evaluation_id"],)))
        for d in cfg.llm_dimensions:
            stored = dims.get(d.id, {}).get("score")
            ok(f"llm_score_matches_evaluation:{d.id}", stored is not None and abs(stored - ev.dimensions[d.id].score) <= tol,
               f"{stored} vs evaluation {ev.dimensions[d.id].score if d.id in ev.dimensions else None}")
        p = load_paper(db, r["paper_id"])
        jctx = journal_context(db, settings, p)
        for d in cfg.enabled:
            if d.method == "deterministic":
                res = get_evaluator(d.evaluator)(ScoreContext(p, report_date, ev, d.params, journal=jctx))  # type: ignore[arg-type]
                stored = dims.get(d.id, {}).get("score")
                exp = 0.0 if res.score is None and d.missing_data_policy == "zero" else res.score
                ok(f"deterministic_recomputed:{d.id}", exp is not None and stored is not None and abs(exp - stored) <= tol,
                   f"recomputed {exp} != stored {stored}")
        c = Candidate(p, r["evaluation_id"], ev, ev.profile_category, journal=jctx)
        reason_inel = check(c, settings.ranking, report_date, settings)
        ok("hard_eligibility", reason_inel is None, reason_inel or "")
        ok("no_duplicate_canonical", cid not in seen_ids and r["paper_id"] not in seen_pids, "duplicate paper")
        seen_ids.add(cid)
        seen_pids.add(r["paper_id"])
        key = (-r["raw_score"], -(p.first_publication_date.toordinal() if p.first_publication_date else 0), cid)
        ok("descending_raw_order", prev_key is None or key >= prev_key, "papers not in descending raw-score order")
        prev_key = key
        lam = float(settings.ranking.diversity.get("lambda", 0.1)) if settings.ranking.diversity.get("enabled", True) else 0.0
        pen = r["raw_score"] - r["adjusted_score"]
        ok("diversity_bounded", -tol <= pen <= lam * 100 + tol, f"penalty {pen:.4f} exceeds bound {lam * 100}")
    # counterpart duplicates (preprint + its journal version both selected)
    pids = list(seen_pids)
    if pids:
        dup = db.scalar(f"SELECT COUNT(*) FROM paper_relations WHERE from_paper_id IN ({','.join('?' * len(pids))})"
                        f" AND to_paper_id IN ({','.join('?' * len(pids))})", (*pids, *pids))
        out.append(Check("no_preprint_journal_duplicates", "report", "pass" if not dup else "fail",
                         f"{dup} preprint/journal pair(s) selected together" if dup else ""))
    n_expl = sum(1 for r in rows if json.loads(r["reason"]).get("category") == "exploratory")
    out.append(Check("exploratory_cap", "report", "pass" if n_expl <= settings.ranking.max_exploratory else "fail",
                     f"{n_expl} exploratory > cap {settings.ranking.max_exploratory}"))
    return out
