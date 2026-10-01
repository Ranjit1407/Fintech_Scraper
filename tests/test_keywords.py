import asyncio

from app.ai.keywords import find_passages, keyword_pattern, tag_items
from app.ai.rag import RagConfig
from app.scraper.parser import parse_html
from app.validation.schemas import ExtractedItem

TFIDF = RagConfig(embedding_provider="tfidf")


def test_pattern_matches_word_forms():
    assert keyword_pattern("companies").search("The company was founded")
    assert keyword_pattern("price").search("Prices start at $10")
    assert keyword_pattern("refund policy").search("Our refund policies are simple")
    assert not keyword_pattern("art").search("The party starts")  # short words match whole words only
    assert not keyword_pattern("cat").search("category")


def test_items_that_mention_a_keyword_come_first():
    items = [ExtractedItem(name=name, source_url="https://a.example/", attributes=attributes)
             for name, attributes in (("Basic", {"Delivery": "3 days"}), ("Pro", {"Warranty": "2 years"}),
                                      ("Max", {"Warranty": "5 years", "Delivery": "Next day"}))]
    tagged = tag_items(items, ["warranty", "delivery"])
    assert [i.name for i in tagged] == ["Basic", "Pro", "Max"]
    assert [i.matched_keywords for i in tagged] == [["delivery"], ["warranty"], ["warranty", "delivery"]]
    untagged = tag_items(items, ["battery"])
    assert all(i.matched_keywords == [] for i in untagged)


def test_exact_and_related_passages():
    page = parse_html(
        "<html><body><h1>Store guide</h1>"
        "<h2>Returns</h2><p>Our refund policy gives you 30 days to return any item.</p>"
        "<h2>Shipping</h2><p>Orders ship within two days. Express shipping costs $9.</p>"
        "<h2>Policy on returns for sale items</h2><p>Sale items can be exchanged but not returned.</p>"
        "</body></html>",
        "https://store.example/guide",
    )
    found = asyncio.run(find_passages(page, ["refund policy", "express shipping", "gift cards"], TFIDF))

    refund = found["refund policy"]
    assert refund[0].match == "exact" and "30 days" in refund[0].text
    assert any(p.match == "related" and "Sale items" in p.text for p in refund)  # shares "policy"
    assert [p.match for p in found["express shipping"]][0] == "exact"
    assert found["gift cards"] == []


