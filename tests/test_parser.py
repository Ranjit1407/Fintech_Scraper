from app.scraper.browser_scraper import needs_browser
from app.scraper.parser import find_financial_figures, parse_html, parse_json_payload
from tests.fixtures import CARDS_HTML


def test_parse_html_extracts_and_cleans():
    page = parse_html(CARDS_HTML, "https://bank.example/cards")

    assert page.title == "Credit cards | Example Bank"
    assert page.metadata["og:site_name"] == "Example Bank"
    assert any(item.get("@type") == "CreditCard" for item in page.json_ld)

    text = page.text
    assert "Platinum Rewards Credit Card" in text
    assert "We use cookies" not in text  # cookie banner removed
    assert "Buy a new car" not in text  # ad removed
    assert "Home" not in text  # navigation removed
    assert "All rights reserved" not in text  # site footer removed
    assert text.count("Find the right card for you.") == 1  # duplicate removed

    assert len(page.tables) == 1
    table = page.tables[0]
    assert table.columns == ["Card", "Purchase APR", "Annual fee"]
    assert table.rows[0] == {"Card": "Basic Card", "Purchase APR": "19.9%", "Annual fee": "£0"}

    percents = {f.value for f in page.figures if f.kind == "percentage"}
    assert {24.9, 19.9, 21.9} <= percents
    assert any(f.kind == "amount" and f.value == 95 and f.currency == "GBP" for f in page.figures)


def test_sections_group_blocks_under_headings():
    page = parse_html(CARDS_HTML, "https://bank.example/cards")
    section = next(s for s in page.sections if s.heading == "Platinum Rewards Credit Card")
    assert "Earn 2x points on travel" in section.text


def test_financial_figures():
    figures = find_financial_figures("Borrow $1,500 to $25k at 6.99% APR. Fee: Rs. 499 or 2,500 INR. Hours 5")
    amounts = sorted(f.value for f in figures if f.kind == "amount")
    assert amounts == [499, 1500, 2500, 25000]
    assert [f.value for f in figures if f.kind == "percentage"] == [6.99]


def test_parse_json_payload_builds_table():
    data = {"meta": {"source": "Example API"}, "products": [
        {"name": "Saver", "rate": {"aer": 4.1}}, {"name": "Bonus Saver", "rate": {"aer": 4.6}},
    ]}
    page = parse_json_payload(data, "https://api.bank.example/products")
    assert page.tables[0].columns == ["name", "rate.aer"]
    assert len(page.tables[0].rows) == 2
    assert any("Example API" in b.text for b in page.blocks)
    assert not any("Bonus Saver" in b.text for b in page.blocks)  # records live in the table


def test_needs_browser():
    assert needs_browser('<html><body><div id="root"></div><script src="app.js"></script></body></html>')
    assert not needs_browser(CARDS_HTML)
