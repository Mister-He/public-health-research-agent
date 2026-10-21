"""URL verification with identity matching (more than an HTTP status).

Statuses: verified | verified_via_authoritative_metadata | temporarily_unavailable |
identity_mismatch | invalid. robots.txt is respected; bot protection (401/403/429/503, challenge
pages) is never taken as proof that an article does not exist."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from research_agent.metadata.normalization import normalize_doi, normalize_title

BOT_STATUSES = {401, 403, 405, 406, 429, 451, 503, 520, 521, 522, 523, 524, 525, 526}
_META_DOI = re.compile(r'<meta[^>]+name=["\'](?:citation_doi|dc\.identifier|prism\.doi)["\'][^>]*content=["\']([^"\']+)', re.I)
_META_TITLE = re.compile(r'<meta[^>]+name=["\']citation_title["\'][^>]*content=["\']([^"\']+)', re.I)
_CHALLENGE = re.compile(r"captcha|cf-chl|just a moment|are you a robot|access denied|enable javascript", re.I)


@dataclass
class LinkResult:
    status: str
    detail: str
    final_url: str | None = None


class LinkVerifier:
    def __init__(self, client: httpx.Client | None = None, *, timeout: float = 20, user_agent: str = "research-agent",
                 min_interval: float = 1.0, sleep=time.sleep):
        self.client = client or httpx.Client(timeout=timeout, headers={"User-Agent": user_agent}, follow_redirects=False)
        self.ua = user_agent
        self._robots: dict[str, RobotFileParser | None] = {}
        self._last: dict[str, float] = {}
        self.min_interval = min_interval
        self._sleep = sleep

    def _allowed(self, url: str) -> bool:
        host = urlparse(url).netloc
        if host not in self._robots:
            rp = RobotFileParser()
            try:
                r = self.client.get(f"{urlparse(url).scheme}://{host}/robots.txt")
                if r.status_code >= 400:
                    self._robots[host] = None      # no robots.txt => allowed
                else:
                    rp.parse(r.text.splitlines())
                    self._robots[host] = rp
            except httpx.HTTPError:
                self._robots[host] = None
        rp = self._robots[host]
        return True if rp is None else rp.can_fetch(self.ua, url)

    def _get(self, url: str) -> httpx.Response:
        host = urlparse(url).netloc
        wait = self.min_interval - (time.monotonic() - self._last.get(host, 0))
        if wait > 0:
            self._sleep(wait)
        self._last[host] = time.monotonic()
        return self.client.get(url)

    def check(self, url: str | None, *, doi: str | None, title: str | None, identity_verified: bool) -> LinkResult:
        fallback = "verified_via_authoritative_metadata" if identity_verified else "temporarily_unavailable"
        if not url or not re.match(r"^https://[^\s]+$", url):
            return LinkResult("invalid", f"malformed URL {url!r}")
        current = url
        try:
            for _ in range(8):  # follow redirects manually so robots.txt is checked per host
                if not self._allowed(current):
                    return LinkResult(fallback, f"robots.txt disallows {urlparse(current).netloc}; not fetched", current)
                r = self._get(current)
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    current = urljoin(current, r.headers["location"])
                    continue
                break
            else:
                return LinkResult(fallback, "too many redirects", current)
        except httpx.TimeoutException:
            return LinkResult("temporarily_unavailable", "timeout", current)
        except httpx.HTTPError as exc:
            return LinkResult("temporarily_unavailable", f"{type(exc).__name__}", current)
        code = r.status_code
        if code in (404, 410):
            return LinkResult("invalid", f"HTTP {code}", current)
        if code in BOT_STATUSES or (code == 200 and _CHALLENGE.search(r.text[:5000]) and not _META_DOI.search(r.text)):
            return LinkResult(fallback, f"landing page not accessible automatically (HTTP {code})", current)
        if code >= 500:
            return LinkResult("temporarily_unavailable", f"HTTP {code}", current)
        if code != 200:
            return LinkResult(fallback, f"HTTP {code}", current)
        html = r.text[:400_000]
        page_doi = next((normalize_doi(m) for m in _META_DOI.findall(html) if normalize_doi(m)), None)
        if doi and page_doi:
            if page_doi == doi:
                return LinkResult("verified", "citation_doi matches", current)
            return LinkResult("identity_mismatch", f"page DOI {page_doi} != {doi}", current)
        page_title = next(iter(_META_TITLE.findall(html)), None)
        if page_title and title:
            if normalize_title(page_title) == normalize_title(title):
                return LinkResult("verified", "citation_title matches", current)
            return LinkResult("identity_mismatch", f"page title {page_title[:120]!r} differs", current)
        if doi and doi in html.lower():
            return LinkResult("verified", "DOI present on landing page", current)
        if title and normalize_title(title) in normalize_title(html):
            return LinkResult("verified", "title present on landing page", current)
        return LinkResult(fallback, "landing page reachable but identity not machine-readable", current)


def link_check_status(result: LinkResult) -> str:
    """Map a link status to a gate status."""
    return {"verified": "pass", "verified_via_authoritative_metadata": "warn", "temporarily_unavailable": "warn",
            "identity_mismatch": "fail", "invalid": "fail"}[result.status]
