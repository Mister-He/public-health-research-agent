"""Structural parsing and integrity checks of a rendered digest (markdown-it token stream)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml
from markdown_it import MarkdownIt

from research_agent.validation.results import Check

FRONT_MATTER_KEYS = {"date", "timezone", "generated_at_utc", "paper_count", "pipeline_version", "scoring_version",
                     "trend_snapshot_version", "validation_status"}
FIELD_ROWS = ["Canonical ID", "Rank", "Authors", "Journal / server", "Publication date", "DOI", "PMID", "URL",
              "Peer review / preprint status", "Journal impact", "Overall score"]
PLACEHOLDERS = re.compile(r"\{\{|\}\}|\{%|%\}|\bTODO\b|\bTBD\b|<placeholder>", re.I)
PY_LEAKS = re.compile(r"\bNone\b|\bnan\b|\bundefined\b|\bNaN\b")      # case-sensitive value leaks
FILENAME = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")


@dataclass
class ParsedPaper:
    position: int | None
    heading: str
    fields: dict[str, str] = field(default_factory=dict)
    abstract: str = ""
    abstract_label: str = ""
    score_rows: list[list[str]] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)       # bold-label paragraphs ("Author keywords" -> text)


@dataclass
class ParsedReport:
    front_matter: dict | None
    front_matter_error: str | None
    title: str = ""
    overview: str = ""
    papers: list[ParsedPaper] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)
    body: str = ""
    overview_has_list: bool = False


def _md() -> MarkdownIt:
    return MarkdownIt("commonmark").enable("table")


def parse(text: str) -> ParsedReport:
    fm, err, body = None, None, text
    if text.startswith("---\n"):
        head, sep, rest = text[4:].partition("\n---\n")
        if not sep:
            err = "front matter not terminated"
        else:
            try:
                fm = yaml.safe_load(head)
                if not isinstance(fm, dict):
                    err, fm = "front matter is not a mapping", None
            except yaml.YAMLError as exc:
                err = f"invalid YAML front matter: {exc}"
            body = rest
    else:
        err = "missing YAML front matter"
    rep = ParsedReport(fm, err, body=body)
    tokens = _md().parse(body)
    i = 0
    section = None
    paper: ParsedPaper | None = None
    pending_label: str | None = None
    while i < len(tokens):
        t = tokens[i]
        if t.type == "heading_open":
            content = tokens[i + 1].content.strip()
            if t.tag == "h1":
                rep.title = content
            elif t.tag == "h2":
                section = content
                rep.sections.append(content)
                paper = None
            elif t.tag == "h3" and section == "Recommendations":
                m = re.match(r"^(\d+)\.\s+(.*)$", content)
                paper = ParsedPaper(int(m[1]) if m else None, m[2] if m else content)
                rep.papers.append(paper)
            i += 3
            continue
        if t.type in ("bullet_list_open", "ordered_list_open") and section == "Daily overview":
            rep.overview_has_list = True
        if t.type == "table_open":
            rows, i = _table(tokens, i)
            if paper is not None:
                if rows and rows[0][:2] == ["Field", "Value"]:
                    paper.fields = {r[0]: r[1] for r in rows[1:] if len(r) >= 2}
                elif rows and rows[0] and rows[0][0] == "Dimension":
                    paper.score_rows = rows[1:]
            continue
        if t.type == "blockquote_open" and paper is not None and pending_label == "Abstract":
            j = i + 1
            parts = []
            while tokens[j].type != "blockquote_close":
                if tokens[j].type == "inline":
                    parts.append(tokens[j].content)
                j += 1
            paper.abstract = " ".join(parts)
            pending_label = None
            i = j + 1
            continue
        if t.type == "inline":
            if section == "Daily overview" and paper is None and tokens[i - 1].type == "paragraph_open":
                if not (t.content.startswith("*") and t.content.endswith("*")):
                    rep.overview += (" " if rep.overview else "") + t.content
            if paper is not None:
                m = re.match(r"^\*\*([^*]+)\*\*(.*)$", t.content, flags=re.S)
                if m:
                    label = m[1].strip().rstrip(":").strip()
                    rest = m[2].strip()
                    paper.labels[label] = rest
                    if label == "Abstract":
                        paper.abstract_label = rest
                        pending_label = "Abstract"
                    else:
                        pending_label = None
                elif pending_label == "Abstract" and tokens[i - 1].type == "paragraph_open":
                    paper.abstract = t.content    # e.g. "Abstract unavailable from verified sources."
                    pending_label = None
        i += 1
    return rep


def _table(tokens, i) -> tuple[list[list[str]], int]:
    rows, row = [], None
    while tokens[i].type != "table_close":
        tt = tokens[i].type
        if tt == "tr_open":
            row = []
        elif tt == "inline" and row is not None:
            row.append(tokens[i].content.strip())
        elif tt == "tr_close":
            rows.append(row)
            row = None
        i += 1
    return rows, i + 1


def check_structure(path: Path, text: str, report_date: date, reports_dir: Path, *, expected_count: int,
                    enabled_dimensions: list[str], dimension_names: dict[str, str]) -> tuple[ParsedReport, list[Check]]:
    checks: list[Check] = []
    ok = lambda name, cond, detail="", target="report": checks.append(  # noqa: E731
        Check(name, target, "pass" if cond else "fail", detail if not cond else ""))
    ok("filename_format", bool(FILENAME.match(path.name)), f"bad filename {path.name}")
    ok("filename_date", path.name == f"{report_date.isoformat()}.md", f"{path.name} != {report_date}.md")
    ok("directory", path.parent.resolve() == reports_dir.resolve(), f"{path.parent} is not {reports_dir}")
    rep = parse(text)
    ok("front_matter_valid", rep.front_matter is not None, rep.front_matter_error or "")
    fm = rep.front_matter or {}
    missing = FRONT_MATTER_KEYS - set(fm)
    ok("front_matter_keys", not missing, f"missing keys {sorted(missing)}")
    ok("front_matter_date", str(fm.get("date")) == report_date.isoformat(), f"date {fm.get('date')} != {report_date}")
    ok("front_matter_timezone", fm.get("timezone") == "Asia/Shanghai", f"timezone {fm.get('timezone')}")
    ok("front_matter_paper_count", fm.get("paper_count") == len(rep.papers) == expected_count,
       f"front matter {fm.get('paper_count')}, sections {len(rep.papers)}, expected {expected_count}")
    ok("title_heading", rep.title == f"Today Digest - {report_date.isoformat()}", rep.title)
    ok("sections_present", rep.sections[:2] == ["Daily overview", "Recommendations"], f"sections {rep.sections}")
    words = len(rep.overview.split())
    ok("overview_nonempty", words >= 50, f"overview has {words} words")
    ok("overview_is_prose", not rep.overview_has_list, "overview contains a list")
    # Verbatim abstracts (blockquotes) may legitimately contain any word; scan everything else.
    scan = "\n".join(ln for ln in rep.body.splitlines() if not ln.startswith(">"))
    placeholders = sorted({m.group(0) for m in PLACEHOLDERS.finditer(scan)} | {m.group(0) for m in PY_LEAKS.finditer(scan)})
    ok("no_placeholders", not placeholders, f"placeholders found: {placeholders}")
    positions = [p.position for p in rep.papers]
    ok("positions_sequential", positions == list(range(1, len(rep.papers) + 1)), f"positions {positions}")
    ids = [p.fields.get("Canonical ID", "").strip("`") for p in rep.papers]
    ok("unique_paper_ids", len(ids) == len(set(ids)) and all(ids), f"ids {ids}")
    titles = [p.heading for p in rep.papers]
    ok("no_duplicate_sections", len(titles) == len(set(titles)), "duplicate paper headings")
    for p in rep.papers:
        tgt = p.fields.get("Canonical ID", p.heading).strip("`")[:120]
        miss = [f for f in FIELD_ROWS if not p.fields.get(f)]
        ok("required_fields", not miss, f"missing fields {miss}", tgt)
        ok("abstract_present_or_marked", bool(p.abstract), "abstract section empty", tgt)
        for label in ("Author keywords", "MeSH terms", "Matched emerging trend tags", "Methodological assessment",
                      "Why it is recommended for you", "Evidence and confidence limitations"):
            ok(f"label:{label}", bool(p.labels.get(label)), f"missing '{label}'", tgt)
        ai_label = next((k for k in p.labels if k.startswith("AI-assigned topic tags")), None)
        ok("ai_tags_labelled", ai_label is not None, "AI tags not labelled as AI-assigned", tgt)
        names = [r[0] for r in p.score_rows if r and not r[0].startswith("**")]
        expected = [dimension_names[d] for d in enabled_dimensions]
        ok("score_breakdown_complete", names == expected, f"dimensions {names} != {expected}", tgt)
        url = p.fields.get("URL", "")
        ok("url_format", bool(re.fullmatch(r"<https://[^\s<>]+>", url)), f"bad URL cell {url!r}", tgt)
        doi = p.fields.get("DOI", "")
        ok("doi_format", doi.startswith("Not available") or bool(re.fullmatch(r"\[10\.\d{4,9}/[^\]]+\]\(https://doi\.org/10\.[^)]+\)", doi)),
           f"bad DOI cell {doi!r}", tgt)
    return rep, checks
