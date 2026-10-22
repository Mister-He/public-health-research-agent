"""Journal impact: open metrics, the trusted list, the block list and the minimum floor.

Metric: OpenAlex `summary_stats.2yr_mean_citedness` — citations in the current year to items
published in the previous two years, divided by the number of those items. It is an open
analogue of the (proprietary) Journal Impact Factor; values differ from Clarivate's, ranking is
similar. Metrics are cached per ISSN in `journal_metrics` and refreshed after 30 days.

Scoring (deterministic):
  preprint                         -> preprint_score      (neutral-low; still eligible)
  journal in config/journals.yaml  -> max(listed_journal_min_score, scaled)
  metric known                     -> scaled = 100 * min(1, ln(1+c) / ln(1+full_score_citedness))
  no ISSN / not in OpenAlex        -> unknown_score
Floor (before Claude, and as hard eligibility): an UNLISTED journal with a KNOWN metric below
min_two_year_mean_citedness is excluded; blocked journals are always excluded.
"""

from __future__ import annotations

import logging
import math
from datetime import timedelta

from research_agent.metadata.schemas import Paper
from research_agent.settings import Settings
from research_agent.storage.database import Database
from research_agent.timeutil import iso_utc, utcnow

log = logging.getLogger(__name__)
REFRESH_DAYS = 30


def missing_issns(db: Database, issns: set[str]) -> list[str]:
    cutoff = iso_utc(utcnow() - timedelta(days=REFRESH_DAYS))
    fresh = {r[0] for r in db.query("SELECT issn FROM journal_metrics WHERE retrieved_at >= ?", (cutoff,))}
    return sorted(i for i in issns if i and i not in fresh)


def fetch_metrics(db: Database, openalex, issns: list[str], limit: int = 300) -> int:
    """Look up and cache metrics for ISSNs. Failures are left uncached (retried next run)."""
    n = 0
    for issn in issns[:limit]:
        try:
            src = openalex.source_by_issn(issn)
        except Exception as exc:  # provider down / budget: keep going with what we have
            log.warning("journal metric lookup failed", extra={"issn": issn, "error": str(exc)[:160]})
            break
        db.execute(
            "INSERT INTO journal_metrics(issn, found, openalex_source_id, display_name, two_year_mean_citedness, h_index,"
            " is_in_doaj, source_type, retrieved_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(issn) DO UPDATE SET"
            " found=excluded.found, openalex_source_id=excluded.openalex_source_id, display_name=excluded.display_name,"
            " two_year_mean_citedness=excluded.two_year_mean_citedness, h_index=excluded.h_index,"
            " is_in_doaj=excluded.is_in_doaj, source_type=excluded.source_type, retrieved_at=excluded.retrieved_at",
            (issn, int(src is not None), (src or {}).get("openalex_source_id"), (src or {}).get("display_name"),
             (src or {}).get("two_year_mean_citedness"), (src or {}).get("h_index"),
             None if src is None or src.get("is_in_doaj") is None else int(src["is_in_doaj"]),
             (src or {}).get("source_type"), iso_utc()))
        n += 1
    return n


def journal_context(db: Database, settings: Settings, paper: Paper) -> dict:
    """Everything the impact score and the floor need, from config + cached metrics only."""
    issns = set(paper.journal_issn or [])
    listed = bool(issns & settings.journals.listed_issns())
    blocked = bool(issns & settings.journals.blocked_issns())
    best = None
    if issns and not paper.is_preprint:
        rows = db.query(f"SELECT * FROM journal_metrics WHERE found=1 AND two_year_mean_citedness IS NOT NULL"
                        f" AND issn IN ({','.join('?' * len(issns))}) ORDER BY two_year_mean_citedness DESC, issn",
                        tuple(sorted(issns)))
        best = dict(rows[0]) if rows else None
    return {"preprint": paper.is_preprint, "listed": listed, "blocked": blocked,
            "citedness": best["two_year_mean_citedness"] if best else None,
            "h_index": best["h_index"] if best else None,
            "metric_source": "OpenAlex 2-year mean citedness" if best else None}


def impact_score(j: dict, params: dict) -> float:
    if j["preprint"]:
        return float(params.get("preprint_score", 40))
    c = j["citedness"]
    if c is None:
        base = float(params.get("unknown_score", 30))
    else:
        full = float(params.get("full_score_citedness", 15))
        base = 100.0 * min(1.0, math.log1p(max(c, 0.0)) / math.log1p(full))
    if j["listed"]:
        base = max(base, float(params.get("listed_journal_min_score", 85)))
    return round(base, 6)


def floor_reason(j: dict, settings: Settings) -> str | None:
    """Hard rule: blocked journals, and unlisted journals with a known metric below the floor."""
    if j["blocked"]:
        return "journal is on the block list"
    f = settings.ranking.journal_floor
    if not f.get("enabled", True) or j["preprint"] or j["listed"] or j["citedness"] is None:
        return None
    minimum = float(f.get("min_two_year_mean_citedness", 2.0))
    if j["citedness"] < minimum:
        return f"journal impact {j['citedness']:.2f} < floor {minimum:.2f}"
    return None


def describe(j: dict) -> str:
    if j["preprint"]:
        return "Preprint (no journal impact)"
    parts = []
    if j["citedness"] is not None:
        parts.append(f"OpenAlex 2-yr mean citedness {j['citedness']:.1f}")
        if j["h_index"] is not None:
            parts.append(f"h-index {j['h_index']}")
    else:
        parts.append("No journal metric available")
    if j["listed"]:
        parts.append("on your journal list")
    return "; ".join(parts)
