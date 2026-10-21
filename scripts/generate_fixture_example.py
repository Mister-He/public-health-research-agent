"""Regenerate docs/examples/fixture-digest-2026-10-21.md from SYNTHETIC fixture records.

Runs the real pipeline (dedup, prescreen, queue, manifest, import, trends, selection, render,
gate) with the offline fixture provider. Every score and narrative is simulated and labelled so.
Usage: uv run python scripts/generate_fixture_example.py
"""

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from fixtures.synthetic import REPORT_DATE, records  # noqa: E402

from research_agent.llm import claude_code_action as cca  # noqa: E402
from research_agent.llm.fixture_provider import as_fixture, simulate_narrative, simulate_scoring  # noqa: E402
from research_agent.metadata.deduplication import ingest_record  # noqa: E402
from research_agent.pipeline.runner import prepare_narrative, publish_stage  # noqa: E402
from research_agent.retrieval.screening import prescreen, shortlist_and_enqueue  # noqa: E402
from research_agent.settings import load_settings  # noqa: E402
from research_agent.storage.database import open_db  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        shutil.copytree(ROOT / "config", root / "config")
        shutil.copytree(ROOT / "prompts", root / "prompts")
        (root / "reports").mkdir()
        s = as_fixture(load_settings(root))
        db = open_db(s.db_path)
        prescreen(db, s, sorted({ingest_record(db, r, run_id="example").paper_id for r in records()}))
        shortlist_and_enqueue(db, s, REPORT_DATE)
        cca.build_scoring_manifest(db, s, run_id="example", report_date=REPORT_DATE)
        simulate_scoring(s)
        cca.write_status(s, cca.ActionStatus(stage="scoring", outcome="success"))
        shutil.copy(s.db_path, root / "copy.sqlite3")
        prepare_narrative(open_db(root / "copy.sqlite3"), s, REPORT_DATE, run_id="example")
        simulate_narrative(s)
        res = publish_stage(db, s, REPORT_DATE, run_id="example", online=False)
        if res.status != "published":
            print(res.summary, file=sys.stderr)
            return 1
        banner = ("<!-- EXAMPLE ONLY: generated from SYNTHETIC fixture records (DOI prefix 10.5555 is "
                  "Crossref's test prefix). Scores and the overview are SIMULATED by the offline fixture "
                  "provider, not produced by Claude. Regenerate with scripts/generate_fixture_example.py. -->\n")
        out = ROOT / "docs" / "examples" / f"fixture-digest-{REPORT_DATE.isoformat()}.md"
        out.write_text(banner + res.report_path.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"wrote {out} ({res.gate.summary()})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
