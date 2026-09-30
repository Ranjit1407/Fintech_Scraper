# A discovered developer portal or spec is only a WARN, so an admin can arrange proper API access.

from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlsplit

from app.compliance.robots import RobotsPolicy
from app.scraper.http_scraper import FetchError, FetchResult, HttpScraper
from app.scraper.parser import Link
from app.validation.schemas import ApiDiscovery, CheckStatus, ComplianceCheck

log = logging.getLogger(__name__)

SPEC_PATHS = ("/openapi.json", "/swagger.json")
PORTAL_PATHS = ("/developers", "/api")
API_TEXT_RE = re.compile(r"\b(api|apis|developers?|developer portal|open data)\b", re.IGNORECASE)
API_URL_RE = re.compile(r"(//(api|developers?)\.|/developers?\b|/api-docs|/openapi|/swagger)", re.IGNORECASE)


def _looks_like_spec(result: FetchResult) -> bool:
    try:
        data = json.loads(result.text)
    except (json.JSONDecodeError, ValueError):
        return False
    return isinstance(data, dict) and ("openapi" in data or "swagger" in data)


async def check_api(
    page_url: str,
    links: list[Link],
    scraper: HttpScraper,
    robots: RobotsPolicy,
    api_endpoint: str | None = None,
) -> tuple[ComplianceCheck, ApiDiscovery, FetchResult | None]:
    discovery = ApiDiscovery()
    api_result: FetchResult | None = None

    # A user-supplied endpoint is not subject to robots.txt, which governs crawlers.
    if api_endpoint:
        try:
            result = await scraper.fetch(api_endpoint, accept="application/json")
            if result.ok:
                result.json()
                discovery.available = True
                discovery.json_endpoint = result.final_url
                api_result = result
            else:
                discovery.notes.append(f"Supplied API endpoint returned HTTP {result.status_code}")
        except (FetchError, json.JSONDecodeError, ValueError) as exc:
            discovery.notes.append(f"Supplied API endpoint is not usable JSON: {exc}")

    for link in links:
        if API_TEXT_RE.search(link.text) or API_URL_RE.search(link.url):
            if link.url not in discovery.portal_urls:
                discovery.portal_urls.append(link.url)
        if len(discovery.portal_urls) >= 5:
            break

    if scraper.settings.api_probe_enabled and not discovery.json_endpoint:
        parts = urlsplit(page_url)
        origin = f"{parts.scheme}://{parts.netloc}"
        for path in SPEC_PATHS + PORTAL_PATHS:
            probe = origin + path
            if not await robots.can_fetch(probe):
                continue
            try:
                result = await scraper.fetch(probe, accept="application/json,text/html;q=0.9")
            except FetchError:
                continue
            if not result.ok:
                continue
            if path in SPEC_PATHS and _looks_like_spec(result):
                discovery.spec_urls.append(result.final_url)
            elif path in PORTAL_PATHS and "html" in result.content_type:
                landed = urlsplit(result.final_url).path.rstrip("/")
                if landed and landed != "/" and result.final_url not in discovery.portal_urls:
                    discovery.portal_urls.append(result.final_url)

    if discovery.spec_urls or discovery.portal_urls:
        discovery.available = True

    details = discovery.model_dump()
    if discovery.json_endpoint:
        return ComplianceCheck(name="official_api", status=CheckStatus.PASS,
                               summary="Official API endpoint verified; data will be collected from the API",
                               details=details), discovery, api_result
    if discovery.available:
        return ComplianceCheck(name="official_api", status=CheckStatus.WARN,
                               summary="An official API or developer portal appears to exist; prefer it over "
                                       "scraping (supply api_endpoint to use it)",
                               details=details), discovery, None
    return ComplianceCheck(name="official_api", status=CheckStatus.PASS,
                           summary="No official API found; the web scraping route will be used",
                           details=details), discovery, None
