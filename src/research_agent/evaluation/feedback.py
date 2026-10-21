"""Human feedback (feedback/feedback.csv) and versioned, NON-applied profile adjustment proposals."""

from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

import yaml

from research_agent.settings import Settings
from research_agent.storage.database import Database
from research_agent.timeutil import iso_utc

LABELS = {"highly_relevant": 2.0, "relevant": 1.0, "methodologically_interesting": 0.5, "already_known": 0.0,
          "too_far_from_core": -1.0, "not_relevant": -2.0}
FEEDBACK_HEADER = ["date", "canonical_id", "label", "note"]


def feedback_path(settings: Settings) -> Path:
    return settings.root / "feedback" / "feedback.csv"


def add_feedback(settings: Settings, canonical_id: str, label: str, note: str = "", report_date: str = "") -> None:
    if label not in LABELS:
        raise ValueError(f"label must be one of {sorted(LABELS)}")
    path = feedback_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(FEEDBACK_HEADER)
        w.writerow([report_date, canonical_id, label, note])


def import_feedback(db: Database, settings: Settings) -> int:
    path = feedback_path(settings)
    if not path.exists():
        return 0
    n = 0
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("label") not in LABELS or not row.get("canonical_id"):
                continue
            cur = db.execute("INSERT OR IGNORE INTO feedback(canonical_id, label, note, report_date, recorded_at)"
                             " VALUES (?,?,?,?,?)", (row["canonical_id"].strip(), row["label"], row.get("note") or "",
                                                     row.get("date") or None, iso_utc()))
            n += cur.rowcount
    return n


def suggest_adjustments(db: Database, settings: Settings, *, min_labels: int = 5, max_step: float = 0.1) -> Path | None:
    """Aggregate feedback by the research domains Claude assigned, and PROPOSE bounded importance
    changes in a dated file. The user's configuration is never modified automatically."""
    rows = db.query("SELECT f.label, e.payload FROM feedback f JOIN papers p ON p.canonical_id=f.canonical_id"
                    " JOIN evaluations e ON e.paper_id=p.id GROUP BY f.id")
    per_topic: dict[str, list[float]] = {}
    for r in rows:
        domains = json.loads(r["payload"]).get("research_domains") or []
        for d in domains:
            per_topic.setdefault(d, []).append(LABELS[r["label"]])
    proposals = []
    for t in settings.profile.topics:
        vals = per_topic.get(t.id, [])
        if len(vals) < min_labels:
            continue
        mean = sum(vals) / len(vals)
        step = max(-max_step, min(max_step, round(mean / 20, 3)))
        if abs(step) < 0.01:
            continue
        proposals.append({"topic": t.id, "current_importance": t.importance,
                          "proposed_importance": round(min(1.0, max(0.0, t.importance + step)), 3),
                          "evidence": {"labels": len(vals), "mean_label_value": round(mean, 3)}})
    if not proposals:
        return None
    out = settings.root / "feedback" / "proposals" / f"{date.today().isoformat()}.yaml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump({"generated_at": iso_utc(), "profile_version": settings.profile.profile_version,
                                   "status": "proposed (not applied)", "proposals": proposals}, sort_keys=False))
    return out
