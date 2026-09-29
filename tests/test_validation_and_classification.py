from app.ai.classification import ProductClassifier, classify_by_keywords
from app.validation.schemas import DataType, ExtractedProduct, ProductCategory, ScrapeRequest, validate_products

import pytest
from pydantic import ValidationError

SOURCE = "https://bank.example/cards"


def test_product_coercion():
    product = ExtractedProduct.model_validate({
        "name": "  Platinum   Card ",
        "category": "credit_card",
        "rates": ["24.9%", {"label": "Cash APR", "min": "29.9", "max": "27.9"}, {"label": "Bad", "value": "n/a"}],
        "fees": [{"label": "Annual fee", "amount": "£95", "currency": "£"}, {"label": "Empty"}],
        "min_amount": "$1,000", "max_amount": "25k",
        "features": ["Travel perks", "travel perks", ""],
        "attributes": [{"name": "Network", "value": "Visa"}],
        "confidence": "high",
        "source_url": SOURCE,
    })
    assert product.name == "Platinum Card"
    assert product.category == ProductCategory.CREDIT_CARD
    assert [r.display() for r in product.rates] == ["Rate: 24.9%", "Cash APR: 27.9% – 29.9%"]
    assert product.fees[0].currency == "GBP" and product.fees[0].amount == 95 and len(product.fees) == 1
    assert (product.min_amount, product.max_amount) == (1000, 25000)
    assert product.features == ["Travel perks"]
    assert product.attributes == {"Network": "Visa"}
    assert product.confidence == 0.9


def test_validate_products_rejects_and_dedupes():
    raw = [
        {"name": "Saver", "confidence": 0.9, "rates": [{"value": 4.1}]},
        {"name": "saver", "confidence": 0.5},
        {"name": "Low", "confidence": 0.1},
        {"confidence": 0.9},
        "not a product",
    ]
    valid, rejected = validate_products(raw, source_url=SOURCE, min_confidence=0.3, data_types=set(DataType))
    assert [p.name for p in valid] == ["Saver"]
    reasons = [r.reason for r in rejected]
    assert any("duplicate" in r for r in reasons)
    assert any("below threshold" in r for r in reasons)
    assert any("schema validation" in r for r in reasons)
    assert len(rejected) == 4


def test_restrict_to_requested_data():
    raw = [{"name": "Saver", "confidence": 0.9, "rates": [{"value": 4.1}], "fees": [{"label": "Exit", "amount": 10}],
            "eligibility": ["UK resident"]}]
    valid, _ = validate_products(raw, source_url=SOURCE, min_confidence=0.3, data_types={DataType.RATES})
    assert valid[0].rates and not valid[0].fees and not valid[0].eligibility


def test_scrape_request_blocks_private_hosts():
    with pytest.raises(ValidationError):
        ScrapeRequest(url="http://127.0.0.1/admin")
    with pytest.raises(ValidationError):
        ScrapeRequest(url="http://localhost:8000")
    assert ScrapeRequest(url="https://bank.example/cards").data_types == list(DataType)


@pytest.mark.parametrize("name, text, expected", [
    ("Platinum Rewards Credit Card", "balance transfer offer", ProductCategory.CREDIT_CARD),
    ("Personal Loan", "borrow up to 25,000 with fixed repayments", ProductCategory.LOAN),
    ("Home Insurance", "buildings and contents cover with a £100 excess", ProductCategory.INSURANCE),
    ("Global Equity Fund", "invest in a diversified portfolio of shares", ProductCategory.INVESTMENT),
    ("Easy Access Savings Account", "4.1% AER", ProductCategory.BANKING),
    ("Pay in 3", "buy now pay later at checkout", ProductCategory.PAYMENT),
])
def test_keyword_classification(name, text, expected):
    assert classify_by_keywords(name, text).category == expected


def test_prior_category_from_extractor():
    classifier = ProductClassifier()
    agree = classifier.classify({"name": "Gold Credit Card", "category": "Credit Card", "category_confidence": 0.9,
                                 "extraction_method": "llm"})
    assert agree.category == ProductCategory.CREDIT_CARD and agree.method == "llm+keywords"
    disagree = classifier.classify({"name": "Gold Personal Loan", "category": "Insurance",
                                    "category_confidence": 0.8, "extraction_method": "llm"})
    assert disagree.category == ProductCategory.INSURANCE and disagree.confidence == 0.68
