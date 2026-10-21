"""Provider-independent ingestion interface."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from research_agent.ingestion.http import HttpClient
from research_agent.metadata.schemas import SourceRecord
from research_agent.retrieval.query_builder import QuerySpec
from research_agent.settings import SourceConfig


@dataclass
class Page:
    records: list[SourceRecord]
    next_cursor: str | None             # None => window complete
    raw_count: int = 0                  # records in the response before mapping
    skipped: int = 0                    # records dropped (no identifier / unsupported type)
    warnings: list[str] = field(default_factory=list)


class Provider(Protocol):
    name: str

    def fetch_window(self, spec: QuerySpec, start: date, end: date, cursor: str | None) -> Iterator[Page]:
        """Yield pages for an inclusive [start, end] window, resuming from `cursor` if given.
        Raise ProviderError on failure; pages already yielded stay persisted."""
        ...


class BaseProvider:
    name = "base"

    def __init__(self, http: HttpClient, config: SourceConfig, *, contact_email: str = "", api_key: str = ""):
        self.http = http
        self.cfg = config
        self.email = contact_email
        self.api_key = api_key
        http.set_rate(self.name, config.min_interval_seconds)

    def extra(self, key: str, default=None):
        return (self.cfg.model_extra or {}).get(key, default)


def as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def map_all(fn, items: list, *args) -> tuple[list[SourceRecord], list[str]]:
    """Map raw items to records; a malformed item is skipped with a warning instead of
    failing the whole page (it is retried on the next overlapping window)."""
    recs, warnings = [], []
    for it in items:
        try:
            r = fn(it, *args)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            warnings.append(f"malformed record skipped: {type(exc).__name__}: {str(exc)[:160]}")
            continue
        if r is not None:
            recs.append(r)
    return recs, warnings
