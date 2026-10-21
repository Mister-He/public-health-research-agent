"""Offline recommendation-quality metrics against a manually labelled gold set.

feedback/gold.csv columns: canonical_id, relevant (0/1), category (core/exploratory),
novelty (0-3), applicability (0-3), priority (0-3). Gold labels are stored apart from model
scores. Metrics without labels are reported as None ("not measured"), never estimated."""

from __future__ import annotations

import csv
import json
import math
from itertools import combinations
from pathlib import Path

from research_agent.ranking.diversity import jaccard
from research_agent.storage.database import Database


def load_gold(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with open(path, newline="", encoding="utf-8") as fh:
        return {r["canonical_id"].strip(): r for r in csv.DictReader(fh) if r.get("canonical_id")}


def precision_at_k(recs: list[str], gold: dict[str, dict], k: int = 10) -> float | None:
    labelled = [c for c in recs[:k] if c in gold]
    if not labelled:
        return None
    return sum(1 for c in labelled if str(gold[c].get("relevant")) == "1") / len(labelled)


def ndcg_at_k(recs: list[str], gold: dict[str, dict], k: int = 10) -> float | None:
    gains = [float(gold[c].get("priority") or 0) if c in gold else None for c in recs[:k]]
    if all(g is None for g in gains):
        return None
    g = [x or 0.0 for x in gains]
    dcg = sum((2 ** x - 1) / math.log2(i + 2) for i, x in enumerate(g))
    ideal = sorted((float(r.get("priority") or 0) for r in gold.values()), reverse=True)[:k]
    idcg = sum((2 ** x - 1) / math.log2(i + 2) for i, x in enumerate(ideal))
    return dcg / idcg if idcg > 0 else None


def evaluate(db: Database, gold_path: Path) -> dict:
    gold = load_gold(gold_path)
    days = db.query("SELECT report_date FROM daily_reports WHERE status='published' ORDER BY report_date")
    per_day = []
    all_recs: list[str] = []
    diversities = []
    for d in days:
        rows = db.query("SELECT canonical_id, reason FROM recommendations WHERE report_date=? AND status='published'"
                        " ORDER BY position", (d["report_date"],))
        recs = [r["canonical_id"] for r in rows]
        all_recs += recs
        tags = [frozenset(json.loads(r["reason"]).get("tags") or []) for r in rows]
        if len(tags) > 1:
            diversities.append(sum(1 - jaccard(a, b) for a, b in combinations(tags, 2)) / math.comb(len(tags), 2))
        per_day.append({"date": d["report_date"], "n": len(recs), "precision_at_10": precision_at_k(recs, gold),
                        "ndcg_at_10": ndcg_at_k(recs, gold)})
    relevant = {c for c, r in gold.items() if str(r.get("relevant")) == "1"}
    recall = len(relevant & set(all_recs)) / len(relevant) if relevant else None
    dup_rate = (len(all_recs) - len(set(all_recs))) / len(all_recs) if all_recs else None
    vf = db.one("SELECT SUM(status='fail') f, COUNT(*) n FROM validation_results WHERE target != 'report'")
    fb = {r["label"]: r["n"] for r in db.query("SELECT label, COUNT(*) n FROM feedback GROUP BY label")}

    def mean(key):
        vals = [x[key] for x in per_day if x[key] is not None]
        return sum(vals) / len(vals) if vals else None

    return {"gold_labels": len(gold), "days": len(per_day), "mean_precision_at_10": mean("precision_at_10"),
            "mean_ndcg_at_10": mean("ndcg_at_10"), "recall_on_curated_set": recall,
            "mean_intra_list_diversity": sum(diversities) / len(diversities) if diversities else None,
            "duplicate_recommendation_rate": dup_rate,
            "paper_check_failure_rate": (vf["f"] / vf["n"]) if vf and vf["n"] else None,
            "user_feedback_counts": fb, "per_day": per_day,
            "note": "None means not measured (no labels/data); values are never imputed."}
