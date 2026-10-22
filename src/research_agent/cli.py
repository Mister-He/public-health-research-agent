"""research-agent command-line interface.

Exit codes: 0 ok | 1 blocking failure (validation/publication) | 2 configuration error |
3 analysis deferred (Claude auth/quota/outage; ingestion state saved) | 4 no report (coverage) |
5 stale data branch after retries."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from research_agent import __version__
from research_agent.settings import ConfigError, Settings, load_settings
from research_agent.timeutil import local_today

log = logging.getLogger("research_agent.cli")


def _date(s: str | None) -> date:
    if not s:
        return local_today()
    try:
        return date.fromisoformat(s)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid date {s!r} (expected YYYY-MM-DD)") from exc


def _db(settings: Settings, override: str | None = None):
    from research_agent.storage.database import open_db

    return open_db(Path(override) if override else settings.db_path)


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


def _gh_output(**kv) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            for k, v in kv.items():
                if "\n" in str(v):
                    fh.write(f"{k}<<__EOF__\n{v}\n__EOF__\n")
                else:
                    fh.write(f"{k}={v}\n")


def _run_id(args) -> str:
    return args.run_id or os.environ.get("RESEARCH_AGENT_RUN_ID") or f"local-{local_today().isoformat()}"


# ------------------------------------------------------------------------------------------ commands

def cmd_init(s: Settings, args) -> int:
    from research_agent.storage.database import dumps
    from research_agent.timeutil import iso_utc
    from research_agent.trends.taxonomy import sync_tags

    db = _db(s, args.db)
    with db.transaction():
        sync_tags(db, s)
        db.execute("INSERT OR IGNORE INTO research_profiles(profile_hash, profile_version, content, first_seen_at)"
                   " VALUES (?,?,?,?)", (s.config_hash, s.profile.profile_version, s.profile.model_dump_json(), iso_utc()))
        db.execute("INSERT OR IGNORE INTO scoring_configs(scoring_version, content, first_seen_at) VALUES (?,?,?)",
                   (s.scoring.version(), dumps([d.model_dump() for d in s.scoring.dimensions]), iso_utc()))
    s.reports_dir.mkdir(parents=True, exist_ok=True)
    _print({"database": str(db.path), "schema_version": db.schema_version(), "config_hash": s.config_hash,
            "scoring_version": s.scoring.version(), "topics": len(s.profile.topics), "journals": len(s.journals.journals),
            "trend_tags": len(s.trends.tags)})
    return 0


def cmd_doctor(s: Settings, args) -> int:
    import subprocess

    import httpx

    problems, info = [], {}
    info["config"] = f"ok (hash {s.config_hash})"
    if os.environ.get("ANTHROPIC_API_KEY"):
        problems.append("ANTHROPIC_API_KEY is set: unset it to avoid accidental pay-as-you-go billing")
    in_ci = os.environ.get("GITHUB_ACTIONS") == "true"
    info["claude_oauth_secret"] = ("present" if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") else
                                   "not visible here (expected: it is passed only to the claude-code-action step)")
    info["contact_email"] = "set" if s.contact_email else "NOT SET (set RESEARCH_AGENT_CONTACT_EMAIL)"
    if not s.contact_email:
        problems.append("RESEARCH_AGENT_CONTACT_EMAIL is not set (required for NCBI/Crossref/OpenAlex etiquette)")
    for k in ("NCBI_API_KEY", "OPENALEX_API_KEY"):
        info[k] = "set" if os.environ.get(k) else "not set (optional)"
    info["llm_provider"] = f"{s.llm.provider} ({s.llm.auth}), model {s.llm.active_model}"
    if s.llm.provider == "deepseek":
        k = s.llm.deepseek.api_key_env
        info[k] = "set" if os.environ.get(k) else "NOT SET"
        if not os.environ.get(k) and not in_ci:
            problems.append(f"provider is deepseek but {k} is not set (pay-per-use API key)")
    try:
        db = _db(s, args.db)
        ok = db.scalar("PRAGMA integrity_check")
        info["database"] = f"{db.path} schema v{db.schema_version()} integrity={ok}"
        if ok != "ok":
            problems.append("database integrity check failed")
    except Exception as exc:
        problems.append(f"database: {exc}")
    probes = {"pubmed": "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/einfo.fcgi?retmode=json",
              "europe_pmc": "https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=malaria&format=json&pageSize=1",
              "openalex": "https://api.openalex.org/works?per_page=1&select=id",
              "crossref": "https://api.crossref.org/works?rows=0",
              "medrxiv": "https://api.medrxiv.org/details/medrxiv/2025-01-01/2025-01-01/0"}
    conn = {}
    if not args.offline:
        with httpx.Client(timeout=20, headers={"User-Agent": f"research-agent/{__version__} doctor"}) as c:
            for name, url in probes.items():
                try:
                    r = c.get(url)
                    conn[name] = f"HTTP {r.status_code}"
                    if r.status_code >= 400:
                        problems.append(f"{name} probe returned HTTP {r.status_code}")
                except httpx.HTTPError as exc:
                    conn[name] = f"unreachable ({type(exc).__name__})"
                    problems.append(f"{name} unreachable")
    info["connectivity"] = conn or "skipped (--offline)"
    remote = subprocess.run(["git", "remote", "get-url", s.runtime.git.get("remote", "origin")], cwd=s.root,
                            capture_output=True, text=True)
    info["git_remote"] = remote.stdout.strip() or "none configured"
    if remote.returncode == 0 and not args.offline:
        br = subprocess.run(["git", "ls-remote", "--heads", "origin", s.runtime.data_branch["name"]], cwd=s.root,
                            capture_output=True, text=True)
        info["data_branch"] = "exists" if br.stdout.strip() else "not created yet (first CI run creates it)"
    info["ci"] = in_ci
    _print({"info": info, "problems": problems})
    return 1 if problems else 0


def cmd_fetch(s: Settings, args, *, skip_llm: bool = True) -> int:
    from research_agent.pipeline.runner import ingest_stage

    db = _db(s, args.db)
    summ = ingest_stage(db, s, args.date, run_id=_run_id(args), skip_llm=skip_llm,
                        max_eval=getattr(args, "max_eval", None), only=set(args.only) if getattr(args, "only", None) else None,
                        time_budget_s=getattr(args, "time_budget", None))
    _print({k: v for k, v in summ.items() if k not in ("provider_outcomes", "retrieval_watermarks_by_source_query")})
    return 0 if summ["successful_providers"] else 1


def cmd_landscape(s: Settings, args) -> int:
    from research_agent.pipeline.runner import ingest_stage, write_landscape_file

    db = _db(s, args.db)
    if args.force:
        db.execute("DELETE FROM state_meta WHERE key IN ('landscape_completed_at','landscape_report_md')")
    s = s.model_copy(deep=True)
    s.runtime.windows["initial_baseline_days"] = args.lookback_days
    summ = ingest_stage(db, s, args.date, run_id=_run_id(args), skip_llm=True, time_budget_s=args.time_budget)
    path = write_landscape_file(db, s)
    _print({"landscape": summ["landscape"], "caught_up": summ["caught_up"], "report": str(path) if path else None,
            "failed_providers": summ["failed_providers"]})
    return 0 if path else 4


def cmd_score(s: Settings, args) -> int:
    from research_agent.llm import claude_code_action as cca

    db = _db(s, args.db)
    run_id = _run_id(args)
    m = cca.build_scoring_manifest(db, s, run_id=run_id, report_date=args.date, limit=args.max_eval)
    out = {"manifest_papers": m["paper_count"], "manifest": str(s.analysis_dir / "scoring" / "manifest.json")}
    if args.provider == "fixture":
        from research_agent.llm.fixture_provider import simulate_scoring

        simulate_scoring(s)  # s already uses the fixture model id (see main)
        cca.write_status(s, cca.ActionStatus(stage="scoring", outcome="success"))
    if args.provider == "deepseek":
        from research_agent.llm.deepseek_provider import run_stage

        if s.llm.provider != "deepseek":
            print("set `provider: deepseek` and `auth: api_key` in config/llm.yaml first (pay-per-use)", file=sys.stderr)
            return 2
        out["deepseek"] = run_stage(s, "scoring").to_json()
    if args.provider in ("fixture", "import", "deepseek"):
        st = cca.read_status(s, "scoring") or cca.ActionStatus(stage="scoring", outcome="success")
        imp = cca.import_scoring_outputs(db, s, run_id=run_id, status=st)
        out["import"] = imp.__dict__
    _print(out)
    return 0


def cmd_trends(s: Settings, args) -> int:
    from research_agent.trends.updater import update_trends

    db = _db(s, args.db)
    snap = update_trends(db, s, args.date, kind="weekly" if args.weekly else "daily", run_id=_run_id(args))
    _print({"snapshot_id": snap.id, "kind": snap.kind, "version": snap.version, "corpus": snap.corpus_size,
            "top": sorted(((k, round(v["score"], 3)) for k, v in snap.tags.items()), key=lambda x: -x[1])[:12]})
    return 0


def cmd_digest_generate(s: Settings, args) -> int:
    from research_agent.pipeline.runner import publish_stage

    db = _db(s, args.db)
    res = publish_stage(db, s, args.date, run_id=_run_id(args), online=not args.offline, dry_run=not args.publish,
                        force=args.force)
    _print({"status": res.status, "path": str(res.report_path) if res.report_path else None,
            "validation": res.gate.summary() if res.gate else None,
            "failures": [c.__dict__ for c in res.gate.failures][:20] if res.gate else [],
            "reason": res.summary.get("reason")})
    return res.exit_code


def cmd_digest_validate(s: Settings, args) -> int:
    from research_agent.pipeline.checkpoints import make_http, make_providers
    from research_agent.validation.gate import run_gate
    from research_agent.validation.links import LinkVerifier
    from research_agent.validation.metadata import IdentityVerifier

    db = _db(s, args.db)
    path = Path(args.path) if args.path else s.reports_dir / f"{args.date.isoformat()}.md"
    if not path.exists():
        print(f"report not found: {path}", file=sys.stderr)
        return 1
    identity = links = None
    if not args.offline:
        prov = make_providers(s, make_http(s))
        identity = IdentityVerifier(prov.get("crossref"), prov.get("pubmed"), prov.get("medrxiv"))
        links = LinkVerifier(timeout=s.validation.url_timeout_seconds) if s.validation.url_checks else None
    res = run_gate(db, s, args.date, path, path.read_text(encoding="utf-8"), identity=identity, links=links)
    _print({"report": str(path), **res.summary(), "failures": [c.__dict__ for c in res.failures],
            "warnings": [c.__dict__ for c in res.warnings][:30]})
    return 0 if res.passed else 1


def cmd_run(s: Settings, args) -> int:
    """Local end-to-end run. Claude Code Action only runs in CI; locally use --provider fixture
    (simulated, for testing) or --skip-llm (reuses cached evaluations only)."""
    from research_agent.llm import claude_code_action as cca
    from research_agent.pipeline.runner import ingest_stage, publish_stage

    db = _db(s, args.db)
    run_id = _run_id(args)
    if not args.no_fetch:
        ingest_stage(db, s, args.date, run_id=run_id, skip_llm=args.skip_llm, max_eval=args.max_eval)
    elif not args.skip_llm:
        from research_agent.retrieval.screening import shortlist_and_enqueue

        shortlist_and_enqueue(db, s, args.date)
        cca.build_scoring_manifest(db, s, run_id=run_id, report_date=args.date, limit=args.max_eval)
    if not args.skip_llm and args.provider == "fixture":
        from research_agent.llm.fixture_provider import simulate_scoring

        simulate_scoring(s)
        cca.write_status(s, cca.ActionStatus(stage="scoring", outcome="success"))
    res = publish_stage(db, s, args.date, run_id=run_id, online=not args.offline, dry_run=args.dry_run, force=args.force)
    _print({k: v for k, v in res.summary.items() if k in ("status", "selected_count", "report", "reason", "validation",
                                                          "failures", "claude_action_failure_category")})
    return res.exit_code


def cmd_backfill(s: Settings, args) -> int:
    from research_agent.pipeline.runner import publish_stage

    db = _db(s, args.db)
    if args.end < args.start or (args.end - args.start).days > s.runtime.windows["backfill_max_days"]:
        print("invalid range (end before start, or longer than backfill_max_days)", file=sys.stderr)
        return 2
    results = {}
    d = args.start
    worst = 0
    while d <= args.end:
        res = publish_stage(db, s, d, run_id=f"{_run_id(args)}-backfill-{d}", online=not args.offline,
                            dry_run=args.dry_run, strict_as_of=True)
        results[d.isoformat()] = res.status
        worst = max(worst, 1 if res.exit_code == 1 else 0)
        d += timedelta(days=1)
    _print(results)
    return worst


def cmd_queue(s: Settings, args) -> int:
    from research_agent.storage import analysis_queue as q

    db = _db(s, args.db)
    if args.action == "status":
        _print(q.status_counts(db))
    else:
        n = q.release_for_retry(db, only_eligible=not args.all)
        _print({"released": n, "note": "permanent failures included" if args.all else "retryable jobs only"})
    return 0


def cmd_tags(s: Settings, args) -> int:
    from research_agent.trends import taxonomy as tx

    db = _db(s, args.db)
    a = args.action
    if a == "list":
        rows = db.query("SELECT id, name, status, pinned, seed, current_score, frequency_30d, growth_signal FROM topic_tags"
                        " ORDER BY status, current_score DESC")
        _print([dict(r) for r in rows])
        return 0
    if a == "proposals":
        _print([dict(r) for r in db.query("SELECT id, name, description, status, proposed_at, supporting_paper_ids"
                                         " FROM tag_proposals ORDER BY proposed_at DESC")])
        return 0
    if a == "add":
        match = [[p.strip() for p in conj.split("+")] for conj in args.match]
        print(tx.add_tag(s, args.name, args.description or args.name, match, status=args.status))
    elif a == "rename":
        tx.update_tag(s, args.tag, name=args.name)
    elif a == "merge":
        tx.merge_tags(s, args.tag, args.into)
    elif a in ("archive", "reactivate", "watch"):
        tx.update_tag(s, args.tag, status={"archive": "archived", "reactivate": "active", "watch": "watchlist"}[a])
    elif a in ("pin", "unpin"):
        tx.update_tag(s, args.tag, pinned=a == "pin")
    elif a == "override":
        tx.update_tag(s, args.tag, manual_override=None if args.value < 0 else args.value)
    elif a == "accept":
        print(tx.accept_proposal(db, s, args.proposal, tag_id=args.tag_id))
    elif a == "reject":
        tx.reject_proposal(db, args.proposal)
    print("config/trends.yaml updated; commit it to make the change effective in CI." if a not in ("reject",) else "ok")
    return 0


def cmd_feedback(s: Settings, args) -> int:
    from research_agent.evaluation import feedback as fb

    if args.action == "add":
        fb.add_feedback(s, args.canonical_id, args.label, args.note or "", args.date_str or "")
        print(f"appended to {fb.feedback_path(s)}")
        return 0
    db = _db(s, args.db)
    if args.action == "import":
        print(f"imported {fb.import_feedback(db, s)} new feedback rows")
    else:
        fb.import_feedback(db, s)
        p = fb.suggest_adjustments(db, s)
        print(f"proposal written to {p} (not applied)" if p else "not enough feedback for proposals")
    return 0


def cmd_evaluate(s: Settings, args) -> int:
    from research_agent.evaluation.metrics import evaluate

    _print(evaluate(_db(s, args.db), Path(args.gold) if args.gold else s.root / "feedback" / "gold.csv"))
    return 0


def cmd_replay(s: Settings, args) -> int:
    from research_agent.evaluation.replay import replay

    _print(replay(s, Path(args.db) if args.db else s.db_path, args.date, retrospective=args.retrospective))
    return 0


def cmd_history(s: Settings, args) -> int:
    db = _db(s, args.db)
    if args.paper:
        rows = db.query("SELECT report_date, position, raw_score, adjusted_score, status, reason FROM recommendations"
                        " WHERE canonical_id=? ORDER BY report_date", (args.paper,))
        prov = db.query("SELECT provider, query_id, strategy, first_seen_at, times_seen FROM paper_discoveries d JOIN papers p"
                        " ON p.id=d.paper_id WHERE p.canonical_id=?", (args.paper,))
        evals = db.query("SELECT e.model, e.prompt_version, e.rubric_version, e.created_at, e.run_id FROM evaluations e"
                         " JOIN papers p ON p.id=e.paper_id WHERE p.canonical_id=?", (args.paper,))
        checks = db.query("SELECT report_date, check_name, status, detail FROM validation_results WHERE target=?"
                          " ORDER BY id DESC LIMIT 40", (args.paper,))
        _print({"recommendations": [dict(r) for r in rows], "discovered_by": [dict(r) for r in prov],
                "evaluations": [dict(r) for r in evals], "validation": [dict(r) for r in checks]})
    else:
        _print([dict(r) for r in db.query("SELECT report_date, status, paper_count, validation_status, path FROM daily_reports"
                                          " ORDER BY report_date DESC LIMIT ?", (args.days,))])
    return 0


def cmd_state(s: Settings, args) -> int:
    from research_agent.git.publisher import DataBranch
    from research_agent.storage.database import open_db

    dbr = DataBranch(s.root, s.runtime.data_branch["name"], s.runtime.git.get("remote", "origin"),
                     s.runtime.data_branch.get("snapshot_dir", "state"))
    if args.action == "restore":
        sha = dbr.restore(s.db_path)
        print(f"restored {s.db_path} from {dbr.branch}@{sha[:8]}" if sha else "data branch not found; nothing restored")
    else:
        # Parent = current remote head: a push is rejected (never forced) if CI moved it meanwhile.
        dbr.fetch_base()
        sha = dbr.persist(open_db(s.db_path), args.message or "chore(data): persist manually edited state")
        print(f"persisted to {dbr.branch}: {sha}")
    return 0


# ------------------------------------------------------------------------------------------ CI commands

def _with_data_branch(s: Settings, stage_fn, message: str, *, persist: bool = True):
    """restore -> stage -> persist, replaying the (idempotent) stage on stale-branch conflicts."""
    from research_agent.git.publisher import DataBranch, StaleDataBranch
    from research_agent.storage.database import open_db

    dbr = DataBranch(s.root, s.runtime.data_branch["name"], s.runtime.git.get("remote", "origin"),
                     s.runtime.data_branch.get("snapshot_dir", "state"))
    retries = int(s.runtime.data_branch.get("push_retries", 3))
    for attempt in range(retries + 1):
        s.db_path.unlink(missing_ok=True)
        dbr.restore(s.db_path)
        db = open_db(s.db_path)
        result = stage_fn(db)
        if not persist:
            return result, None
        try:
            sha = dbr.persist(db, message)
            db.close()
            return result, sha
        except StaleDataBranch as exc:
            log.warning("data branch moved; reloading and replaying", extra={"attempt": attempt + 1, "error": str(exc)})
            db.close()
    raise SystemExit(5)


def cmd_ci(s: Settings, args) -> int:
    from research_agent.llm import claude_code_action as cca
    from research_agent.pipeline import runner
    from research_agent.pipeline.state import write_execution_summary

    run_id = _run_id(args)
    d = args.date
    a = args.action
    if a == "claude-args":
        _gh_output(args=cca.claude_args(s, args.stage), prompt=cca.action_prompt(s, args.stage))
        print(cca.claude_args(s, args.stage))
        return 0
    if a == "provider":
        _gh_output(provider=s.llm.provider)
        print(s.llm.provider)
        return 0
    if a == "analyze":
        if s.llm.provider != "deepseek":
            print(f"`ci analyze` runs API providers; configured provider is {s.llm.provider}", file=sys.stderr)
            return 2
        from research_agent.llm.deepseek_provider import run_stage

        st = run_stage(s, args.stage)
        if st.category:
            print(f"::error title=DeepSeek {args.stage}: {st.category}::{st.error_message or st.detail}")
        _print(st.to_json())
        return 0
    if a == "classify":
        st = cca.classify_execution(args.stage, args.outcome, Path(args.execution_file) if args.execution_file else None)
        cca.write_status(s, st)
        if st.category and os.environ.get("GITHUB_ACTIONS") == "true":
            hint = {"auth": "regenerate the token with `claude setup-token` and update secret CLAUDE_CODE_OAUTH_TOKEN",
                    "quota": "subscription usage limit reached; work stays queued and is retried next run"}.get(st.category, "")
            print(f"::error title=Claude {args.stage}: {st.category}::{st.error_message or st.detail}"
                  + (f" — {hint}" if hint else ""))
        _gh_output(category=st.category or "none")
        _print(st.to_json())
        return 0
    if a == "ingest":
        if args.reconcile:
            # Force metadata refresh: re-fetch the reconciliation window for every query.
            s.runtime.windows["daily_lookback_days"] = s.runtime.windows["reconciliation_days"]

        def stage(db):
            return runner.ingest_stage(db, s, d, run_id=run_id, skip_llm=args.skip_llm, max_eval=args.max_eval,
                                       time_budget_s=args.time_budget)
        summ, sha = _with_data_branch(s, stage, f"chore(data): persist ingestion state for {d} [{run_id}]")
        summ["data_commit"] = sha
        if args.snapshot:
            from research_agent.storage.database import open_db

            open_db(s.db_path).snapshot_to(Path(args.snapshot))
        write_execution_summary(s, summ, "ingest-summary.json")
        _gh_output(has_work=str(summ["manifest_papers"] > 0).lower(), report_date=d.isoformat())
        return 0 if summ["successful_providers"] else 1
    if a == "prepare":
        # Runs in the read-only Claude job from the snapshot artifact the ingest job pushed,
        # so that job needs no Git credentials. Nothing here is persisted.
        from research_agent.storage.database import open_db, restore_snapshot

        if not args.snapshot:
            print("--snapshot is required", file=sys.stderr)
            return 2
        s.db_path.unlink(missing_ok=True)
        restore_snapshot(Path(args.snapshot), s.db_path)
        db = open_db(s.db_path)
        n = runner.prepare_narrative(db, s, d, run_id=run_id)
        t = runner.prepare_trend_discovery(db, s, d)
        _gh_output(narrative=str(n is not None).lower(), trends=str(t is not None).lower())
        print(f"narrative manifest: {n is not None}; trend manifest: {t is not None}")
        return 0
    if a in ("publish", "backfill") and cca.read_status(s, "scoring") is None and args.analysis_result:
        # The analysis job did not produce a status file (skipped/cancelled/failed before classify).
        st = cca.classify_execution("scoring", {"success": "success", "skipped": "skipped"}.get(
            args.analysis_result, "cancelled" if args.analysis_result == "cancelled" else "failure"), None)
        if args.analysis_result == "skipped":
            st.category, st.outcome = None, "skipped"
        cca.write_status(s, st)
    if a == "validate":
        def stage(db):
            from research_agent.validation.gate import run_gate

            path = s.reports_dir / f"{d.isoformat()}.md"
            if not path.exists():
                return None
            prov = make_providers(s, make_http(s)) if not args.offline else {}
            idv = IdentityVerifier(prov.get("crossref"), prov.get("pubmed"), prov.get("medrxiv")) if prov else None
            return run_gate(db, s, d, path, path.read_text(encoding="utf-8"), identity=idv)
        from research_agent.pipeline.checkpoints import make_http, make_providers
        from research_agent.validation.metadata import IdentityVerifier

        res, _ = _with_data_branch(s, stage, "", persist=False)
        if res is None:
            print(f"no report for {d}", file=sys.stderr)
            return 1
        _print({**res.summary(), "failures": [c.__dict__ for c in res.failures]})
        return 0 if res.passed else 1
    if a == "backfill":
        from research_agent.git.publisher import commit_report

        if not args.start or not args.end or args.end < args.start:
            print("--start/--end required (start <= end)", file=sys.stderr)
            return 2

        def stage(db):
            out, cur = {}, args.start
            while cur <= args.end:
                out[cur] = runner.publish_stage(db, s, cur, run_id=f"{run_id}-bf-{cur}", online=not args.offline,
                                                dry_run=args.dry_run, strict_as_of=True)
                cur += timedelta(days=1)
            return out
        results, _ = _with_data_branch(s, stage, f"chore(data): persist backfill state {args.start}..{args.end} [{run_id}]",
                                       persist=not args.dry_run)
        for day, res in results.items():
            if res.status == "published" and not args.dry_run and not args.no_commit:
                commit_report(s.root, res.report_path, day.isoformat(), remote=s.runtime.git.get("remote", "origin"),
                              branch=s.runtime.git.get("report_branch", "main"))
        _print({k.isoformat(): v.status for k, v in results.items()})
        return max((r.exit_code for r in results.values() if r.exit_code == 1), default=0)
    if a == "publish":
        from research_agent.git.publisher import commit_report

        holder = {}

        def stage(db):
            res = runner.publish_stage(db, s, d, run_id=run_id, online=not args.offline, dry_run=args.dry_run,
                                       force=args.force)
            holder["landscape"] = runner.write_landscape_file(db, s)
            return res
        # Dry runs still persist validated analysis results (Claude work is never thrown away);
        # they only skip promotion and the report commit.
        res, sha = _with_data_branch(s, stage, f"chore(data): persist publication state for {d} [{run_id}]")
        res.summary["data_commit"] = sha
        if res.status == "published" and not args.dry_run and not args.no_commit:
            res.summary["report_commit"] = commit_report(s.root, res.report_path, d.isoformat(),
                                                         remote=s.runtime.git.get("remote", "origin"),
                                                         branch=s.runtime.git.get("report_branch", "main"))
        lp = holder.get("landscape")
        if lp and not args.dry_run and not args.no_commit:
            from research_agent.git.publisher import git

            rel = lp.relative_to(s.root).as_posix()
            if git("status", "--porcelain", "--", rel, cwd=s.root).stdout.strip():
                git("add", "--", rel, cwd=s.root)
                git("commit", "-q", "-m", f"docs(landscape): add initial research landscape baseline ({lp.stem})",
                    cwd=s.root)
                git("push", s.runtime.git.get("remote", "origin"), f"HEAD:refs/heads/{s.runtime.git.get('report_branch', 'main')}",
                    cwd=s.root)
        write_execution_summary(s, res.summary, "publish-summary.json")
        _gh_output(status=res.status)
        _print({k: v for k, v in res.summary.items() if k in ("status", "selected_count", "report", "reason", "validation",
                                                              "claude_action_failure_category", "report_commit",
                                                              "data_commit")})
        return res.exit_code
    raise ValueError(a)


# ------------------------------------------------------------------------------------------ parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="research-agent", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", help="project root (default: cwd or $RESEARCH_AGENT_ROOT)")
    p.add_argument("--db", help="override database path")
    p.add_argument("--run-id", help="run identifier (default: $RESEARCH_AGENT_RUN_ID or local-<date>)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create/migrate storage and validate configuration")
    d = sub.add_parser("doctor", help="check configuration, secrets presence, database, connectivity")
    d.add_argument("--offline", action="store_true")

    x = sub.add_parser("research-landscape", help="initial literature + trend baseline")
    x.add_argument("--lookback-days", type=int, default=180)
    x.add_argument("--date", type=_date, default=local_today())
    x.add_argument("--time-budget", type=float, default=None, help="seconds; resume next run if exceeded")
    x.add_argument("--force", action="store_true", help="recompute even if a baseline exists")

    f = sub.add_parser("fetch", help="retrieve and normalize candidates (checkpointed)")
    f.add_argument("--date", type=_date, default=local_today())
    f.add_argument("--only", nargs="*", help="limit to providers")
    f.add_argument("--time-budget", type=float, default=None)

    sc = sub.add_parser("score", help="lease jobs into a Claude manifest (and optionally import outputs)")
    sc.add_argument("--date", type=_date, default=local_today())
    sc.add_argument("--max-eval", type=int, default=None)
    sc.add_argument("--provider", choices=["manifest", "import", "fixture", "deepseek"], default="manifest",
                    help="manifest: write files for claude-code-action; import: validate existing outputs; "
                         "fixture: SIMULATED offline scores (testing only); deepseek: call the DeepSeek API "
                         "(requires provider: deepseek in llm.yaml; pay-per-use)")

    t = sub.add_parser("trends", help="trend statistics")
    t.add_argument("action", choices=["update"])
    t.add_argument("--date", type=_date, default=local_today())
    t.add_argument("--weekly", action="store_true")

    dg = sub.add_parser("digest", help="generate or validate a digest")
    dg.add_argument("action", choices=["generate", "validate"])
    dg.add_argument("--date", type=_date, default=local_today())
    dg.add_argument("--offline", action="store_true", help="skip online identity/URL checks")
    dg.add_argument("--publish", action="store_true", help="generate: promote to reports/ if validation passes")
    dg.add_argument("--force", action="store_true")
    dg.add_argument("--path", help="validate: report path")

    r = sub.add_parser("run", help="complete local pipeline")
    r.add_argument("--date", type=_date, default=local_today())
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--skip-llm", action="store_true")
    r.add_argument("--offline", action="store_true")
    r.add_argument("--no-fetch", action="store_true")
    r.add_argument("--force", action="store_true")
    r.add_argument("--max-eval", type=int, default=None)
    r.add_argument("--provider", choices=["none", "fixture"], default="none")

    b = sub.add_parser("backfill", help="generate digests for past dates (no look-ahead in trends/selection)")
    b.add_argument("--start", type=_date, required=True)
    b.add_argument("--end", type=_date, required=True)
    b.add_argument("--offline", action="store_true")
    b.add_argument("--dry-run", action="store_true")

    qu = sub.add_parser("queue", help="analysis queue")
    qu.add_argument("action", choices=["status", "retry"])
    qu.add_argument("--eligible", action="store_true", help="retry: retryable jobs only (default)")
    qu.add_argument("--all", action="store_true", help="retry: also permanent failures (explicit operator decision)")

    tg = sub.add_parser("tags", help="manage trend tags (edits config/trends.yaml)")
    tg.add_argument("action", choices=["list", "add", "rename", "merge", "archive", "reactivate", "watch", "pin", "unpin",
                                       "override", "proposals", "accept", "reject"])
    tg.add_argument("--tag")
    tg.add_argument("--name")
    tg.add_argument("--description")
    tg.add_argument("--match", nargs="*", default=[], help='conjunctions: "phrase a + phrase b" "phrase c"')
    tg.add_argument("--status", default="active", choices=["active", "watchlist"])
    tg.add_argument("--into")
    tg.add_argument("--value", type=float, default=-1, help="override: 0..1, negative clears")
    tg.add_argument("--proposal", type=int)
    tg.add_argument("--tag-id")

    fb = sub.add_parser("feedback", help="human feedback")
    fb.add_argument("action", choices=["add", "import", "suggest"])
    fb.add_argument("--canonical-id")
    fb.add_argument("--label")
    fb.add_argument("--note")
    fb.add_argument("--date", dest="date_str")

    ev = sub.add_parser("evaluate", help="offline recommendation-quality metrics")
    ev.add_argument("--gold")

    rp = sub.add_parser("replay", help="historical replay on a DB copy")
    rp.add_argument("--date", type=_date, required=True)
    rp.add_argument("--retrospective", action="store_true")

    h = sub.add_parser("history", help="recommendation history")
    h.add_argument("--paper")
    h.add_argument("--days", type=int, default=30)

    st = sub.add_parser("state", help="data-branch snapshot")
    st.add_argument("action", choices=["restore", "persist"])
    st.add_argument("--message")

    ci = sub.add_parser("ci", help="GitHub Actions stages")
    ci.add_argument("action", choices=["ingest", "prepare", "publish", "validate", "backfill", "classify", "claude-args",
                                       "provider", "analyze"])
    ci.add_argument("--snapshot", help="ingest: also export the snapshot here; prepare: restore from it")
    ci.add_argument("--reconcile", action="store_true", help="ingest: re-fetch the reconciliation window")
    ci.add_argument("--analysis-result", help="publish: result of the analysis job (success/failure/cancelled/skipped)")
    ci.add_argument("--start", type=_date)
    ci.add_argument("--end", type=_date)
    ci.add_argument("--date", type=_date, default=local_today())
    ci.add_argument("--stage", choices=["scoring", "narrative", "trend_discovery"])
    ci.add_argument("--outcome", default="missing")
    ci.add_argument("--execution-file")
    ci.add_argument("--skip-llm", action="store_true")
    ci.add_argument("--max-eval", type=int, default=None)
    ci.add_argument("--time-budget", type=float, default=None)
    ci.add_argument("--offline", action="store_true")
    ci.add_argument("--dry-run", action="store_true")
    ci.add_argument("--force", action="store_true")
    ci.add_argument("--no-commit", action="store_true")
    return p


DISPATCH = {"init": cmd_init, "doctor": cmd_doctor, "research-landscape": cmd_landscape, "fetch": cmd_fetch,
            "score": cmd_score, "trends": cmd_trends, "run": cmd_run, "backfill": cmd_backfill, "queue": cmd_queue,
            "tags": cmd_tags, "feedback": cmd_feedback, "evaluate": cmd_evaluate, "replay": cmd_replay,
            "history": cmd_history, "state": cmd_state, "ci": cmd_ci}


def main(argv: list[str] | None = None) -> int:
    from research_agent.logging_setup import setup_logging

    args = build_parser().parse_args(argv)
    try:
        s = load_settings(args.root)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    setup_logging(s.root / s.runtime.log_dir, args.verbose)
    if getattr(args, "provider", None) == "fixture":
        from research_agent.llm.fixture_provider import as_fixture

        s = as_fixture(s)
        log.warning("FIXTURE MODE: assessments are simulated (model id 'fixture-simulated'), not Claude output")
    if args.cmd == "digest":
        return cmd_digest_generate(s, args) if args.action == "generate" else cmd_digest_validate(s, args)
    if args.cmd == "ci" and args.action in ("claude-args", "classify", "analyze") and not args.stage:
        print("--stage is required", file=sys.stderr)
        return 2
    return DISPATCH[args.cmd](s, args)


if __name__ == "__main__":
    sys.exit(main())
