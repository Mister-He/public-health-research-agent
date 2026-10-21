"""Git operations: verified report commits (Conventional Commits) and the SQLite data branch.

Data branch protocol (no force pushes, ever):
  restore: fetch <data-branch>, remember its head as BASE, gunzip the snapshot.
  persist: VACUUM INTO -> partitioned NDJSON (storage/snapshot.py), commit with parent BASE via a temporary index
           (the working tree is never touched), then `git push <sha>:refs/heads/<branch>`.
           If the remote moved, the push is rejected (non-fast-forward) -> StaleDataBranch;
           the caller reloads the newer state and replays its idempotent stage.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from research_agent.storage.database import Database
from research_agent.storage.snapshot import export_dir, import_dir

log = logging.getLogger(__name__)

DATA_README = """# research-data branch

Machine-managed state for the research agent: a consistent SQLite snapshot serialized as
partitioned, gzip-compressed NDJSON (`state/`), containing ingestion checkpoints, the analysis
queue, evaluation cache, trend history and recommendation history. Unchanged partitions are
byte-identical, so Git stores them once.

Do not edit by hand. Restore locally with `research-agent state restore`.
"""


class GitError(RuntimeError):
    pass


class StaleDataBranch(GitError):
    """The data branch moved since this run restored it."""


class UnexpectedChanges(GitError):
    pass


def git(*args: str, cwd: Path, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", *args], cwd=cwd, env={**os.environ, **(env or {})}, text=True,
                          capture_output=True)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args[:3])} failed: {proc.stderr.strip()[:500]}")
    return proc


@dataclass
class DataBranch:
    repo: Path
    branch: str = "research-data"
    remote: str = "origin"
    snapshot_dir: str = "state"
    base_sha: str | None = None

    def _remote_head(self) -> str | None:
        proc = git("ls-remote", "--heads", self.remote, self.branch, cwd=self.repo, check=False)
        if proc.returncode != 0:
            raise GitError(f"cannot reach remote {self.remote}: {proc.stderr.strip()[:300]}")
        line = proc.stdout.strip()
        return line.split()[0] if line else None

    def fetch_base(self) -> str | None:
        """Fetch the current data-branch head (the parent for the next persist)."""
        head = self._remote_head()
        self.base_sha = head
        if head is not None:
            git("fetch", "--no-tags", "--depth=1", self.remote,
                f"+refs/heads/{self.branch}:refs/remotes/{self.remote}/{self.branch}", cwd=self.repo)
        return head

    def restore(self, db_path: Path) -> str | None:
        """Restore the latest snapshot into db_path. Returns the base commit (None on first run)."""
        head = self.fetch_base()
        if head is None:
            log.info("data branch does not exist yet; starting from an empty database")
            return None
        with tempfile.TemporaryDirectory() as td:
            tar = Path(td) / "snap.tar"
            with open(tar, "wb") as fh:
                proc = subprocess.run(["git", "archive", "--format=tar", head, self.snapshot_dir], cwd=self.repo,
                                      stdout=fh, stderr=subprocess.PIPE)
            if proc.returncode != 0:
                raise GitError(f"snapshot missing on {self.branch}@{head[:8]}: {proc.stderr.decode()[:200]}")
            subprocess.run(["tar", "-xf", str(tar), "-C", td], check=True)
            import_dir(Path(td) / self.snapshot_dir, db_path)
        return head

    def persist(self, db: Database, message: str, *, push: bool = True) -> str | None:
        """Commit the current DB as a child of base_sha and push (fast-forward only)."""
        with tempfile.TemporaryDirectory() as td:
            work = Path(td) / "tree"
            export_dir(db, work / self.snapshot_dir)
            (work / "README.md").write_text(DATA_README)
            env = {"GIT_INDEX_FILE": str(Path(td) / "index"), "GIT_WORK_TREE": str(work)}
            git("read-tree", "--empty", cwd=self.repo, env=env)
            git("add", "-A", "--", ".", cwd=self.repo, env=env)
            tree = git("write-tree", cwd=self.repo, env=env).stdout.strip()
        if self.base_sha:
            base_tree = git("rev-parse", f"{self.base_sha}^{{tree}}", cwd=self.repo, check=False).stdout.strip()
            if base_tree == tree:
                log.info("data branch unchanged; nothing to persist")
                return self.base_sha
        args = ["commit-tree", tree, "-m", message]
        if self.base_sha:
            args[2:2] = ["-p", self.base_sha]
        sha = git(*args, cwd=self.repo, env=_bot_env(self.repo)).stdout.strip()
        if push:
            proc = git("push", "--porcelain", self.remote, f"{sha}:refs/heads/{self.branch}", cwd=self.repo, check=False)
            if proc.returncode != 0:
                text = proc.stderr + proc.stdout
                if "non-fast-forward" in text or "rejected" in text or "fetch first" in text:
                    raise StaleDataBranch(f"{self.branch} moved since restore (base {str(self.base_sha)[:8]})")
                raise GitError(f"push failed: {text.strip()[:400]}")
        self.base_sha = sha
        return sha


def _bot_env(repo: Path) -> dict:
    name = git("config", "user.name", cwd=repo, check=False).stdout.strip() or "research-agent[bot]"
    email = git("config", "user.email", cwd=repo, check=False).stdout.strip() or "research-agent@users.noreply.github.com"
    return {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}


def commit_report(repo: Path, report: Path, report_date: str, *, remote: str = "origin", branch: str = "main",
                  push: bool = True, retries: int = 3) -> str | None:
    """Stage exactly one report file, verify nothing else is staged, commit, push (rebase on race).
    Returns the commit sha, or None when the file is unchanged."""
    rel = report.resolve().relative_to(repo.resolve()).as_posix()
    existed = git("cat-file", "-e", f"HEAD:{rel}", cwd=repo, check=False).returncode == 0
    staged_before = git("diff", "--cached", "--name-only", cwd=repo).stdout.split()
    if staged_before:
        raise UnexpectedChanges(f"index already has staged changes: {staged_before[:10]}")
    git("add", "--", rel, cwd=repo)
    staged = git("diff", "--cached", "--name-only", cwd=repo).stdout.split()
    if not staged:
        return None
    if staged != [rel]:
        git("reset", "-q", "--", *staged, cwd=repo)
        raise UnexpectedChanges(f"unexpected staged files: {staged}")
    verb = "update" if existed else "add"
    msg = f"docs(digest): {verb} public health research digest for {report_date}"
    git("commit", "-q", "-m", msg, cwd=repo, env=_bot_env(repo))
    sha = git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    if not push:
        return sha
    for attempt in range(retries):
        proc = git("push", remote, f"HEAD:refs/heads/{branch}", cwd=repo, check=False)
        if proc.returncode == 0:
            return git("rev-parse", "HEAD", cwd=repo).stdout.strip()
        # Someone pushed code meanwhile: our commit only adds one report file, so a rebase is safe.
        git("pull", "--rebase", "-q", remote, branch, cwd=repo)
    raise GitError(f"could not push report after {retries} attempts")
