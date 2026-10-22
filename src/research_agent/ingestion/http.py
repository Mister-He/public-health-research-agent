"""HTTP client with per-provider rate limiting, retries (exponential backoff + full jitter),
429/Retry-After handling and structured, secret-free error logging."""

from __future__ import annotations

import logging
import random
import time
from email.utils import parsedate_to_datetime
from typing import Any, Callable

import httpx

from research_agent import __version__
from research_agent.storage.repositories import RecordedUsage

log = logging.getLogger("research_agent.http")

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
_SECRET_PARAMS = {"api_key", "apikey", "key", "token"}


class ProviderError(RuntimeError):
    """A provider request failed after retries (or permanently)."""

    def __init__(self, provider: str, message: str, *, status: int | None = None, retryable: bool = True):
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.status = status
        self.retryable = retryable


class SchemaError(ProviderError):
    """Response did not have the documented shape."""

    def __init__(self, provider: str, message: str):
        super().__init__(provider, f"unexpected response schema: {message}", retryable=False)


class WindowTooLarge(ProviderError):
    """A date window holds more records than the per-window safety limit; the caller splits it."""

    def __init__(self, provider: str, count: int, limit: int):
        super().__init__(provider, f"window has {count} records (> limit {limit})", retryable=True)
        self.count = count


def redact_url(url: str) -> str:
    u = httpx.URL(url)
    params = [(k, "***" if k.lower() in _SECRET_PARAMS else v) for k, v in u.params.multi_items()]
    return str(u.copy_with(params=params)) if params else str(u)


class HttpClient:
    def __init__(
        self,
        *,
        contact_email: str = "",
        connect_timeout: float = 10,
        read_timeout: float = 60,
        max_retries: int = 5,
        backoff_base: float = 1.5,
        backoff_max: float = 60,
        max_retry_after: float = 120,
        user_agent: str = "research-agent/{version} (mailto:{email})",
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.contact_email = contact_email
        ua = user_agent.format(version=__version__, email=contact_email or "unset")
        self.client = httpx.Client(
            timeout=httpx.Timeout(read_timeout, connect=connect_timeout),
            headers={"User-Agent": ua},
            follow_redirects=True,
            transport=transport,
        )
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.max_retry_after = max_retry_after
        self._sleep = sleep
        self._clock = clock
        self._intervals: dict[str, float] = {}
        self._last: dict[str, float] = {}
        self.usage: dict[str, RecordedUsage] = {}

    def set_rate(self, provider: str, min_interval_seconds: float) -> None:
        self._intervals[provider] = min_interval_seconds

    def _throttle(self, provider: str) -> None:
        interval = self._intervals.get(provider, 0.0)
        last = self._last.get(provider)
        if last is not None and interval > 0:
            wait = interval - (self._clock() - last)
            if wait > 0:
                self._sleep(wait)
        self._last[provider] = self._clock()

    def _u(self, provider: str) -> RecordedUsage:
        return self.usage.setdefault(provider, RecordedUsage(provider))

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return max(retry_after, 0.0)
        return random.uniform(0, min(self.backoff_max, self.backoff_base * (2 ** attempt)))

    def request(self, provider: str, method: str, url: str, *, params: dict[str, Any] | None = None,
                headers: dict[str, str] | None = None, retry_statuses: set[int] = RETRYABLE_STATUS,
                json: Any = None) -> httpx.Response:
        u = self._u(provider)
        last_error = ""
        status = None
        for attempt in range(self.max_retries + 1):
            self._throttle(provider)
            u.requests += 1
            try:
                resp = self.client.request(method, url, params=params, headers=headers, json=json)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"{type(exc).__name__}"
                status = None
                retry_after = None
            else:
                status = resp.status_code
                if status < 400:
                    return resp
                if status not in retry_statuses:
                    u.failures += 1
                    log.warning("http_error", extra={"provider": provider, "status": status,
                                                     "url": redact_url(str(resp.request.url)), "attempt": attempt})
                    raise ProviderError(provider, f"HTTP {status} for {redact_url(str(resp.request.url))}",
                                        status=status, retryable=False)
                if status == 429:
                    u.rate_limited += 1
                retry_after = _retry_after(resp)
                last_error = f"HTTP {status}"
                if retry_after is not None and retry_after > self.max_retry_after:
                    # e.g. a daily budget exhausted: waiting would stall the whole run. Fail this
                    # provider now; its watermark is kept and the window is caught up next run.
                    u.failures += 1
                    raise ProviderError(provider, f"HTTP {status}; server asks to retry after {retry_after:.0f}s",
                                        status=status, retryable=True)
            if attempt < self.max_retries:
                u.retries += 1
                delay = self._backoff(attempt, retry_after)
                log.info("http_retry", extra={"provider": provider, "status": status, "error": last_error,
                                              "attempt": attempt + 1, "delay_s": round(delay, 2)})
                self._sleep(delay)
        u.failures += 1
        log.error("http_failed", extra={"provider": provider, "status": status, "error": last_error,
                                        "url": redact_url(url)})
        raise ProviderError(provider, f"giving up after {self.max_retries + 1} attempts: {last_error}", status=status)

    def get_json(self, provider: str, url: str, **kw: Any) -> Any:
        resp = self.request(provider, "GET", url, **kw)
        try:
            return resp.json()
        except ValueError as exc:
            raise SchemaError(provider, f"invalid JSON ({exc})") from exc

    def get_text(self, provider: str, url: str, **kw: Any) -> str:
        return self.request(provider, "GET", url, **kw).text

    def close(self) -> None:
        self.client.close()


def _retry_after(resp: httpx.Response) -> float | None:
    v = resp.headers.get("Retry-After")
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        try:
            from datetime import UTC, datetime
            return (parsedate_to_datetime(v) - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError):
            return None
