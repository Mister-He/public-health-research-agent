"""JSON contracts for Claude Code Action outputs, plus grounding checks.

Pydantic validates shape and ranges; `check_grounding` then verifies the content against the
exact record that was sent: evidence quotes must occur in the supplied text, and numbers in
interpretive fields must appear in the record (the classic hallucination channel).
Malformed or ungrounded output is rejected — never coerced into a plausible score.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from research_agent.metadata.normalization import normalize_text_for_match

SCHEMA_VERSION = "1.0"
ClaimType = Literal["reported_finding", "author_claim", "interpretation", "external_context", "speculation",
                    "missing_information"]
NoveltyClass = Literal["new_method", "new_combination", "new_application", "incremental_application",
                       "not_methodological", "insufficient_information"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DimensionAssessment(Strict):
    score: float = Field(ge=0, le=100)
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(min_length=10, max_length=1500)
    evidence: list[str] = Field(default_factory=list, max_length=6)
    limitations: list[str] = Field(default_factory=list, max_length=6)


class Claim(Strict):
    type: ClaimType
    text: str = Field(min_length=3, max_length=600)


class TagAssignment(Strict):
    tag_id: str
    confidence: float = Field(ge=0, le=1)
    evidence: str = Field(min_length=3, max_length=400)


class PaperEvaluation(Strict):
    canonical_id: str
    metadata_version: str
    dimensions: dict[str, DimensionAssessment]
    profile_category: Literal["core", "exploratory", "none"]
    research_domains: list[str] = Field(default_factory=list, max_length=8)
    topic_tags: list[TagAssignment] = Field(default_factory=list, max_length=8)
    untagged_themes: list[str] = Field(default_factory=list, max_length=2)
    methodological_contributions: list[str] = Field(default_factory=list, max_length=5)
    novelty_class: NoveltyClass
    novelty_basis: Literal["abstract_only", "title_only", "full_metadata", "none"]
    methodology_assessment: str = Field(min_length=10, max_length=1200)
    why_useful: str = Field(min_length=10, max_length=1200)
    claims: list[Claim] = Field(default_factory=list, max_length=12)

    @property
    def is_new_methodology(self) -> bool:
        return self.novelty_class in ("new_method", "new_combination")


class EvaluationBatch(Strict):
    schema_version: Literal["1.0"]
    batch_id: str
    evaluations: list[PaperEvaluation]


class Narrative(Strict):
    schema_version: Literal["1.0"]
    report_date: str
    selection_hash: str
    narrative: str = Field(min_length=200, max_length=4000)

    @field_validator("narrative")
    @classmethod
    def _prose(cls, v: str) -> str:
        if re.search(r"^\s*([-*+]|\d+[.)])\s", v, flags=re.MULTILINE):
            raise ValueError("narrative must be prose, not a list")
        if re.search(r"^\s*#", v, flags=re.MULTILINE):
            raise ValueError("narrative must not contain headings")
        return v


class TagProposal(Strict):
    name: str = Field(min_length=3, max_length=80)
    description: str = Field(min_length=10, max_length=600)
    match: list[list[str]] = Field(min_length=1, max_length=8)
    supporting_paper_ids: list[str] = Field(min_length=1, max_length=30)
    evidence: list[str] = Field(min_length=1, max_length=8)
    sample_note: str = Field(min_length=5, max_length=400)
    overlaps_with: list[str] = Field(default_factory=list)


class TagProposals(Strict):
    schema_version: Literal["1.0"]
    as_of_date: str
    proposals: list[TagProposal] = Field(default_factory=list, max_length=10)


# ------------------------------------------------------------------------- grounding

_NUM = re.compile(r"(?<![\w.])(\d+(?:[.,]\d+)?)(%?)(?![\w])")


def _numbers(text: str) -> set[str]:
    out = set()
    for m in _NUM.finditer(text or ""):
        n = m.group(1).replace(",", ".")
        if len(n) >= 2 or "." in n or m.group(2):
            out.add(n.rstrip("0").rstrip(".") if "." in n else n)
    return out


def source_text(record: dict) -> str:
    parts = [record.get("title") or "", record.get("abstract") or "",
             " ".join(record.get("author_keywords") or []), " ".join(record.get("mesh_terms") or []),
             record.get("journal") or "", record.get("first_publication_date") or "",
             record.get("publication_type") or ""]
    return " ".join(parts)


def quote_grounded(quote: str, normalized_source: str) -> bool:
    parts = [p for p in re.split(r"\.\.\.|…", quote) if normalize_text_for_match(p).strip()]
    if not parts:
        return False
    return all(normalize_text_for_match(p).strip() in normalized_source for p in parts)


class GroundingReport(BaseModel):
    ok: bool
    errors: list[str] = []
    warnings: list[str] = []
    dropped_evidence: int = 0


def check_grounding(ev: PaperEvaluation, record: dict, *, llm_dimensions: list[str],
                    taxonomy_ids: set[str]) -> tuple[PaperEvaluation, GroundingReport]:
    """Return a cleaned evaluation and a report. ok=False means the evaluation must be rejected."""
    rep = GroundingReport(ok=True)
    src = source_text(record)
    nsrc = normalize_text_for_match(src)
    src_numbers = _numbers(src)

    missing = [d for d in llm_dimensions if d not in ev.dimensions]
    if missing:
        rep.errors.append(f"missing dimensions: {missing}")
    extra = [d for d in ev.dimensions if d not in llm_dimensions]
    if extra:
        rep.warnings.append(f"ignored unknown dimensions: {extra}")
        ev.dimensions = {k: v for k, v in ev.dimensions.items() if k in llm_dimensions}

    has_abstract = bool(record.get("abstract"))
    for dim_id, d in ev.dimensions.items():
        kept = [q for q in d.evidence if quote_grounded(q, nsrc)]
        dropped = len(d.evidence) - len(kept)
        if dropped:
            rep.dropped_evidence += dropped
            rep.warnings.append(f"{dim_id}: dropped {dropped} evidence quote(s) not found in the record")
        d.evidence = kept
        if not kept and d.confidence > 0.4:
            if dropped:     # every quote was fabricated: reject
                rep.errors.append(f"{dim_id}: no grounded evidence but confidence {d.confidence}")
            else:           # no quote offered (common for interpretive dimensions): cap, don't reject
                rep.warnings.append(f"{dim_id}: confidence {d.confidence} capped to 0.4 (no evidence quoted)")
                d.confidence = 0.4
        if not has_abstract and d.confidence > 0.5:
            rep.errors.append(f"{dim_id}: confidence {d.confidence} without an abstract")

    interpretive = " ".join([ev.methodology_assessment, ev.why_useful, *ev.methodological_contributions,
                             *(d.rationale for d in ev.dimensions.values()), *(c.text for c in ev.claims)])
    unsupported = sorted(_numbers(interpretive) - src_numbers)
    if unsupported:
        rep.errors.append(f"numbers not present in the record: {unsupported[:8]}")

    unknown_tags = [t.tag_id for t in ev.topic_tags if t.tag_id not in taxonomy_ids]
    if unknown_tags:
        rep.warnings.append(f"dropped unknown tag ids: {unknown_tags}")
    ev.topic_tags = [t for t in ev.topic_tags if t.tag_id in taxonomy_ids and quote_grounded(t.evidence, nsrc)]

    if not has_abstract and ev.novelty_basis == "abstract_only":
        ev.novelty_basis = "title_only"
    rep.ok = not rep.errors
    return ev, rep


def narrative_number_check(text: str, manifest_text: str) -> list[str]:
    return sorted(_numbers(text) - _numbers(manifest_text))


def json_schema(model: type[BaseModel]) -> dict:
    return model.model_json_schema()
