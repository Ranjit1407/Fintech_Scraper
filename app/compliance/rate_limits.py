from __future__ import annotations

from app.scraper.http_scraper import FetchResult, host_of, parse_retry_after, rate_limiter
from app.validation.schemas import CheckStatus, ComplianceCheck

RATE_LIMIT_HEADERS = (
    "retry-after", "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset",
    "ratelimit", "ratelimit-policy", "ratelimit-limit", "ratelimit-remaining", "ratelimit-reset",
)


def check_rate_limits(landing: FetchResult, robots_details: dict, min_interval: float) -> tuple[ComplianceCheck, float]:
    headers = {h: landing.headers[h] for h in RATE_LIMIT_HEADERS if h in landing.headers}
    crawl_delay = robots_details.get("crawl_delay") or 0.0
    request_rate = robots_details.get("request_rate")
    rate_interval = request_rate["seconds"] / request_rate["requests"] if request_rate and request_rate["requests"] else 0.0

    interval = max(min_interval, crawl_delay, rate_interval)
    host = host_of(landing.final_url)
    rate_limiter.set_min_interval(host, interval)
    details = {"effective_interval_seconds": interval, "crawl_delay": crawl_delay or None,
               "request_rate": request_rate, "headers": headers}

    if landing.status_code == 429:
        retry = parse_retry_after(landing.headers.get("retry-after"))
        return ComplianceCheck(name="rate_limits", status=CheckStatus.FAIL,
                               summary=f"Site is rate-limiting us (HTTP 429){f', retry after {retry:.0f}s' if retry else ''}",
                               details=details), interval

    remaining = headers.get("x-ratelimit-remaining") or headers.get("ratelimit-remaining")
    if remaining is not None and remaining.strip().split(",")[0].strip() == "0":
        return ComplianceCheck(name="rate_limits", status=CheckStatus.WARN,
                               summary="Rate-limit quota exhausted; requests will be slowed down",
                               details=details), interval
    if interval > 60:
        return ComplianceCheck(name="rate_limits", status=CheckStatus.WARN,
                               summary=f"Very long crawl delay ({interval:.0f}s between requests)",
                               details=details), interval
    return ComplianceCheck(name="rate_limits", status=CheckStatus.PASS,
                           summary=f"Requests to {host} spaced {interval:g}s apart", details=details), interval
