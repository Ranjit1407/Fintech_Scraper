import asyncio

from app.compliance.checker import check_access
from app.compliance.pii import PiiDetector, check_pii, luhn_valid
from app.compliance.robots import RobotsPolicy
from app.compliance.terms import analyze_terms_text, find_terms_links
from app.config import get_settings
from app.scraper.http_scraper import FetchResult, HttpScraper, parse_retry_after
from app.scraper.parser import Link
from app.validation.schemas import CheckStatus
from tests.fixtures import ROBOTS_BLOCK_BOT, make_transport


def _robots_check(robots_txt: str, url: str):
    async def run():
        async with HttpScraper(get_settings(), transport=make_transport(robots=robots_txt)) as scraper:
            return await RobotsPolicy(scraper).check(url)

    return asyncio.run(run())


def test_robots_allows_and_disallows():
    assert _robots_check("User-agent: *\nDisallow: /private\n", "https://bank.example/cards").status == CheckStatus.PASS
    assert _robots_check("User-agent: *\nDisallow: /private\n", "https://bank.example/private/x").status == CheckStatus.FAIL
    # rule targeted at our user agent
    assert _robots_check(ROBOTS_BLOCK_BOT, "https://bank.example/cards").status == CheckStatus.FAIL


def test_robots_crawl_delay_reported():
    check = _robots_check("User-agent: *\nCrawl-delay: 5\n", "https://bank.example/cards")
    assert check.status == CheckStatus.PASS
    assert check.details["crawl_delay"] == 5


def test_terms_analysis():
    prohibited = analyze_terms_text(
        "You agree to our terms. You may not use any robot, spider or scraper to access the site. Enjoy."
    )
    assert len(prohibited["prohibitions"]) == 1

    stated = analyze_terms_text("Automated scraping of this website is strictly prohibited.")
    assert stated["prohibitions"]

    mention = analyze_terms_text("Our site publishes a robots.txt file for search engines.")
    assert not mention["prohibitions"] and mention["mentions"]

    assert analyze_terms_text("Rates are subject to change without notice.") == {"prohibitions": [], "mentions": []}


def test_find_terms_links_prefers_terms_over_privacy():
    links = [
        Link("https://bank.example/privacy", "Privacy policy"),
        Link("https://bank.example/legal/terms", "Terms and conditions"),
        Link("https://other.example/terms", "Terms"),
    ]
    assert find_terms_links(links, "https://bank.example/cards") == ["https://bank.example/legal/terms"]


def test_pii_regex_detection_and_redaction():
    detector = PiiDetector(get_settings())
    assert detector.engine == "regex"
    text = "Mail jane.doe@example.com, card 4111 1111 1111 1111, call +44 20 7946 0958. Rate 19.9% on £1,000."
    redacted, count = detector.redact(text)
    assert count == 3
    assert "<EMAIL_ADDRESS>" in redacted and "<CREDIT_CARD>" in redacted and "<PHONE_NUMBER>" in redacted
    assert "19.9%" in redacted and "£1,000" in redacted
    assert luhn_valid("4111111111111111") and not luhn_valid("4111111111111112")


def test_pii_check_blocks_personal_data_at_scale():
    detector = PiiDetector(get_settings())
    directory = " ".join(f"person{i}@example.com" for i in range(30))
    assert check_pii(directory, detector, block_threshold=25).status == CheckStatus.FAIL
    assert check_pii("contact help@bank.example", detector, block_threshold=25).status == CheckStatus.WARN
    assert check_pii("Fixed rate 5.1% for 2 years", detector, block_threshold=25).status == CheckStatus.PASS


def _landing(status: int, body: str = "<html><title>Cards</title><body>ok</body></html>", **headers) -> FetchResult:
    return FetchResult("https://bank.example/", "https://bank.example/", status,
                       {"content-type": "text/html", **headers}, body, 0.1)


def test_access_check():
    assert check_access(_landing(200)).status == CheckStatus.PASS
    assert check_access(_landing(403)).status == CheckStatus.FAIL
    assert check_access(_landing(200, "<html><title>Just a moment...</title></html>")).status == CheckStatus.FAIL
    assert check_access(_landing(200, **{"cf-mitigated": "challenge"})).status == CheckStatus.FAIL


def test_retry_after_parsing():
    assert parse_retry_after("120") == 120
    assert parse_retry_after(None) is None
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0
