"""Durable analysis queue for Claude assessments.

Job identity = (paper, metadata_version, rubric_version, prompt_version, model): the same
paper re-discovered through other sources never creates a second job. States:
pending -> in_progress (leased) -> completed | retryable_failed | permanent_failed;
superseded when the paper's metadata changes or it ages out of the recommendation horizon.

Failure categories:
  auth / quota / action_failure / timeout / max_turns -> outage: no format-attempt consumed,
      retried on the next eligible run (retry_after), never marked completed.
  malformed / ungrounded / missing_output -> format failure: exponential backoff,
      permanent_failed after `max_format_attempts`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from research_agent.storage.database import Database
from research_agent.timeutil import iso_utc, utcnow

OUTAGE = {"auth", "quota", "budget", "action_failure", "timeout", "max_turns", "cancelled"}
FORMAT = {"malformed", "ungrounded", "missing_output", "identity_mismatch"}


@dataclass
class QueuePolicy:
    lease_minutes: int = 240
    max_format_attempts: int = 3
    backoff_base_hours: float = 6
    backoff_max_hours: float = 72
    quota_retry_hours: float = 20

    @classmethod
    def from_config(cls, q: dict) -> QueuePolicy:
        return cls(**{k: q[k] for k in cls.__dataclass_fields__ if k in q})


def enqueue(db: Database, *, paper_id: int, canonical_id: str, metadata_version: str, rubric_version: str,
            prompt_version: str, model: str, priority: float, intended_report_date: date) -> bool:
    """Insert a pending job unless an identical job (any status) or a cached evaluation exists."""
    cached = db.scalar(
        "SELECT 1 FROM evaluations WHERE paper_id=? AND metadata_version=? AND rubric_version=? AND prompt_version=? AND model=?",
        (paper_id, metadata_version, rubric_version, prompt_version, model))
    if cached:
        return False
    now = iso_utc()
    with db.transaction():
        # Older metadata versions of the same paper are superseded (materially revised paper).
        db.execute("UPDATE analysis_jobs SET status='superseded', updated_at=? WHERE paper_id=? AND metadata_version!=?"
                   " AND status IN ('pending','retryable_failed')", (now, paper_id, metadata_version))
        cur = db.execute(
            "INSERT OR IGNORE INTO analysis_jobs(paper_id, canonical_id, metadata_version, rubric_version, prompt_version,"
            " model, status, priority, intended_report_date, created_at, updated_at) VALUES (?,?,?,?,?,?,'pending',?,?,?,?)",
            (paper_id, canonical_id, metadata_version, rubric_version, prompt_version, model, priority,
             intended_report_date.isoformat(), now, now))
    return cur.rowcount == 1


def recover_expired_leases(db: Database, now: datetime | None = None) -> int:
    now_s = iso_utc(now or utcnow())
    cur = db.execute("UPDATE analysis_jobs SET status='pending', leased_by_run=NULL, lease_expires_at=NULL,"
                     " failure_category='lease_expired', updated_at=? WHERE status='in_progress' AND lease_expires_at < ?",
                     (now_s, now_s))
    return cur.rowcount


def lease(db: Database, *, run_id: str, limit: int, policy: QueuePolicy, now: datetime | None = None,
          rubric_version: str | None = None, prompt_version: str | None = None, model: str | None = None) -> list[dict]:
    """Lease eligible jobs: oldest intended report date first (catch-up), then priority."""
    now = now or utcnow()
    now_s = iso_utc(now)
    expires = iso_utc(now + timedelta(minutes=policy.lease_minutes))
    with db.transaction():
        recover_expired_leases(db, now)
        cond = ["status IN ('pending','retryable_failed')", "(retry_after IS NULL OR retry_after <= ?)"]
        params: list = [now_s]
        for col, val in (("rubric_version", rubric_version), ("prompt_version", prompt_version), ("model", model)):
            if val is not None:
                cond.append(f"{col}=?")
                params.append(val)
        rows = db.query(
            f"SELECT * FROM analysis_jobs WHERE {' AND '.join(cond)}"
            " ORDER BY intended_report_date ASC, priority DESC, id ASC LIMIT ?", (*params, limit))
        for r in rows:
            db.execute("UPDATE analysis_jobs SET status='in_progress', leased_by_run=?, lease_expires_at=?,"
                       " attempts=attempts+1, updated_at=? WHERE id=?", (run_id, expires, now_s, r["id"]))
    return [dict(r) for r in rows]


def complete(db: Database, job_id: int, evaluation_id: int) -> None:
    db.execute("UPDATE analysis_jobs SET status='completed', evaluation_id=?, failure_category=NULL, last_error=NULL,"
               " lease_expires_at=NULL, retry_after=NULL, updated_at=? WHERE id=?", (evaluation_id, iso_utc(), job_id))


def fail(db: Database, job_id: int, category: str, error: str, policy: QueuePolicy,
         now: datetime | None = None) -> str:
    """Record a failure; returns the resulting status."""
    now = now or utcnow()
    row = db.one("SELECT format_failures, outage_failures FROM analysis_jobs WHERE id=?", (job_id,))
    if row is None:
        raise KeyError(job_id)
    if category in FORMAT:
        n = row["format_failures"] + 1
        if n >= policy.max_format_attempts:
            status, retry = "permanent_failed", None
        else:
            hours = min(policy.backoff_max_hours, policy.backoff_base_hours * 2 ** (n - 1))
            status, retry = "retryable_failed", iso_utc(now + timedelta(hours=hours))
        db.execute("UPDATE analysis_jobs SET status=?, format_failures=?, failure_category=?, last_error=?, retry_after=?,"
                   " lease_expires_at=NULL, updated_at=? WHERE id=?",
                   (status, n, category, error[:500], retry, iso_utc(now), job_id))
        return status
    # Outages never consume format attempts and never mark work as done.
    n = row["outage_failures"] + 1
    hours = policy.quota_retry_hours if category in ("auth", "quota") else min(
        policy.backoff_max_hours, policy.backoff_base_hours * 2 ** min(n - 1, 4))
    db.execute("UPDATE analysis_jobs SET status='retryable_failed', outage_failures=?, failure_category=?, last_error=?,"
               " retry_after=?, lease_expires_at=NULL, updated_at=? WHERE id=?",
               (n, category, error[:500], iso_utc(now + timedelta(hours=hours)), iso_utc(now), job_id))
    return "retryable_failed"


def release_for_retry(db: Database, *, only_eligible: bool = True, now: datetime | None = None) -> int:
    """Manual `queue retry`: clear retry_after on retryable jobs (never resurrects permanent failures
    unless only_eligible=False, which is an explicit operator decision)."""
    now_s = iso_utc(now or utcnow())
    statuses = "('retryable_failed')" if only_eligible else "('retryable_failed','permanent_failed')"
    cur = db.execute(f"UPDATE analysis_jobs SET status='pending', retry_after=NULL, updated_at=? WHERE status IN {statuses}",
                     (now_s,))
    return cur.rowcount


def expire_out_of_horizon(db: Database, cutoff: date) -> int:
    """Pending jobs for papers published before `cutoff` can no longer be recommended."""
    cur = db.execute(
        "UPDATE analysis_jobs SET status='superseded', failure_category='expired_out_of_horizon', updated_at=?"
        " WHERE status IN ('pending','retryable_failed') AND paper_id IN"
        " (SELECT id FROM papers WHERE first_publication_date IS NOT NULL AND first_publication_date < ?)",
        (iso_utc(), cutoff.isoformat()))
    return cur.rowcount


def status_counts(db: Database) -> dict:
    counts = {r["status"]: r["n"] for r in db.query("SELECT status, COUNT(*) n FROM analysis_jobs GROUP BY status")}
    oldest = db.scalar("SELECT MIN(p.discovered_at) FROM analysis_jobs j JOIN papers p ON p.id=j.paper_id"
                       " WHERE j.status IN ('pending','retryable_failed','in_progress')")
    cats = {r["failure_category"]: r["n"] for r in db.query(
        "SELECT failure_category, COUNT(*) n FROM analysis_jobs WHERE status IN ('retryable_failed','permanent_failed')"
        " GROUP BY failure_category")}
    return {"by_status": counts, "oldest_pending_discovery": oldest, "failure_categories": cats}
