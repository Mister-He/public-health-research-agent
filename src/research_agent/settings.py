"""Typed, validated configuration loaded from config/*.yaml.

Every behavioural decision lives in YAML; this module only defines the schema,
defaults and cross-file invariants (e.g. weights sum to 1, no API-key fallback).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from research_agent import __version__

PIPELINE_VERSION = __version__


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConfigError(ValueError):
    """Raised for invalid configuration (exit code 2 in the CLI)."""


# ---------------------------------------------------------------- research profile


class Topic(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    name: str
    kind: Literal["core", "exploratory"]
    importance: float = Field(ge=0, le=1)
    description: str
    keywords: list[str] = Field(min_length=1)
    mesh_terms: list[str] = []
    exclude: list[str] = []
    application_domain: bool = False


class ResearchProfile(StrictModel):
    profile_version: str
    owner: str = ""
    topics: list[Topic]
    global_exclusions: list[str] = []

    @model_validator(mode="after")
    def _unique_ids(self) -> ResearchProfile:
        ids = [t.id for t in self.topics]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate topic ids in research_profile.yaml")
        return self

    def topic(self, topic_id: str) -> Topic:
        return next(t for t in self.topics if t.id == topic_id)


# ---------------------------------------------------------------- sources & journals


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    enabled: bool = True
    base_url: str
    min_interval_seconds: float = 0.5
    page_size: int = 100
    chunk_days: int = Field(default=14, ge=1, le=90)
    max_records_per_query_window: int = 5000


class HttpConfig(StrictModel):
    connect_timeout_seconds: float = 10
    read_timeout_seconds: float = 60
    max_retries: int = Field(default=5, ge=0, le=10)
    backoff_base_seconds: float = 1.5
    backoff_max_seconds: float = 60
    user_agent: str


class SourcesFile(StrictModel):
    sources: dict[str, SourceConfig]
    http: HttpConfig
    minimum_successful_providers: int = 3

    @field_validator("sources")
    @classmethod
    def _mandatory(cls, v: dict[str, SourceConfig]) -> dict[str, SourceConfig]:
        missing = {"pubmed", "europe_pmc", "openalex", "crossref", "medrxiv"} - set(v)
        if missing:
            raise ValueError(f"mandatory sources missing from sources.yaml: {sorted(missing)}")
        return v


_ISSN = r"^\d{4}-\d{3}[\dX]$"


class Journal(StrictModel):
    id: str
    name: str
    issns: list[str] = Field(min_length=1)
    priority: Literal["high", "normal"] = "normal"
    enabled: bool = True

    @field_validator("issns")
    @classmethod
    def _issn_format(cls, v: list[str]) -> list[str]:
        import re

        for issn in v:
            if not re.match(_ISSN, issn):
                raise ValueError(f"invalid ISSN {issn!r}")
        return v


class JournalsFile(StrictModel):
    journals: list[Journal]


class QueryGroup(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    terms: list[str] = Field(min_length=1)
    mesh: list[str] = []
    mesh_anchor: list[str] = []
    context: list[str] = []
    providers: list[Literal["pubmed", "europe_pmc", "openalex"]] | None = None   # None = file default


class SearchQueriesFile(StrictModel):
    query_version: str
    providers: list[Literal["pubmed", "europe_pmc", "openalex"]]
    groups: list[QueryGroup]


# ---------------------------------------------------------------- scoring


class Dimension(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    name: str
    description: str
    enabled: bool = True
    weight: float = Field(ge=0, le=1)
    method: Literal["llm", "deterministic", "hybrid"]
    rubric_version: str
    prompt: str | None = None
    evaluator: str | None = None
    params: dict[str, Any] = {}
    missing_data_policy: Literal["reject", "zero", "exclude_and_renormalize"] = "reject"
    score_range: tuple[float, float] = (0.0, 100.0)

    @model_validator(mode="after")
    def _method_requirements(self) -> Dimension:
        if self.method == "llm" and not self.prompt:
            raise ValueError(f"dimension {self.id}: llm dimensions need a prompt file")
        if self.method in ("deterministic", "hybrid") and not self.evaluator:
            raise ValueError(f"dimension {self.id}: {self.method} dimensions need a registered evaluator")
        return self


class ScoringConfig(StrictModel):
    scale: dict[str, float] = {"minimum": 0, "maximum": 100}
    dimensions: list[Dimension]
    tolerance: float = 1e-6

    @model_validator(mode="after")
    def _weights(self) -> ScoringConfig:
        enabled = [d for d in self.dimensions if d.enabled]
        if not enabled:
            raise ValueError("at least one scoring dimension must be enabled")
        total = sum(d.weight for d in enabled)
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"enabled scoring weights must sum to 1.0 (got {total:.6f})")
        ids = [d.id for d in self.dimensions]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate scoring dimension ids")
        return self

    @property
    def enabled(self) -> list[Dimension]:
        return [d for d in self.dimensions if d.enabled]

    @property
    def llm_dimensions(self) -> list[Dimension]:
        return [d for d in self.enabled if d.method == "llm"]

    def version(self) -> str:
        """Scoring version = hash of every enabled dimension's id/weight/method/rubric."""
        payload = [(d.id, d.weight, d.method, d.rubric_version, d.params) for d in self.enabled]
        return "scoring-" + _short_hash(payload)

    def rubric_version(self) -> str:
        """Version of what Claude is asked to score (weights excluded: Python applies them)."""
        payload = sorted((d.id, d.rubric_version) for d in self.llm_dimensions)
        return "rubric-" + _short_hash(payload)


# ---------------------------------------------------------------- trends


class TrendTag(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    name: str
    description: str = ""
    aliases: list[str] = []
    parent_tags: list[str] = []
    match: list[list[str]] = []
    status: Literal["active", "watchlist", "archived"] = "active"
    pinned: bool = False
    seed: bool = False
    manual_override: float | None = Field(default=None, ge=0, le=1)
    created_at: str | None = None


class TrendParams(StrictModel):
    recent_window_days: int = 30
    baseline_window_days: int = 180
    frequency_windows: list[int] = [7, 30, 90]
    weights: dict[str, float] = {"frequency": 0.5, "growth": 0.3, "external": 0.2}
    smoothing: dict[str, float] = {"pseudo_count": 1.0, "ema_alpha": 0.3, "growth_logistic_scale": 0.7}
    min_sample: int = 5
    corpus_min_prescreen: float = 0.15
    reassessment_weekday: Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"] = "monday"
    external: dict[str, Any] = {"enabled": True, "window_days": 90}
    proposals: dict[str, float] = {"max_new_per_week": 3, "min_cluster_size": 8, "min_unmatched_share": 0.1}

    @field_validator("weights")
    @classmethod
    def _w(cls, v: dict[str, float]) -> dict[str, float]:
        if set(v) != {"frequency", "growth", "external"} or abs(sum(v.values()) - 1) > 1e-9:
            raise ValueError("trend weights must be frequency/growth/external summing to 1")
        return v


class TrendsFile(StrictModel):
    taxonomy_version: int
    parameters: TrendParams
    tags: list[TrendTag]

    @model_validator(mode="after")
    def _ids(self) -> TrendsFile:
        ids = [t.id for t in self.tags]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate trend tag ids")
        known = set(ids)
        for t in self.tags:
            bad = set(t.parent_tags) - known
            if bad:
                raise ValueError(f"tag {t.id} has unknown parent_tags {sorted(bad)}")
        return self


# ---------------------------------------------------------------- ranking / llm / reporting / validation / runtime


class RankingConfig(StrictModel):
    max_recommendations: int = Field(default=10, ge=1, le=50)
    min_verified_papers: int = Field(default=3, ge=1)
    publication_horizon_days: int = 30
    core_relevance_threshold: float = 55
    exploratory_relevance_threshold: float = 60
    max_exploratory: int = 2
    min_metadata_completeness: float = 0.5
    diversity: dict[str, Any] = {"enabled": True, "lambda": 0.1}
    resurfacing: dict[str, bool] = {}


class LLMStage(StrictModel):
    max_turns: int = Field(ge=1, le=200)
    timeout_minutes: int = Field(ge=1, le=360)
    batch_size: int | None = None
    max_papers_per_run: int | None = None


class DeepSeekConfig(StrictModel):
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    api_key_env: str = "DEEPSEEK_API_KEY"
    max_output_tokens: int = Field(default=8000, ge=256, le=64000)
    request_timeout_seconds: float = 300
    max_tokens_per_run: int = Field(default=600_000, ge=1000)   # input+output budget per pipeline run
    max_retries: int = Field(default=3, ge=0, le=6)


class LLMConfig(StrictModel):
    provider: Literal["claude_code_action", "deepseek", "ollama", "fixture"]
    auth: Literal["oauth_subscription", "api_key"]
    allow_api_key_fallback: Literal[False] = False
    model: str
    prompt_version: str
    stages: dict[str, LLMStage]
    screening: dict[str, float]
    queue: dict[str, float]
    local: dict[str, Any] = {}
    deepseek: DeepSeekConfig = DeepSeekConfig()

    @model_validator(mode="after")
    def _provider_auth(self) -> LLMConfig:
        if self.provider == "claude_code_action" and self.auth != "oauth_subscription":
            raise ValueError("provider claude_code_action requires auth: oauth_subscription")
        if self.provider == "deepseek" and self.auth != "api_key":
            raise ValueError("provider deepseek requires auth: api_key (pay-per-use; set it deliberately)")
        return self

    @property
    def active_model(self) -> str:
        """Model id used in cache/job keys and reports for the selected provider."""
        return self.deepseek.model if self.provider == "deepseek" else self.model


class ReportingConfig(StrictModel):
    timezone: str = "Asia/Shanghai"
    output_dir: str = "reports"
    narrative_words: dict[str, int] = {"min": 150, "max": 250}
    abstract_policy: Literal["full", "link_only"] = "full"
    link_only_licenses: list[str] = []
    include_coverage_footer: bool = True


class ValidationConfig(StrictModel):
    online_identity_checks: bool = True
    url_checks: bool = True
    url_timeout_seconds: float = 20
    max_repair_rounds: int = Field(default=3, ge=0, le=10)
    title_match_ratio: float = 0.93


class RuntimeConfig(StrictModel):
    timezone: str = "Asia/Shanghai"
    database_path: str = "state/research.sqlite3"
    analysis_dir: str = "analysis"
    log_dir: str = "logs"
    windows: dict[str, int]
    max_chunks_per_query_per_run: int = 6
    data_branch: dict[str, Any]
    git: dict[str, str]


# ---------------------------------------------------------------- aggregate


class Settings(BaseModel):
    root: Path
    profile: ResearchProfile
    sources: SourcesFile
    journals: JournalsFile
    queries: SearchQueriesFile
    scoring: ScoringConfig
    trends: TrendsFile
    ranking: RankingConfig
    llm: LLMConfig
    reporting: ReportingConfig
    validation: ValidationConfig
    runtime: RuntimeConfig
    config_hash: str

    @property
    def db_path(self) -> Path:
        return self.root / self.runtime.database_path

    @property
    def analysis_dir(self) -> Path:
        return self.root / self.runtime.analysis_dir

    @property
    def reports_dir(self) -> Path:
        return self.root / self.reporting.output_dir

    @property
    def contact_email(self) -> str:
        return os.environ.get("RESEARCH_AGENT_CONTACT_EMAIL", "").strip()

    def tag(self, tag_id: str) -> TrendTag | None:
        return next((t for t in self.trends.tags if t.id == tag_id), None)


_FILES: dict[str, tuple[str, type[BaseModel], str | None]] = {
    "profile": ("research_profile.yaml", ResearchProfile, None),
    "sources": ("sources.yaml", SourcesFile, None),
    "journals": ("journals.yaml", JournalsFile, None),
    "queries": ("search_queries.yaml", SearchQueriesFile, None),
    "scoring": ("scoring.yaml", ScoringConfig, "scoring"),
    "trends": ("trends.yaml", TrendsFile, None),
    "ranking": ("ranking.yaml", RankingConfig, "ranking"),
    "llm": ("llm.yaml", LLMConfig, "llm"),
    "reporting": ("reporting.yaml", ReportingConfig, "reporting"),
    "validation": ("validation.yaml", ValidationConfig, "validation"),
    "runtime": ("runtime.yaml", RuntimeConfig, "runtime"),
}


def _short_hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:10]


def load_settings(root: Path | str | None = None) -> Settings:
    """Load and validate every config file. Raises ConfigError with the file name on failure."""
    root = Path(root or os.environ.get("RESEARCH_AGENT_ROOT") or Path.cwd()).resolve()
    cfg_dir = root / "config"
    raw_all: dict[str, Any] = {}
    parsed: dict[str, Any] = {}
    for key, (fname, model, inner) in _FILES.items():
        path = cfg_dir / fname
        if not path.exists():
            raise ConfigError(f"missing configuration file: {path}")
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"{fname}: invalid YAML: {exc}") from exc
        raw_all[key] = raw
        try:
            obj = model.model_validate(raw[inner] if inner else raw)
        except (KeyError, TypeError) as exc:
            raise ConfigError(f"{fname}: missing top-level key {inner!r}") from exc
        except Exception as exc:  # pydantic.ValidationError
            raise ConfigError(f"{fname}: {exc}") from exc
        parsed[key] = obj

    settings = Settings(root=root, config_hash=_short_hash(raw_all), **parsed)
    _cross_validate(settings)
    return settings


def _cross_validate(s: Settings) -> None:
    for d in s.scoring.llm_dimensions:
        if not (s.root / d.prompt).exists():  # type: ignore[operator]
            raise ConfigError(f"scoring.yaml: prompt file not found for {d.id}: {d.prompt}")
    for stage in ("scoring", "narrative"):
        if stage not in s.llm.stages:
            raise ConfigError(f"llm.yaml: missing stage {stage!r}")
