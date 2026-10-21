"""Pipeline run records (observability) and execution summaries."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from research_agent.settings import PIPELINE_VERSION, Settings
from research_agent.storage.database import Database, dumps
from research_agent.timeutil import iso_utc


@dataclass
class RunRecord:
    run_id: str
    stage: str
    scheduled_date: date
    started_at: str = field(default_factory=iso_utc)
    _t0: float = field(default_factory=time.monotonic)
    summary: dict = field(default_factory=dict)

    def start(self, db: Database, settings: Settings) -> RunRecord:
        db.execute("INSERT INTO pipeline_runs(run_id, scheduled_date, stage, started_at, status, pipeline_version,"
                   " config_hash, summary) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET"
                   " started_at=excluded.started_at, status=excluded.status",
                   (self.key, self.scheduled_date.isoformat(), self.stage, self.started_at, "running", PIPELINE_VERSION,
                    settings.config_hash, "{}"))
        return self

    @property
    def key(self) -> str:
        return f"{self.run_id}:{self.stage}"

    def finish(self, db: Database, status: str) -> None:
        self.summary["duration_seconds"] = round(time.monotonic() - self._t0, 2)
        db.execute("UPDATE pipeline_runs SET completed_at=?, duration_seconds=?, status=?, summary=? WHERE run_id=?",
                   (iso_utc(), self.summary["duration_seconds"], status, dumps(self.summary), self.key))


def latest_stage_summary(db: Database, scheduled_date: date, stage: str) -> dict:
    row = db.one("SELECT summary FROM pipeline_runs WHERE scheduled_date=? AND stage=? ORDER BY started_at DESC LIMIT 1",
                 (scheduled_date.isoformat(), stage))
    return json.loads(row["summary"]) if row else {}


def write_execution_summary(settings: Settings, summary: dict, name: str = "run-summary.json") -> Path:
    """Machine-readable summary (non-sensitive) + GitHub step summary when available."""
    out = settings.root / settings.runtime.log_dir / name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2, default=str, sort_keys=True), encoding="utf-8")
    step = os.environ.get("GITHUB_STEP_SUMMARY")
    if step:
        with open(step, "a", encoding="utf-8") as fh:
            fh.write(f"### research-agent: {summary.get('stage', '')} ({summary.get('scheduled_date', '')})\n\n")
            fh.write("| key | value |\n|---|---|\n")
            for k, v in sorted(summary.items()):
                if isinstance(v, (dict, list)):
                    v = json.dumps(v, default=str)[:300]
                fh.write(f"| {k} | {str(v).replace('|', '/')} |\n")
            fh.write("\n")
    return out
