"""Publication gate: reads the rendered artifact AND the persisted records, independently.

Fail-closed: any report-level failure blocks publication; any paper-level failure (identity,
fabricated/mismatched metadata, DOI/URL mismatch, score error) blocks that paper (the pipeline
then excludes it, refills and re-renders). Warnings (e.g. a bot-protected publisher page whose
identity was confirmed via Crossref) do not block."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from research_agent.reporting.renderer import load_items, paper_view
from research_agent.settings import Settings
from research_agent.storage.database import Database
from research_agent.timeutil import iso_utc
from research_agent.trends.updater import latest_snapshot
from research_agent.validation.links import LinkVerifier, link_check_status
from research_agent.validation.markdown import check_structure
from research_agent.validation.metadata import IdentityVerifier, check_provenance, check_rendered_fields
from research_agent.validation.results import Check, GateResult
from research_agent.validation.scoring import validate_scores


def run_gate(db: Database, settings: Settings, report_date: date, path: Path, text: str, *,
             identity: IdentityVerifier | None = None, links: LinkVerifier | None = None,
             final_dir: Path | None = None) -> GateResult:
    res = GateResult()
    items = load_items(db, settings, report_date)
    dim_names = {d.id: d.name for d in settings.scoring.dimensions}
    parsed, checks = check_structure(
        path if final_dir is None else final_dir / path.name, text, report_date, settings.reports_dir,
        expected_count=len(items), enabled_dimensions=[d.id for d in settings.scoring.enabled], dimension_names=dim_names)
    res.checks.extend(checks)
    snap = latest_snapshot(db, on_or_before=report_date)
    snap_tags = snap.tags if snap else {}
    by_id = {pp.fields.get("Canonical ID", "").strip("`"): pp for pp in parsed.papers}
    rec_rows = {r["canonical_id"]: r for r in db.query(
        "SELECT * FROM recommendations WHERE report_date=? AND status IN ('selected','published')",
        (report_date.isoformat(),))}
    for it in items:
        p = it.paper
        pp = by_id.get(p.canonical_id)
        if pp is None:
            res.checks.append(Check("section_present", p.canonical_id, "fail", "selected paper missing from report"))
            continue
        res.checks.extend(check_rendered_fields(pp, p, paper_view(it, settings, snap_tags), rec_rows.get(p.canonical_id)))
        res.checks.extend(check_provenance(db, p))
        id_check = identity.verify(p) if identity else Check("identity", p.canonical_id, "warn",
                                                             "online identity check disabled (offline mode)")
        res.checks.append(id_check)
        if links:
            lr = links.check(p.canonical_url, doi=p.doi, title=p.title, identity_verified=id_check.status == "pass")
            res.checks.append(Check(f"url:{lr.status}", p.canonical_id, link_check_status(lr), lr.detail))
    unknown = set(by_id) - {it.paper.canonical_id for it in items}
    if unknown:
        res.checks.append(Check("no_fabricated_citations", "report", "fail",
                                f"report contains papers that are not persisted selections: {sorted(unknown)}"))
    else:
        res.checks.append(Check("no_fabricated_citations", "report", "pass"))
    res.checks.extend(validate_scores(db, settings, report_date))
    return res


def persist_results(db: Database, report_date: date, res: GateResult, *, run_id: str | None, round_no: int) -> None:
    now = iso_utc()
    with db.transaction():
        for c in res.checks:
            db.execute("INSERT INTO validation_results(report_date, run_id, round, check_name, target, status, detail,"
                       " created_at) VALUES (?,?,?,?,?,?,?,?)",
                       (report_date.isoformat(), run_id, round_no, c.name, c.target, c.status, c.detail[:1000], now))
