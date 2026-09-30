"""A small fake website served through httpx.MockTransport."""

import httpx

CARDS_HTML = """<!doctype html>
<html lang="en"><head>
<title>Credit cards | Example Bank</title>
<meta name="description" content="Compare Example Bank credit cards">
<meta property="og:site_name" content="Example Bank">
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "CreditCard", "name": "Example Cashback Card",
 "annualPercentageRate": 22.9, "provider": {"@type": "BankOrCreditUnion", "name": "Example Bank"},
 "description": "Earn 1% cashback on every purchase."}
</script>
</head><body>
<header><nav><a href="/">Home</a> <a href="/cards">Cards</a></nav></header>
<div class="cookie-banner">We use cookies to improve your experience.</div>
<main>
  <h1>Credit cards</h1>
  <p>Find the right card for you.</p>
  <p>Find the right card for you.</p>
  <h2>Platinum Rewards Credit Card</h2>
  <p>Representative 24.9% APR variable. Annual fee £95.</p>
  <ul><li>Earn 2x points on travel</li><li>No foreign transaction fees</li></ul>
  <h3>Compare our everyday cards</h3>
  <table>
    <tr><th>Card</th><th>Purchase APR</th><th>Annual fee</th></tr>
    <tr><td>Basic Card</td><td>19.9%</td><td>£0</td></tr>
    <tr><td>Gold Card</td><td>21.9%</td><td>£50</td></tr>
  </table>
  <p>Questions? Email help@example-bank.test or call +44 20 7946 0958.</p>
  <div class="ad-slot">Buy a new car today!</div>
</main>
<footer>
  <a href="/terms">Terms of use</a> <a href="/privacy">Privacy policy</a>
  <p>&copy; 2026 Example Bank plc. All rights reserved.</p>
</footer>
</body></html>"""

TERMS_OK = "<html><body><h1>Terms of use</h1><p>" + (
    "These terms govern your use of this website. The information is provided for general guidance. "
    "We may update these terms from time to time. " * 5
) + "</p></body></html>"

TERMS_PROHIBITED = "<html><body><h1>Terms of use</h1><p>" + (
    "These terms govern your use of this website. "
    "You must not use any robot, spider, scraper or other automated means to access the site. " * 3
) + "</p></body></html>"

ROBOTS_OK = "User-agent: *\nDisallow: /private\n"
ROBOTS_BLOCK_BOT = "User-agent: WebScraperBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n"


def make_transport(robots: str = ROBOTS_OK, terms: str = TERMS_OK, cards_status: int = 200) -> httpx.MockTransport:
    pages = {
        "/robots.txt": (200, "text/plain", robots),
        "/cards": (cards_status, "text/html; charset=utf-8", CARDS_HTML),
        "/terms": (200, "text/html; charset=utf-8", terms),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        status, content_type, body = pages.get(request.url.path, (404, "text/html", "<h1>Not found</h1>"))
        return httpx.Response(status, headers={"content-type": content_type}, text=body)

    return httpx.MockTransport(handler)
