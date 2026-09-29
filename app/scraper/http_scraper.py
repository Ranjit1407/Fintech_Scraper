from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.config import Settings, get_settings

log = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_WAIT = 60.0
DEFAULT_ACCEPT = "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8"
_META_CHARSET = re.compile(rb"""<meta[^>]+charset=["']?([\w-]+)""", re.IGNORECASE)


class FetchError(RuntimeError):
    pass


@dataclass
class FetchResult:
    url: str
    final_url: str
    status_code: int
    headers: dict[str, str]
    text: str
    elapsed: float
    method: str = "http"
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    @property
    def is_json(self) -> bool:
        return self.content_type in ("application/json", "application/ld+json") or self.content_type.endswith("+json")

    def json(self) -> Any:
        return json.loads(self.text)


def host_of(url: str) -> str:
    return urlsplit(url).netloc.lower()


class HostRateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_slot: dict[str, float] = {}
        self._min_interval: dict[str, float] = {}

    def set_min_interval(self, host: str, seconds: float) -> None:
        with self._lock:
            self._min_interval[host] = max(self._min_interval.get(host, 0.0), seconds)

    def defer(self, host: str, seconds: float) -> None:
        with self._lock:
            self._next_slot[host] = max(self._next_slot.get(host, 0.0), time.monotonic() + seconds)

    async def wait(self, host: str, default_interval: float) -> None:
        with self._lock:
            interval = max(default_interval, self._min_interval.get(host, 0.0))
            now = time.monotonic()
            slot = max(now, self._next_slot.get(host, 0.0))
            self._next_slot[host] = slot + interval
        if slot > now:
            await asyncio.sleep(slot - now)


rate_limiter = HostRateLimiter()  # shared so concurrent jobs respect one schedule per host


def parse_retry_after(value: str | None) -> float | None:
    """Retry-After is either delay-seconds or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _backoff(attempt: int) -> float:
    return min(2**attempt + random.uniform(0, 1), 30.0)


def _decode(body: bytes, charset: str | None) -> str:
    encoding = charset
    if not encoding:
        match = _META_CHARSET.search(body[:4096])
        encoding = match.group(1).decode("ascii", "ignore") if match else "utf-8"
    try:
        return body.decode(encoding, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


class HttpScraper:
    def __init__(self, settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings or get_settings()
        self._client = httpx.AsyncClient(
            headers={"User-Agent": self.settings.user_agent, "Accept-Language": "en;q=0.9, *;q=0.5"},
            timeout=self.settings.request_timeout,
            follow_redirects=True,
            transport=transport,
        )

    async def __aenter__(self) -> HttpScraper:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch(self, url: str, *, accept: str = DEFAULT_ACCEPT) -> FetchResult:
        host = host_of(url)
        max_attempts = max(1, self.settings.max_retries)
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            await rate_limiter.wait(host, self.settings.min_request_interval)
            started = time.monotonic()
            try:
                result = await self._get(url, accept, started)
            except httpx.HTTPError as exc:
                last_error = exc
                log.warning("GET %s failed (attempt %d/%d): %s", url, attempt, max_attempts, exc)
                if attempt < max_attempts:
                    await asyncio.sleep(_backoff(attempt))
                continue

            if result.status_code in RETRY_STATUSES and attempt < max_attempts:
                wait = min(parse_retry_after(result.headers.get("retry-after")) or _backoff(attempt), MAX_RETRY_WAIT)
                log.warning("GET %s -> %d; retrying in %.1fs", url, result.status_code, wait)
                rate_limiter.defer(host, wait)
                continue

            log.info("GET %s -> %d (%.2fs)", url, result.status_code, result.elapsed)
            return result

        raise FetchError(f"Could not fetch {url}: {last_error}")

    async def _get(self, url: str, accept: str, started: float) -> FetchResult:
        limit = self.settings.max_response_bytes
        async with self._client.stream("GET", url, headers={"Accept": accept}) as response:
            body = bytearray()
            truncated = False
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > limit:
                    del body[limit:]
                    truncated = True
                    log.warning("Response from %s truncated at %d bytes", url, limit)
                    break
            return FetchResult(
                url=url,
                final_url=str(response.url),
                status_code=response.status_code,
                headers={k.lower(): v for k, v in response.headers.items()},
                text=_decode(bytes(body), response.charset_encoding),
                elapsed=time.monotonic() - started,
                truncated=truncated,
            )
