# Any FAIL blocks the job; WARN is recorded and the job proceeds.

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from app.ai.llm_client import LLMClient
from app.compliance.api_checker import check_api
from app.compliance.license import check_license
from app.compliance.pii import check_pii, get_pii_detector
from app.compliance.rate_limits import check_rate_limits
from app.compliance.robots import RobotsPolicy
from app.compliance.terms import check_terms
from app.scraper.http_scraper import FetchError, FetchResult, HttpScraper
from app.scraper.parser import extract_links, extract_metadata, make_soup, parse_html
from app.validation.schemas import CheckStatus, ComplianceCheck, ComplianceReport

log = logging.getLogger(__name__)

SUPPORTED_CONTENT_TYPES = {"text/html", "application/xhtml+xml", "application/json", "application/ld+json", ""}
CHALLENGE_TITLES = ("just a moment", "attention required", "access denied", "are you a robot", "captcha",
                    "security check", "verify you are human")


@dataclass
class ComplianceOutcome:
    report: ComplianceReport
    landing: FetchResult | None = None
    api_result: FetchResult | None = None


def check_access(landing: FetchResult) -> ComplianceCheck:
    status = landing.status_code
    details = {"http_status": status, "final_url": landing.final_url, "content_type": landing.content_type}
    if status in (401, 403):
        return ComplianceCheck(name="page_access", status=CheckStatus.FAIL, details=details,
                               summary=f"Access denied (HTTP {status}); access controls are never bypassed")
    if status == 429:
        return ComplianceCheck(name="page_access", status=CheckStatus.FAIL, details=details,
                               summary="Rate limited (HTTP 429) even after backing off")
    if status >= 400:
        return ComplianceCheck(name="page_access", status=CheckStatus.FAIL, details=details,
                               summary=f"Page returned HTTP {status}")

    if "html" in landing.content_type or not landing.content_type:
        soup = make_soup(landing.text[:200_000])
        title = (soup.title.get_text(" ", strip=True) if soup.title else "").lower()
        challenged = landing.headers.get("cf-mitigated") == "challenge" or any(t in title for t in CHALLENGE_TITLES)
        if challenged:
            return ComplianceCheck(name="page_access", status=CheckStatus.FAIL, details=details,
                                   summary="Bot challenge / CAPTCHA detected; the site does not permit automated access")

    if landing.content_type not in SUPPORTED_CONTENT_TYPES:
        return ComplianceCheck(name="page_access", status=CheckStatus.FAIL, details=details,
                               summary=f"Unsupported content type {landing.content_type!r}")
    return ComplianceCheck(name="page_access", status=CheckStatus.PASS, details=details,
                           summary=f"Page reachable (HTTP {status})")


def _report(url: str, checks: list[ComplianceCheck], **kwargs) -> ComplianceReport:
    allowed = not any(c.status == CheckStatus.FAIL for c in checks)
    return ComplianceReport(url=url, allowed=allowed, checks=checks, **kwargs)


async def run_compliance_checks(
    url: str,
    scraper: HttpScraper,
    *,
    api_endpoint: str | None = None,
    llm: LLMClient | None = None,
) -> ComplianceOutcome:
    settings = scraper.settings
    llm = llm or LLMClient(settings)
    robots = RobotsPolicy(scraper)
    checks: list[ComplianceCheck] = []

    # robots.txt must be checked before the page itself is requested.
    robots_check = await robots.check(url)
    checks.append(robots_check)
    if robots_check.status == CheckStatus.FAIL:
        return ComplianceOutcome(_report(url, checks))

    try:
        landing = await scraper.fetch(url)
    except FetchError as exc:
        checks.append(ComplianceCheck(name="page_access", status=CheckStatus.FAIL, summary=str(exc)))
        return ComplianceOutcome(_report(url, checks))

    if landing.final_url != url and not await robots.can_fetch(landing.final_url):
        checks.append(ComplianceCheck(name="robots_txt_redirect", status=CheckStatus.FAIL,
                                      summary=f"Redirected to {landing.final_url}, which robots.txt disallows"))
        return ComplianceOutcome(_report(url, checks), landing)

    access_check = check_access(landing)
    checks.append(access_check)
    rate_check, interval = check_rate_limits(landing, robots_check.details, settings.min_request_interval)
    checks.append(rate_check)
    if access_check.status == CheckStatus.FAIL:
        return ComplianceOutcome(_report(url, checks, request_interval=interval), landing)

    is_html = not landing.is_json
    if is_html:
        soup = make_soup(landing.text)
        links = extract_links(soup, landing.final_url)
        metadata = extract_metadata(soup, landing.final_url)
        raw_text = soup.get_text(" ", strip=True)
    else:
        links, metadata, raw_text = [], {}, landing.text[:200_000]

    terms_check, terms_text = await check_terms(landing.final_url, links, scraper, robots, llm)
    checks.append(terms_check)

    api_check, discovery, api_result = await check_api(landing.final_url, links, scraper, robots, api_endpoint)
    checks.append(api_check)

    checks.append(await check_license(raw_text, links, metadata, terms_text, llm))

    detector = get_pii_detector()
    if is_html:
        content_text = await asyncio.to_thread(lambda: parse_html(landing.text, landing.final_url).text)
    else:
        content_text = raw_text
    checks.append(await asyncio.to_thread(check_pii, content_text, detector, settings.pii_block_threshold))

    report = _report(url, checks, api=discovery, request_interval=interval)
    log.info("Compliance for %s: %s (%s)", url, "ALLOW" if report.allowed else "BLOCK",
             ", ".join(f"{c.name}={c.status.value}" for c in checks))
    return ComplianceOutcome(report, landing, api_result)
