"""Historical replay on a throwaway copy of the database (never writes the real state).

Look-ahead controls: only papers discovered on/before the simulated date, published on/before it,
and a trend snapshot built only from those papers (strict_as_of). Evaluations created later are
allowed only with --retrospective (an assessment of paper content, flagged in the output)."""

from __future__ import annotations

import shutil
import tempfile
from datetime import date
from pathlib import Path

from research_agent.ranking.selector import run_selection
from research_agent.settings import Settings
from research_agent.storage.database import Database
from research_agent.trends.updater import update_trends


def replay(settings: Settings, db_path: Path, as_of: date, *, retrospective: bool = False) -> dict:
    with tempfile.TemporaryDirectory() as td:
        copy = Path(td) / "replay.sqlite3"
        shutil.copy(db_path, copy)
        db = Database(copy)
        end = f"{as_of.isoformat()}T23:59:59Z"
        future = {r[0] for r in db.query("SELECT id FROM papers WHERE discovered_at > ?", (end,))}
        if not retrospective:
            future |= {r[0] for r in db.query("SELECT paper_id FROM evaluations GROUP BY paper_id HAVING MIN(created_at) > ?",
                                              (end,))}
        db.execute("DELETE FROM recommendations WHERE report_date >= ?", (as_of.isoformat(),))
        db.execute("DELETE FROM trend_snapshots WHERE as_of_date >= ? AND id NOT IN (SELECT trend_snapshot_id FROM"
                   " paper_scores WHERE trend_snapshot_id IS NOT NULL)", (as_of.isoformat(),))
        snap = update_trends(db, settings, as_of, kind="daily", run_id="replay", strict_as_of=True)
        sel = run_selection(db, settings, as_of, run_id="replay", force=True, exclude_paper_ids=future)
        db.close()
    return {"as_of": as_of.isoformat(), "retrospective": retrospective, "excluded_future_papers": len(future),
            "trend_corpus": snap.corpus_size,
            "selected": [{"position": r["position"], "canonical_id": r["canonical_id"], "raw_score": r["raw_score"]}
                         for r in sel.selected],
            "excluded": sel.excluded}
