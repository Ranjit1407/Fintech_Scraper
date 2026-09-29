from app.ai.extraction import merge_missed_products
from app.scraper.parser import parse_html
from app.validation.grounding import PageEvidence, ground_product
from tests.fixtures import CARDS_HTML

PAGE = parse_html(CARDS_HTML, "https://bank.example/cards")
EVIDENCE = PageEvidence.from_page(PAGE)


def test_supported_facts_are_kept():
    product = ground_product({"name": "Gold Card", "confidence": 0.8,
                              "rates": [{"label": "Purchase APR", "value": 21.9}],
                              "fees": [{"label": "Annual fee", "amount": 50}]}, EVIDENCE)
    assert product["unverified"] == [] and product["confidence"] == 0.8


def test_invented_and_misattributed_facts_are_removed():
    product = ground_product({"name": "Basic Card", "confidence": 0.9,
                              "rates": [{"label": "Purchase APR", "value": 19.9},
                                        {"label": "Cash APR", "value": 29.9}],  # invented
                              "fees": [{"label": "Annual fee", "amount": 0},
                                       {"label": "Late fee", "amount": 95}],  # belongs to another card
                              "max_amount": 5000}, EVIDENCE)  # invented
    assert [r["value"] for r in product["rates"]] == [19.9]
    assert [f["amount"] for f in product["fees"]] == [0]
    assert product["max_amount"] is None
    assert product["unverified"] == ["Cash APR: 29.9%", "Late fee: 95", "max amount: 5000"]
    assert product["confidence"] == 0.63  # 0.9 * (0.5 + 0.5 * 2 verified / 5 checked)


def test_unknown_product_name_is_penalised():
    product = ground_product({"name": "Diamond Elite Card", "confidence": 0.9, "rates": [{"value": 19.9}]}, EVIDENCE)
    assert "product name not found on the page" in product["unverified"]
    assert product["confidence"] == round(0.9 * 0.7, 3)


def test_name_matching_tolerates_small_differences():
    assert EVIDENCE.context_for("Platinum Rewards Card") is not None
    assert "24.9%" in EVIDENCE.context_for("Platinum Rewards Card")


def test_merge_adds_missed_products_but_not_listings():
    llm = [{"name": "Platinum Rewards Card", "extraction_method": "llm"}]
    rules = [
        {"name": "Platinum Rewards Credit Card", "extraction_method": "rules"},  # same product
        {"name": "Basic Card", "extraction_method": "table"},
        {"name": "Our savings accounts", "extraction_method": "rules"},  # generic listing heading
    ]
    assert merge_missed_products(llm, rules) == 1
    assert [p["name"] for p in llm] == ["Platinum Rewards Card", "Basic Card"]
