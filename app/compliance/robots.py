# Fetched with our own client so the request carries the bot's User-Agent and obeys rate limits.
# A missing file allows crawling; auth errors, server errors and network failures count as disallow.

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from app.scraper.http_scraper import FetchError, HttpScraper
from app.validation.schemas import CheckStatus, ComplianceCheck

log = logging.getLogger(__name__)


@dataclass
class _RobotsEntry:
    parser: RobotFileParser
    robots_url: str
    status_code: int | None
    note: str


class RobotsPolicy:
    def __init__(self, scraper: HttpScraper):
        self._scraper = scraper
        self._user_agent = scraper.settings.user_agent
        self._cache: dict[str, _RobotsEntry] = {}

    async def _entry(self, url: str) -> _RobotsEntry:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin in self._cache:
            return self._cache[origin]

        robots_url = f"{origin}/robots.txt"
        parser = RobotFileParser(robots_url)
        status: int | None = None
        try:
            result = await self._scraper.fetch(robots_url, accept="text/plain,*/*;q=0.5")
            status = result.status_code
            if status in (401, 403):
                parser.disallow_all = True
                note = f"robots.txt returned HTTP {status}; treating the site as disallowed"
            elif 400 <= status < 500:
                parser.allow_all = True
                note = f"No robots.txt (HTTP {status}); no crawler restrictions published"
            elif status >= 500:
                parser.disallow_all = True
                note = f"robots.txt unavailable (HTTP {status}); assuming disallowed until it recovers"
            else:
                parser.parse(result.text.splitlines())
                note = "robots.txt parsed"
        except FetchError as exc:
            parser.disallow_all = True
            note = f"robots.txt unreachable ({exc}); assuming disallowed"
        parser.modified()  # mark as loaded so can_fetch() evaluates the rules

        entry = _RobotsEntry(parser, robots_url, status, note)
        self._cache[origin] = entry
        log.info("%s: %s", robots_url, note)
        return entry

    async def can_fetch(self, url: str) -> bool:
        entry = await self._entry(url)
        return entry.parser.can_fetch(self._user_agent, url)

    async def check(self, url: str) -> ComplianceCheck:
        entry = await self._entry(url)
        allowed = entry.parser.can_fetch(self._user_agent, url)
        delay = entry.parser.crawl_delay(self._user_agent)
        rate = entry.parser.request_rate(self._user_agent)
        details = {
            "robots_url": entry.robots_url,
            "http_status": entry.status_code,
            "allowed": allowed,
            "crawl_delay": float(delay) if delay is not None else None,
            "request_rate": {"requests": rate.requests, "seconds": rate.seconds} if rate else None,
            "sitemaps": (entry.parser.site_maps() or [])[:10],
            "note": entry.note,
        }
        if allowed:
            return ComplianceCheck(name="robots_txt", status=CheckStatus.PASS,
                                   summary=f"robots.txt allows this URL ({entry.note})", details=details)
        return ComplianceCheck(name="robots_txt", status=CheckStatus.FAIL,
                               summary=f"robots.txt disallows this URL for our crawler ({entry.note})",
                               details=details)
