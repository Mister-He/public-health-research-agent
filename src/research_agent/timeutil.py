"""Date/time helpers. Persist UTC; display and name reports in Asia/Shanghai."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Asia/Shanghai")


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso_utc(dt: datetime | None = None) -> str:
    return (dt or utcnow()).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_today(now: datetime | None = None, tz: str = "Asia/Shanghai") -> date:
    """The report date: the calendar date in the configured zone (never the runner's zone)."""
    return (now or utcnow()).astimezone(ZoneInfo(tz)).date()


def report_filename(d: date) -> str:
    return f"{d.isoformat()}.md"


def parse_date(value: str | None) -> date | None:
    """Parse a full ISO date (YYYY-MM-DD or a datetime string). Partial dates return None
    here; use parse_partial_date when month/year precision is acceptable."""
    if not value:
        return None
    value = value.strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", value)
    if not m:
        return None
    try:
        return date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None


def parse_partial_date(parts: list[int] | tuple[int, ...] | None) -> tuple[date | None, str]:
    """Crossref-style date-parts -> (date, precision). Missing month/day => precision flag,
    date set to the first day so ordering is stable; callers must not treat it as exact."""
    if not parts or not parts[0]:
        return None, "none"
    y = int(parts[0])
    if len(parts) == 1 or parts[1] in (None, 0):
        return date(y, 1, 1), "year"
    mth = int(parts[1])
    if len(parts) == 2 or parts[2] in (None, 0):
        return date(y, mth, 1), "month"
    try:
        return date(y, mth, int(parts[2])), "day"
    except ValueError:
        return date(y, mth, 1), "month"


def date_chunks(start: date, end: date, chunk_days: int) -> list[tuple[date, date]]:
    """Inclusive [start, end] split into consecutive inclusive chunks."""
    if end < start:
        return []
    out = []
    cur = start
    while cur <= end:
        stop = min(end, cur + timedelta(days=chunk_days - 1))
        out.append((cur, stop))
        cur = stop + timedelta(days=1)
    return out
