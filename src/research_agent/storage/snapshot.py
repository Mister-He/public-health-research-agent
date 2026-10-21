"""Git-friendly serialization of the SQLite state.

A consistent copy is taken with `VACUUM INTO`, then every table is written as deterministic,
gzip-compressed NDJSON files partitioned by rowid range:

    state/schema.json
    state/tables/<table>/<bucket>.ndjson.gz      (bucket = rowid // ROWS_PER_BUCKET)

Byte-identical output for unchanged partitions means Git stores them only once, so the data
branch grows by roughly the size of each day's new/changed rows instead of a full copy, and no
file approaches GitHub's 100 MB limit.
"""

from __future__ import annotations

import gzip
import io
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

from research_agent.storage.database import Database

ROWS_PER_BUCKET = 500
FORMAT = "research-agent-ndjson-v1"


def _tables(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def _gz(text: str) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0, filename="", compresslevel=6) as gz:
        gz.write(text.encode("utf-8"))
    return buf.getvalue()


def export_dir(db: Database, out: Path) -> Path:
    """Write the partitioned snapshot to `out` (replacing previous contents)."""
    with tempfile.TemporaryDirectory() as td:
        copy = Path(td) / "copy.sqlite3"
        db.conn.execute("VACUUM INTO ?", (str(copy),))
        conn = sqlite3.connect(str(copy))
        try:
            if out.exists():
                shutil.rmtree(out)
            (out / "tables").mkdir(parents=True)
            schema = {"format": FORMAT, "rows_per_bucket": ROWS_PER_BUCKET,
                      "schema_version": conn.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0],
                      "tables": {}}
            for t in _tables(conn):
                cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
                schema["tables"][t] = cols
                buckets: dict[int, list[str]] = {}
                for row in conn.execute(f"SELECT rowid, * FROM {t} ORDER BY rowid"):
                    buckets.setdefault(row[0] // ROWS_PER_BUCKET, []).append(
                        json.dumps(list(row), ensure_ascii=False, separators=(",", ":")))
                tdir = out / "tables" / t
                tdir.mkdir()
                for b, lines in buckets.items():
                    (tdir / f"{b:06d}.ndjson.gz").write_bytes(_gz("\n".join(lines) + "\n"))
            (out / "schema.json").write_text(json.dumps(schema, indent=1, sort_keys=True))
        finally:
            conn.close()
    return out


def import_dir(src: Path, db_path: Path) -> None:
    """Rebuild a SQLite database from a partitioned snapshot, then apply newer migrations."""
    schema = json.loads((src / "schema.json").read_text())
    if schema.get("format") != FORMAT:
        raise ValueError(f"unknown snapshot format {schema.get('format')!r}")
    tmp = db_path.with_suffix(".restoring")
    tmp.unlink(missing_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = Database(tmp)
    db.migrate(up_to=schema["schema_version"])
    db.conn.execute("PRAGMA foreign_keys = OFF")
    with db.transaction():
        db.execute("DELETE FROM schema_migrations")
        for t, cols in schema["tables"].items():
            tdir = src / "tables" / t
            ph = ",".join("?" * (len(cols) + 1))
            sql = f"INSERT INTO {t} (rowid, {', '.join(cols)}) VALUES ({ph})"
            for f in sorted(tdir.glob("*.ndjson.gz")):
                with gzip.open(f, "rt", encoding="utf-8") as fh:
                    db.conn.executemany(sql, (json.loads(line) for line in fh if line.strip()))
    db.conn.execute("PRAGMA foreign_keys = ON")
    bad = db.conn.execute("PRAGMA foreign_key_check").fetchall()
    ok = db.scalar("PRAGMA integrity_check")
    db.migrate()
    db.close()
    if ok != "ok" or bad:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"restored snapshot failed checks: integrity={ok}, foreign keys={bad[:5]}")
    tmp.replace(db_path)
