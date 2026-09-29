from __future__ import annotations

import logging
import time

from app.config import Settings, get_settings
from app.scraper.http_scraper import FetchResult, host_of, rate_limiter
from app.scraper.parser import make_soup

log = logging.getLogger(__name__)

SPA_ROOT_IDS = ("root", "app", "__next", "__nuxt", "svelte", "main-app")
BLOCKED_RESOURCES = {"image", "media", "font"}


class BrowserUnavailable(RuntimeError):
    pass


def needs_browser(html: str) -> bool:
    soup = make_soup(html)
    script_count = len(soup.find_all("script"))
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    body = soup.body or soup
    text_length = len(body.get_text(" ", strip=True))

    if text_length < 300 and script_count > 0:
        return True
    for root_id in SPA_ROOT_IDS:
        root = soup.find(id=root_id)
        if root is not None and len(root.get_text(" ", strip=True)) < 50 and text_length < 1500:
            return True
    return False


async def fetch_rendered(url: str, settings: Settings | None = None) -> FetchResult:
    settings = settings or get_settings()
    try:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import TimeoutError as PlaywrightTimeout
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise BrowserUnavailable("playwright is not installed") from exc

    await rate_limiter.wait(host_of(url), settings.min_request_interval)
    started = time.monotonic()

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await browser.new_context(user_agent=settings.user_agent)
                page = await context.new_page()

                async def block_heavy(route):
                    if route.request.resource_type in BLOCKED_RESOURCES:
                        await route.abort()
                    else:
                        await route.continue_()

                await page.route("**/*", block_heavy)
                response = await page.goto(url, wait_until="domcontentloaded", timeout=settings.browser_timeout_ms)
                try:
                    await page.wait_for_load_state("networkidle", timeout=10_000)
                except PlaywrightTimeout:
                    log.debug("Network did not go idle for %s; using current DOM", url)

                html = await page.content()
                status = response.status if response else 200
                headers = await response.all_headers() if response else {}
                final_url = page.url
            finally:
                await browser.close()
    except NotImplementedError as exc:  # Windows selector event loop cannot spawn subprocesses
        raise BrowserUnavailable("the current event loop cannot launch a browser subprocess") from exc
    except PlaywrightError as exc:
        if "Executable doesn't exist" in str(exc):
            raise BrowserUnavailable("Chromium is not installed; run `playwright install chromium`") from exc
        raise

    elapsed = time.monotonic() - started
    log.info("Rendered %s with Playwright -> %d (%.2fs)", url, status, elapsed)
    return FetchResult(
        url=url,
        final_url=final_url,
        status_code=status,
        headers={k.lower(): v for k, v in headers.items()},
        text=html,
        elapsed=elapsed,
        method="browser",
    )
