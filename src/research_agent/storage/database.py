"""SQLite access, migrations and consistent snapshots."""

from __future__ import annotations

import gzip
import json
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from research_agent.timeutil import iso_utc

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = DELETE")  # no -wal side files to forget in snapshots
        self._depth = 0

    # ------------------------------------------------------------ transactions
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Nested-safe transaction (savepoints inside an outer BEGIN IMMEDIATE)."""
        if self._depth == 0:
            self.conn.execute("BEGIN IMMEDIATE")
        else:
            self.conn.execute(f"SAVEPOINT sp{self._depth}")
        self._depth += 1
        try:
            yield self.conn
        except BaseException:
            self._depth -= 1
            if self._depth == 0:
                self.conn.execute("ROLLBACK")
            else:
                self.conn.execute(f"ROLLBACK TO sp{self._depth}")
                self.conn.execute(f"RELEASE sp{self._depth}")
            raise
        else:
            self._depth -= 1
            if self._depth == 0:
                self.conn.execute("COMMIT")
            else:
                self.conn.execute(f"RELEASE sp{self._depth}")

    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def query(self, sql: str, params: Any = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: Any = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Any = ()) -> Any:
        row = self.conn.execute(sql, params).fetchone()
        return row[0] if row else None

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------ migrations
    def migrate(self, up_to: int | None = None) -> list[str]:
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        applied = {r[0] for r in self.conn.execute("SELECT version FROM schema_migrations")}
        done = []
        for f in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql")):
            version = int(f.name[:4])
            if version in applied or (up_to is not None and version > up_to):
                continue
            with self.transaction():
                for stmt in _split_sql(f.read_text(encoding="utf-8")):
                    self.conn.execute(stmt)
                self.conn.execute(
                    "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?,?,?)",
                    (version, f.stem, iso_utc()),
                )
            done.append(f.stem)
        return done

    def schema_version(self) -> int:
        return int(self.scalar("SELECT COALESCE(MAX(version),0) FROM schema_migrations") or 0)

    # ------------------------------------------------------------ meta
    def get_meta(self, key: str) -> str | None:
        return self.scalar("SELECT value FROM state_meta WHERE key=?", (key,))

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO state_meta(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, iso_utc()),
        )

    # ------------------------------------------------------------ snapshots
    def snapshot_to(self, dest_gz: Path) -> Path:
        """Consistent snapshot via VACUUM INTO, then gzip (deterministic mtime)."""
        dest_gz.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest_gz.with_suffix(".tmp.sqlite3")
        tmp.unlink(missing_ok=True)
        self.conn.execute("VACUUM INTO ?", (str(tmp),))
        tmp_gz = dest_gz.with_suffix(".tmp.gz")
        with open(tmp, "rb") as src, open(tmp_gz, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as gz:
                shutil.copyfileobj(src, gz)
        tmp.unlink()
        tmp_gz.replace(dest_gz)
        return dest_gz


def restore_snapshot(src_gz: Path, dest_db: Path) -> None:
    """Restore a gzip snapshot to dest_db atomically and check integrity."""
    dest_db.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest_db.with_suffix(".restoring")
    with gzip.open(src_gz, "rb") as gz, open(tmp, "wb") as out:
        shutil.copyfileobj(gz, out)
    conn = sqlite3.connect(str(tmp))
    try:
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    if ok != "ok":
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"snapshot integrity check failed: {ok}")
    tmp.replace(dest_db)


def _split_sql(script: str) -> list[str]:
    """Split a migration script on ';' at line ends (no triggers/procedures in migrations)."""
    lines = [ln for ln in script.splitlines() if not ln.strip().startswith("--")]
    out, buf = [], []
    for ln in lines:
        buf.append(ln)
        if ln.rstrip().endswith(";"):
            stmt = "\n".join(buf).strip().rstrip(";")
            if stmt:
                out.append(stmt)
            buf = []
    if "\n".join(buf).strip():
        out.append("\n".join(buf).strip())
    return out


def dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)


def open_db(path: Path | str) -> Database:
    db = Database(path)
    db.migrate()
    return db
