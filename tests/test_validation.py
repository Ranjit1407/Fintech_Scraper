import pytest
from pydantic import ValidationError

from app.validation.schemas import DataType, ExtractedItem, ScrapeRequest, validate_items

SOURCE = "https://shop.example/laptops"


def test_item_coercion():
    item = ExtractedItem.model_validate({
        "name": "  Aero   14 Laptop ",
        "type": "LaptopComputer",
        "attributes": [{"label": "Price", "value": "$1,299"}, {"name": "Weight", "value": ["1.2 kg", "light"]}],
        "confidence": "high",
        "source_url": SOURCE,
    })
    assert item.name == "Aero 14 Laptop"
    assert item.type == "laptop computer"
    assert item.attributes == {"Price": "$1,299", "Weight": "1.2 kg; light"}
    assert item.confidence == 0.9


def test_validate_items_rejects_and_dedupes():
    raw = [
        {"name": "Aero 14", "confidence": 0.9},
        {"name": "aero 14", "confidence": 0.5},
        {"name": "Low", "confidence": 0.1},
        {"confidence": 0.9},
        "not an item",
    ]
    valid, rejected = validate_items(raw, source_url=SOURCE, min_confidence=0.3)
    assert [i.name for i in valid] == ["Aero 14"]
    reasons = [r.reason for r in rejected]
    assert any("duplicate" in r for r in reasons)
    assert any("below threshold" in r for r in reasons)
    assert any("schema validation" in r for r in reasons)
    assert len(rejected) == 4


def test_scrape_request_blocks_private_hosts():
    with pytest.raises(ValidationError):
        ScrapeRequest(url="http://127.0.0.1/admin")
    with pytest.raises(ValidationError):
        ScrapeRequest(url="http://localhost:8000")
    assert ScrapeRequest(url=SOURCE).data_types == list(DataType)


def test_keywords_are_cleaned():
    request = ScrapeRequest(url=SOURCE, keywords="battery life, Price ,price;  2024-2026 models,,")
    assert request.keywords == ["battery life", "Price", "2024-2026 models"]
    assert ScrapeRequest(url=SOURCE).keywords == []


@pytest.mark.parametrize("keyword", ["jane.doe@example.com", "+44 20 7946 0958", "123-45-6789 SSN"])
def test_keywords_must_not_be_personal_identifiers(keyword):
    with pytest.raises(ValidationError, match="personal identifier"):
        ScrapeRequest(url=SOURCE, keywords=[keyword])


def test_keyword_limits():
    with pytest.raises(ValidationError, match="at most 10"):
        ScrapeRequest(url=SOURCE, keywords=[f"topic {n}" for n in range(11)])
    with pytest.raises(ValidationError, match="too long"):
        ScrapeRequest(url=SOURCE, keywords=["x" * 61])
