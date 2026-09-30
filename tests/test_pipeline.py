import asyncio

from fastapi.testclient import TestClient

from app.database.database import SessionLocal, init_db
from app.database.models import ScrapeJob
from app.main import app
from app.pipeline import run_job
from app.reports.csv_report import build_csv
from app.reports.pdf_report import build_pdf
from app.scraper.http_scraper import HttpScraper
from app.validation.schemas import ScrapeRequest
from tests.fixtures import ROBOTS_BLOCK_BOT, TERMS_PROHIBITED, make_transport

URL = "https://bank.example/cards"


class FakeLLM:
    """Stands in for LLMClient: answers each pipeline prompt with canned JSON and records the prompts."""

    enabled = True
    model_name = "fake:model"
    max_input_chars = 15_000

    def __init__(self, _settings=None):
        self.prompts: list[str] = []

    async def complete_json(self, system: str, user: str):
        self.prompts.append(user)
        if user.startswith("Analyse this web page"):
            return {"page_type": "listing", "publisher": "Example Bank", "topic": "Credit cards"}
        if user.startswith("Extract the distinct items"):
            # Mimics real small-model mistakes: the table cards are missed, and the Platinum card's £95 fee
            # is attributed to the Cashback card.
            return {"items": [
                {
                    "name": "Platinum Rewards Credit Card", "type": "credit card", "confidence": 1.0,
                    "attributes": {"Representative APR": "24.9% variable", "Annual fee": "£95",
                                   "Perk": "Earn 2x points on travel"},
                    "source_snippet": "Representative 24.9% APR variable. Annual fee £95.",
                },
                {
                    "name": "Example Cashback Card", "type": "credit card", "confidence": 1.0,
                    "attributes": {"APR": "22.9%", "Annual fee": "£95", "Perk": "Earn 2x points on travel",
                                   "Cashback": "Earn 1% cashback on every purchase"},
                },
            ]}
        if user.startswith("For each keyword below"):
            return {"findings": [{
                "keyword": "annual fee",
                "summary": "The Platinum Rewards card has a £95 annual fee. A £12 fee applies to all cards.",
                "points": ["Platinum Rewards: annual fee £95", "Gold Card: annual fee £50", "Invented fee £12"],
            }]}
        if user.startswith("Give detailed information"):
            return {
                "detailed_summary": "Example Bank compares its credit cards. The Platinum Rewards card charges £95 a "
                                    "year. A 12.34% cashback bonus applies.\n\nThe Gold Card has a 21.9% APR.",
                "sections": [
                    {"title": "Platinum Rewards", "summary": "Representative 24.9% APR variable. Annual fee £95."},
                    {"title": "Invented section", "summary": "Pays 12.34% cashback."},
                ],
                "key_figures": [
                    {"label": "Platinum Rewards APR", "value": "24.9%", "context": "representative, variable"},
                    {"label": "Invented fee", "value": "£9,876"},
                    {"label": "missing value"},
                ],
            }
        if user.startswith("Write a report"):
            return {
                "headline": "Example Bank's credit card range",
                "overview": "Example Bank's credit card page compares three cards, from a no-fee Basic Card to a "
                            "Platinum Rewards card with a £95 annual fee.",
                "audience": "UK consumers choosing a credit card",
                "key_points": ["Three cards compared", "APRs from 19.9% to 24.9%", "Balance transfer fee of 3%"],
                "things_to_watch": ["The Platinum APR is variable"],
                "data_gaps": ["No eligibility criteria are stated"],
            }
        return None

    async def aclose(self):
        pass


def _run(llm=None, keywords=(), **transport_kwargs) -> ScrapeJob:
    init_db()
    with SessionLocal() as db:
        request = ScrapeRequest(url=URL, keywords=list(keywords))
        job = ScrapeJob(url=URL, request=request.model_dump(mode="json"), status="queued", events=[])
        db.add(job)
        db.commit()
        job_id = job.id

    transport = make_transport(**transport_kwargs)
    asyncio.run(run_job(
        job_id,
        scraper_factory=lambda settings: HttpScraper(settings, transport=transport),
        llm_factory=(lambda settings: llm) if llm else None,
    ))

    db = SessionLocal()
    return db.get(ScrapeJob, job_id)


def test_pipeline_completes_and_stores_items():
    job = _run()
    assert job.status == "completed", job.error

    checks = {c["name"]: c["status"] for c in job.compliance["checks"]}
    assert checks["robots_txt"] == "pass"
    assert checks["terms_of_service"] == "pass"
    assert checks["page_access"] == "pass"
    assert checks["content_license"] == "warn"  # "All rights reserved"
    assert checks["personal_data"] == "warn"  # contact email/phone on the page

    items = {i.name: i for i in job.items}
    assert {"Example Cashback Card", "Basic Card", "Gold Card"} <= set(items)
    assert items["Example Cashback Card"].type == "credit card"  # from the JSON-LD @type
    assert items["Gold Card"].type == "card"  # from the table's name column
    assert items["Gold Card"].data["attributes"] == {"Purchase APR": "21.9%", "Annual fee": "£50"}
    assert all(i.references and i.references[0].url == URL for i in job.items)

    # PII redacted before storage
    assert "help@example-bank.test" not in (job.extraction.text or "")
    assert "<EMAIL_ADDRESS>" in job.extraction.text

    stages = [e["stage"] for e in job.events]
    for stage in ("compliance", "router", "extraction", "cleaning", "understanding", "validation", "storage",
                  "report", "done"):
        assert stage in stages
    assert "keywords" not in stages

    # Without an LLM the report is a clearly labelled extractive summary
    report = job.extraction.page_report
    assert report["method"] == "basic" and report["model"] is None
    assert "Compare Example Bank credit cards" in report["overview"]
    assert "Gold Card — card — Purchase APR: 21.9%" in report["items_overview"]
    assert report["sections"] and all(s["title"] and s["summary"] for s in report["sections"])
    assert any(f["value"] == "21.9%" for f in report["key_figures"])
    assert report["keyword_findings"] == []

    csv_text = build_csv(job.items).decode("utf-8-sig")
    assert "Gold Card" in csv_text and "Purchase APR: 21.9%" in csv_text
    assert build_pdf(job, job.items).startswith(b"%PDF")


def test_keywords_come_first_without_an_llm():
    job = _run(keywords=["annual fees", "travel", "mortgage"])
    assert job.status == "completed", job.error
    findings = {f["keyword"]: f for f in job.extraction.page_report["keyword_findings"]}
    assert list(findings) == ["annual fees", "travel", "mortgage"]

    fee = findings["annual fees"]
    assert fee["found"] and fee["method"] == "extract"
    assert {"Basic Card", "Gold Card"} <= set(fee["items"])
    assert any(p["match"] == "exact" and "Annual fee £95" in p["text"] for p in fee["passages"])
    assert fee["points"][0] == "Platinum Rewards Credit Card: Annual fee £95."
    assert "Card: Gold Card | Purchase APR: 21.9% | Annual fee: £50" in fee["points"]

    assert findings["travel"]["found"] and findings["travel"]["items"] == []
    assert not findings["mortgage"]["found"] and findings["mortgage"]["passages"] == []

    tagged = [i.name for i in job.items if i.data["matched_keywords"]]
    assert set(tagged) == {"Basic Card", "Gold Card"}
    assert [i.data["matched_keywords"] for i in job.items][:2] == [["annual fees"], ["annual fees"]]
    assert any(e["stage"] == "keywords" and "'mortgage'" in e["message"] and e["level"] == "warn"
               for e in job.events)
    assert b"%PDF" in build_pdf(job, job.items)[:8]


def test_pipeline_with_llm_grounds_items_and_keyword_findings():
    llm = FakeLLM()
    job = _run(llm=llm, keywords=["annual fee"])
    assert job.status == "completed", job.error

    items = {i.name: i for i in job.items}
    platinum = items["Platinum Rewards Credit Card"]
    assert platinum.extraction_method == "llm"
    assert platinum.data["attributes"]["Annual fee"] == "£95"  # correctly attributed, kept
    assert platinum.confidence == 0.95  # LLM self-reported 1.0 is capped

    # items the LLM missed are recovered from the source's table
    assert items["Basic Card"].extraction_method == "table"
    assert items["Gold Card"].extraction_method == "table"
    # the misattributed fee and perk are removed and recorded
    cashback = items["Example Cashback Card"]
    assert cashback.data["attributes"] == {"APR": "22.9%", "Cashback": "Earn 1% cashback on every purchase"}
    assert cashback.data["unverified"] == ["Annual fee: £95", "Perk: Earn 2x points on travel"]
    assert any("Removed 2 fact(s)" in e["message"] for e in job.events)

    extraction_prompt = next(p for p in llm.prompts if p.startswith("Extract the distinct items"))
    assert "The user is especially interested in: annual fee" in extraction_prompt

    report = job.extraction.page_report
    assert report["method"] == "llm" and report["model"] == "fake:model"
    assert report["headline"] == "Example Bank's credit card range"
    assert report["key_points"] == ["Three cards compared", "APRs from 19.9% to 24.9%"]  # invented 3% dropped

    finding = report["keyword_findings"][0]
    assert finding["keyword"] == "annual fee" and finding["method"] == "llm"
    assert finding["summary"] == "The Platinum Rewards card has a £95 annual fee."  # invented £12 sentence dropped
    assert finding["points"] == ["Platinum Rewards: annual fee £95", "Gold Card: annual fee £50"]
    keyword_prompt = next(p for p in llm.prompts if p.startswith("For each keyword below"))
    assert "KEYWORD: annual fee" in keyword_prompt and "Annual fee £95" in keyword_prompt

    # Detailed information: invented figures are removed; supported ones stay
    assert report["detailed_summary"] == ("Example Bank compares its credit cards. The Platinum Rewards card charges "
                                          "£95 a year.\n\nThe Gold Card has a 21.9% APR.")
    assert report["key_figures"] == [{"label": "Platinum Rewards APR", "value": "24.9%",
                                      "context": "representative, variable"}]

    report_prompt = next(p for p in llm.prompts if p.startswith("Write a report"))
    assert "Compare our everyday cards" in report_prompt  # page content
    assert '"Representative APR"' in report_prompt  # validated item JSON
    assert "help@example-bank.test" not in report_prompt  # PII redacted before the LLM
    assert "We use cookies" not in report_prompt  # cleaned
    assert build_pdf(job, job.items).startswith(b"%PDF")


def test_pipeline_blocked_by_robots():
    job = _run(robots=ROBOTS_BLOCK_BOT)
    assert job.status == "blocked"
    assert "robots_txt" in job.error
    assert job.items == []


def test_pipeline_blocked_by_terms():
    job = _run(terms=TERMS_PROHIBITED)
    assert job.status == "blocked"
    assert "terms_of_service" in job.error


def test_pipeline_blocked_on_access_denied():
    job = _run(cards_status=403)
    assert job.status == "blocked"
    assert "page_access" in job.error


def test_api_endpoints():
    _run()
    with TestClient(app) as client:
        health = client.get("/api/health").json()
        assert health["status"] == "ok" and health["llm"]["enabled"] is False
        jobs = client.get("/api/jobs").json()
        assert jobs and jobs[0]["url"] == URL and jobs[0]["item_count"] >= 3
        detail = client.get(f"/api/jobs/{jobs[0]['id']}").json()
        assert "compliance" in detail and "events" in detail and detail["items"]
        assert detail["page_report"]["overview"]
        stats = client.get("/api/stats").json()
        assert stats["items"] >= 3 and stats["items_by_type"].get("card", 0) >= 2
        assert client.get("/api/items", params={"type": "card"}).json()
        assert client.get(f"/api/jobs/{jobs[0]['id']}/report/pdf").headers["content-type"] == "application/pdf"
        assert client.post("/api/jobs", json={"url": "http://10.0.0.1/"}).status_code == 422

        bad = client.post("/api/jobs", json={"url": URL, "keywords": ["someone@example.com"]})
        assert bad.status_code == 422 and "personal identifier" in bad.text
