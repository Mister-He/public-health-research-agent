"""Markdown helpers and the deterministic fallback overview."""

from __future__ import annotations

import re

UNAVAILABLE = "Not available from verified sources"
ABSTRACT_UNAVAILABLE = "Abstract unavailable from verified sources."


def cell(text: str | None) -> str:
    """Safe single-line table cell."""
    if not text:
        return UNAVAILABLE
    return re.sub(r"\s+", " ", str(text)).replace("|", "\\|").strip()


_BLOCK_MARKER = re.compile(r"^(\s*)([#>+\-*=]|\d+[.)])")


def blockquote(text: str) -> str:
    """Quote verbatim text; leading block markers are backslash-escaped so an abstract that
    starts with e.g. '1. Background' stays a paragraph (the validator un-escapes)."""
    lines = []
    for line in text.strip().splitlines():
        line = _BLOCK_MARKER.sub(lambda m: m[1] + (m[2][:-1] + "\\" + m[2][-1] if m[2][0].isdigit()
                                                   else "\\" + m[2]), line)
        lines.append("> " + line if line.strip() else ">")
    return "\n".join(lines)


def fmt(x: float | None, digits: int = 2) -> str:
    return "n/a" if x is None else f"{x:.{digits}f}"


def fallback_overview(papers: list[dict], snapshot_tags: dict[str, dict], tag_names: dict[str, str]) -> str:
    """A STRUCTURAL overview built only from fields of the selected papers (no interpretation),
    used when Claude's narrative is unavailable or fails validation. It is labelled as such."""
    n = len(papers)
    domains: dict[str, int] = {}
    novelty: dict[str, int] = {}
    tags: dict[str, int] = {}
    for p in papers:
        for d in p.get("research_domain_names", []):
            domains[d] = domains.get(d, 0) + 1
        novelty[p.get("novelty_label", "unclassified")] = novelty.get(p.get("novelty_label", "unclassified"), 0) + 1
        for t in p.get("trend_tag_ids", []):
            tags[t] = tags.get(t, 0) + 1

    def top(d: dict[str, int], k: int = 4) -> str:
        items = sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))[:k]
        return ", ".join(f"{name} ({c})" for name, c in items) or "none"

    preprints = sum(1 for p in papers if p.get("is_preprint"))
    tag_list = ", ".join(tag_names.get(t, t) for t, _ in sorted(tags.items(), key=lambda kv: (-kv[1], kv[0]))[:4]) or "none"
    return (
        "This overview was generated automatically from structured fields because the AI narrative synthesis was "
        "unavailable for this run, so it describes the composition of today's selection rather than interpreting it. "
        f"Today's selection contains {n} paper{'s' if n != 1 else ''}, of which {preprints} "
        f"{'is a preprint' if preprints == 1 else 'are preprints'} that have not completed peer review. "
        f"By research domain, as classified by the AI assessment, the most represented areas are {top(domains)}. "
        f"The preliminary methodological classifications, which are based on abstracts only, are distributed as "
        f"follows: {top(novelty)}. The emerging trend tags matched most often within this selection are {tag_list}; "
        "these tags come from the configured trend taxonomy, and their trend statistics are reported in the "
        "methodology section below together with sample sizes and windows. Because a single day's selection is a "
        "small and non-random sample, none of these counts should be read as evidence of field-wide change. Each "
        "recommendation below includes the verified source metadata, the original abstract where it is available, "
        "and the full dimension-level score breakdown, so the selection can be audited paper by paper."
    )
