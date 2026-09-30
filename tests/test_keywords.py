import asyncio

from app.ai.keywords import find_passages, keyword_pattern, tag_items
from app.ai.rag import RagConfig
from app.documents.loader import load_document
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


def test_exact_and_related_passages(tmp_path):
    path = tmp_path / "guide.md"
    path.write_text(
        "# Store guide\n\n## Returns\n\nOur refund policy gives you 30 days to return any item.\n\n"
        "## Shipping\n\nOrders ship within two days. Express shipping costs $9.\n\n"
        "## Policy on returns for sale items\n\nSale items can be exchanged but not returned.\n",
        encoding="utf-8",
    )
    page = load_document(path)
    found = asyncio.run(find_passages(page, ["refund policy", "express shipping", "gift cards"], TFIDF))

    refund = found["refund policy"]
    assert refund[0].match == "exact" and "30 days" in refund[0].text
    assert any(p.match == "related" and "Sale items" in p.text for p in refund)  # shares "policy"
    assert [p.match for p in found["express shipping"]][0] == "exact"
    assert found["gift cards"] == []


def test_sample_document_keywords():
    from app.config import PROJECT_ROOT

    page = load_document(PROJECT_ROOT / "sample_docs" / "northwind-cd-rates.md")
    found = asyncio.run(find_passages(page, ["early withdrawal penalty"], TFIDF))
    assert found["early withdrawal penalty"] and found["early withdrawal penalty"][0].match == "exact"
