"""Data-branch persistence and report commits against a real local bare remote; CLI smoke tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from research_agent.cli import main
from research_agent.git.publisher import DataBranch, StaleDataBranch, UnexpectedChanges, commit_report, git
from research_agent.storage.database import open_db


def sh(*args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repos(tmp_path):
    remote = tmp_path / "remote.git"
    sh("git", "init", "-q", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    work = tmp_path / "work"
    sh("git", "clone", "-q", str(remote), str(work), cwd=tmp_path)
    for k, v in (("user.name", "Test"), ("user.email", "t@example.org"), ("commit.gpgsign", "false")):
        sh("git", "config", k, v, cwd=work)
    (work / "README.md").write_text("x\n")
    sh("git", "add", "README.md", cwd=work)
    sh("git", "commit", "-q", "-m", "chore: init", cwd=work)
    sh("git", "push", "-q", "origin", "main", cwd=work)
    other = tmp_path / "other"
    sh("git", "clone", "-q", str(remote), str(other), cwd=tmp_path)
    for k, v in (("user.name", "Test"), ("user.email", "t@example.org")):
        sh("git", "config", k, v, cwd=other)
    return remote, work, other


def test_data_branch_roundtrip_and_stale_detection(repos, tmp_path):
    remote, work, other = repos
    a = DataBranch(work)
    assert a.restore(tmp_path / "a.sqlite3") is None                    # first run: no branch yet
    db = open_db(tmp_path / "a.sqlite3")
    db.set_meta("k", "v1")
    sha1 = a.persist(db, "chore(data): first")
    assert sh("git", "ls-remote", "--heads", str(remote), "research-data", cwd=work).startswith(sha1)
    # Unchanged state -> no new commit.
    assert a.persist(db, "chore(data): noop") == sha1
    # Two concurrent writers restore the same base; the second push is rejected (never forced).
    b = DataBranch(other)
    b.restore(tmp_path / "b.sqlite3")
    dbb = open_db(tmp_path / "b.sqlite3")
    assert dbb.get_meta("k") == "v1"
    db.set_meta("k", "v2")
    a.persist(db, "chore(data): second")
    dbb.set_meta("k", "conflicting")
    with pytest.raises(StaleDataBranch):
        b.persist(dbb, "chore(data): stale")
    # Reload + replay succeeds and keeps the newer history.
    b.restore(tmp_path / "b2.sqlite3")
    db2 = open_db(tmp_path / "b2.sqlite3")
    assert db2.get_meta("k") == "v2"
    db2.set_meta("k2", "x")
    b.persist(db2, "chore(data): replayed")
    log = sh("git", "--git-dir", str(remote), "log", "--format=%s", "research-data", cwd=tmp_path).split("\n")
    assert log[:3] == ["chore(data): replayed", "chore(data): second", "chore(data): first"]


def test_commit_report_stages_only_the_report(repos):
    _, work, _ = repos
    (work / "reports").mkdir()
    rep = work / "reports" / "2026-10-21.md"
    rep.write_text("# digest\n")
    (work / "stray.txt").write_text("not approved\n")                   # untracked, must not be committed
    sha = commit_report(work, rep, "2026-10-21")
    assert sha and sh("git", "log", "-1", "--format=%s", cwd=work) == \
        "docs(digest): add public health research digest for 2026-10-21"
    assert sh("git", "show", "--name-only", "--format=", "HEAD", cwd=work) == "reports/2026-10-21.md"
    assert commit_report(work, rep, "2026-10-21") is None              # unchanged -> no empty daily commit
    rep.write_text("# digest v2\n")
    commit_report(work, rep, "2026-10-21")
    assert sh("git", "log", "-1", "--format=%s", cwd=work).startswith("docs(digest): update")
    (work / "stray.txt").write_text("staged by someone\n")
    git("add", "stray.txt", cwd=work)
    with pytest.raises(UnexpectedChanges):
        commit_report(work, rep, "2026-10-21")


def test_commit_report_rebases_on_concurrent_push(repos):
    _, work, other = repos
    (other / "CODE.md").write_text("new code\n")
    sh("git", "add", "CODE.md", cwd=other)
    sh("git", "commit", "-q", "-m", "feat: code change", cwd=other)
    sh("git", "push", "-q", "origin", "main", cwd=other)
    (work / "reports").mkdir()
    rep = work / "reports" / "2026-10-21.md"
    rep.write_text("# digest\n")
    commit_report(work, rep, "2026-10-21")
    assert sh("git", "log", "--format=%s", "-2", "origin/main", cwd=work).split("\n")[0].startswith("docs(digest)")


def test_cli_init_queue_doctor_offline(project, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("RESEARCH_AGENT_CONTACT_EMAIL", "t@example.org")
    assert main(["--root", str(project), "init"]) == 0
    assert main(["--root", str(project), "queue", "status"]) == 0
    assert main(["--root", str(project), "tags", "list"]) == 0
    assert main(["--root", str(project), "doctor", "--offline"]) == 0
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-be-set")
    assert main(["--root", str(project), "doctor", "--offline"]) == 1
    out = capsys.readouterr().out
    assert "sk-ant-should-not-be-set" not in out and "pay-as-you-go" in out


def test_cli_config_error_exit_code(project):
    (project / "config" / "scoring.yaml").write_text("scoring: [broken")
    assert main(["--root", str(project), "init"]) == 2


def test_cli_fixture_run_end_to_end(project, monkeypatch, capsys):
    """`run --provider fixture` is clearly marked as simulated and isolated by model id."""
    import sys
    sys.path.insert(0, str(Path(__file__).parents[1]))
    from fixtures.synthetic import REPORT_DATE, records

    from research_agent.metadata.deduplication import ingest_record
    from research_agent.retrieval.screening import prescreen
    from research_agent.settings import load_settings

    s = load_settings(project)
    db = open_db(s.db_path)
    prescreen(db, s, [ingest_record(db, r).paper_id for r in records()])
    db.close()
    rc = main(["--root", str(project), "run", "--date", REPORT_DATE.isoformat(), "--no-fetch", "--offline",
               "--provider", "fixture"])
    assert rc == 0, capsys.readouterr()
    text = (project / "reports" / f"{REPORT_DATE.isoformat()}.md").read_text()
    assert "model: fixture-simulated" in text
    # Independent re-validation of the published file from disk + persisted records.
    assert main(["--root", str(project), "digest", "validate", "--date", REPORT_DATE.isoformat(), "--offline"]) == 0
