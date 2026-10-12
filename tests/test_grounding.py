from app.ai.extraction import merge_missed_items
from app.scraper.parser import parse_html
from app.validation.grounding import PageEvidence, ground_item, ground_report
from app.validation.schemas import PageReport
from tests.fixtures import CARDS_HTML

PAGE = parse_html(CARDS_HTML, "https://bank.example/cards")
EVIDENCE = PageEvidence.from_page(PAGE)


def test_supported_facts_are_kept():
    item = ground_item({"name": "Gold Card", "confidence": 0.8,
                        "attributes": {"Purchase APR": "21.9%", "Annual fee": "£50"}}, EVIDENCE)
    assert item["unverified"] == [] and item["confidence"] == 0.8


def test_invented_and_misattributed_facts_are_removed():
    item = ground_item({"name": "Basic Card", "confidence": 0.9,
                        "description": "A card with a £75 fee.",  # invented figure
                        "attributes": {"Purchase APR": "19.9%",
                                       "Cash APR": "29.9%",  # invented
                                       "Annual fee": "£0",
                                       "Late fee": "£95",  # belongs to another card
                                       "Network": "Visa Signature"}}, EVIDENCE)  # not in the source
    assert item["attributes"] == {"Purchase APR": "19.9%", "Annual fee": "£0"}
    assert item["description"] is None
    assert item["unverified"] == ["Cash APR: 29.9%", "Late fee: £95", "Network: Visa Signature",
                                  "description: A card with a £75 fee."]
    assert item["confidence"] == 0.63  # 0.9 * (0.5 + 0.5 * 2 verified / 5 checked)


def test_text_facts_need_word_overlap_with_the_item():
    item = ground_item({"name": "Platinum Rewards Credit Card", "confidence": 0.9,
                        "attributes": {"Perk": "Earn 2x points on travel", "Lounge": "Free airport lounge access"}},
                       EVIDENCE)
    assert item["attributes"] == {"Perk": "Earn 2x points on travel"}


def test_unknown_item_name_is_penalised():
    item = ground_item({"name": "Diamond Elite Card", "confidence": 0.9, "attributes": {"APR": "19.9%"}}, EVIDENCE)
    assert "name not found in the source" in item["unverified"]
    assert item["confidence"] == round(0.9 * 0.7, 3)


def test_name_matching_tolerates_small_differences():
    assert EVIDENCE.context_for("Platinum Rewards Card") is not None
    assert "24.9%" in EVIDENCE.context_for("Platinum Rewards Card")


def test_report_drops_names_and_figures_the_source_never_mentions():
    report = PageReport(
        headline="Example Bank credit cards",
        overview="Example Bank compares its credit cards. The Gold Card has a 21.9% APR. "
                 "The bank is owned by Barclays.",  # outside knowledge
        audience="Customers of Lloyds Bank",  # invented
        key_points=["The Gold Card has a 21.9% APR", "Cards are issued by Mastercard",  # invented name
                    "APRs average 22.2%"],  # calculated figure
        data_gaps=["No Mastercard or Visa network is stated"],  # describes what is missing: kept
    )
    grounded, removed = ground_report(report, EVIDENCE)
    assert grounded.headline == "Example Bank credit cards"
    assert grounded.overview == "Example Bank compares its credit cards. The Gold Card has a 21.9% APR."
    assert grounded.audience is None
    assert grounded.key_points == ["The Gold Card has a 21.9% APR"]
    assert grounded.data_gaps == report.data_gaps
    assert removed == 4


def test_unsupported_headline_is_emptied_for_the_caller_to_replace():
    grounded, _ = ground_report(PageReport(headline="Credit cards from Santander", overview="x"), EVIDENCE)
    assert grounded.headline == ""


def test_merge_adds_missed_items():
    llm = [{"name": "Platinum Rewards Card", "extraction_method": "llm"}]
    rules = [
        {"name": "Platinum Rewards Credit Card", "extraction_method": "json-ld"},  # same item
        {"name": "Basic Card", "extraction_method": "table"},
    ]
    assert merge_missed_items(llm, rules) == 1
    assert [i["name"] for i in llm] == ["Platinum Rewards Card", "Basic Card"]
