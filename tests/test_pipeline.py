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
            return {"page_type": "product_listing", "institution": "Example Bank", "topic": "Credit cards"}
        if user.startswith("Extract every distinct"):
            # Mimics real qwen2.5:7b mistakes: the table cards are missed, and the Platinum card's £95 fee
            # is attributed to the Cashback card.
            return {"products": [
                {
                    "name": "Platinum Rewards Credit Card", "category": "Credit Card", "category_confidence": 0.95,
                    "rates": [{"label": "Representative APR", "value": 24.9, "period": "variable"}],
                    "fees": [{"label": "Annual fee", "amount": 95, "currency": "GBP"}],
                    "features": ["Earn 2x points on travel"], "confidence": 1.0,
                    "source_snippet": "Representative 24.9% APR variable. Annual fee £95.",
                },
                {
                    "name": "Example Cashback Card", "category": "Credit Card", "confidence": 1.0,
                    "rates": [{"label": "Representative APR", "value": 22.9}],
                    "fees": [{"label": "Annual fee", "amount": 95, "currency": "GBP"}],
                    "features": ["Earn 1% cashback on every purchase", "Earn 2x points on travel"],
                },
            ]}
        if user.startswith("Summarise each product"):
            return {"summaries": [{"index": 0, "summary": "A travel rewards card with a £95 annual fee.",
                                   "fees_and_rates": ["Representative APR: 24.9% (variable)"]}]}
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
                "key_points": ["Three cards compared", "APRs from 19.9% to 24.9%"],
                "rates_and_fees": ["Platinum Rewards: 24.9% APR variable", "Platinum Rewards: £95 annual fee",
                                   "Balance transfer fee of 3%"],  # not on the page
                "things_to_watch": ["The Platinum APR is variable"],
                "data_gaps": ["No eligibility criteria are stated"],
            }
        return None

    async def aclose(self):
        pass


def _run(llm=None, **transport_kwargs) -> ScrapeJob:
    init_db()
    with SessionLocal() as db:
        request = ScrapeRequest(url=URL)
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


def test_pipeline_completes_and_stores_products():
    job = _run()
    assert job.status == "completed", job.error

    checks = {c["name"]: c["status"] for c in job.compliance["checks"]}
    assert checks["robots_txt"] == "pass"
    assert checks["terms_of_service"] == "pass"
    assert checks["page_access"] == "pass"
    assert checks["content_license"] == "warn"  # "All rights reserved"
    assert checks["personal_data"] == "warn"  # contact email/phone on the page

    names = {p.name for p in job.products}
    assert {"Example Cashback Card", "Basic Card", "Gold Card", "Platinum Rewards Credit Card"} <= names
    assert all(p.category == "Credit Card" for p in job.products)
    assert all(p.references and p.references[0].url == URL for p in job.products)
    assert all(p.summary and p.summary["summary"] for p in job.products)

    gold = next(p for p in job.products if p.name == "Gold Card")
    assert gold.data["rates"][0]["value"] == 21.9
    assert gold.data["fees"][0]["amount"] == 50

    # PII redacted before storage
    assert "help@example-bank.test" not in (job.extraction.text or "")
    assert "<EMAIL_ADDRESS>" in job.extraction.text

    stages = [e["stage"] for e in job.events]
    for stage in ("compliance", "router", "extraction", "cleaning", "understanding", "classification",
                  "validation", "storage", "summarization", "report", "done"):
        assert stage in stages

    # Without an LLM the page report is a clearly labelled extractive summary
    report = job.extraction.page_report
    assert report["method"] == "basic" and report["model"] is None
    assert "Compare Example Bank credit cards" in report["overview"]
    assert any(line.startswith("Gold Card") for line in report["products_overview"])
    assert any("Gold Card: Purchase APR: 21.9%" == line for line in report["rates_and_fees"])
    # detailed information comes straight from the page's sections and figures
    assert report["sections"] and all(s["title"] and s["summary"] for s in report["sections"])
    assert any(f["value"] == "21.9%" for f in report["key_figures"])
    assert any(e["stage"] == "report" and e["level"] == "warn" for e in job.events)

    csv_text = build_csv(job.products).decode("utf-8-sig")
    assert "Gold Card" in csv_text and "Purchase APR: 21.9%" in csv_text
    assert build_pdf(job, job.products).startswith(b"%PDF")


def test_pipeline_with_llm_writes_page_report():
    llm = FakeLLM()
    job = _run(llm=llm)
    assert job.status == "completed", job.error

    product = job.products[0]
    assert product.extraction_method == "llm"
    assert product.summary["method"] == "llm"
    assert product.data["fees"][0]["amount"] == 95  # correctly attributed, kept
    assert product.confidence == 0.95  # LLM self-reported 1.0 is capped

    products = {p.name: p for p in job.products}
    # products the LLM missed are recovered from the parser
    assert products["Basic Card"].extraction_method == "table"
    assert products["Gold Card"].extraction_method == "table"
    # the misattributed fee is removed and recorded
    cashback = products["Example Cashback Card"]
    assert cashback.data["fees"] == []
    assert cashback.data["unverified"] == ["Annual fee: 95", "feature: Earn 2x points on travel"]
    assert cashback.data["features"] == ["Earn 1% cashback on every purchase"]
    assert cashback.data["rates"][0]["value"] == 22.9  # supported by the JSON-LD
    assert products["Platinum Rewards Credit Card"].data["features"] == ["Earn 2x points on travel"]
    assert any("Removed 2 fact(s)" in e["message"] for e in job.events)

    report = job.extraction.page_report
    assert report["method"] == "llm" and report["model"] == "fake:model"
    assert report["headline"] == "Example Bank's credit card range"
    assert report["things_to_watch"] == ["The Platinum APR is variable"]
    assert "Balance transfer fee of 3%" not in report["rates_and_fees"]
    assert len(report["rates_and_fees"]) == 2

    # Detailed information: invented figures are removed; supported ones stay
    assert report["detailed_summary"] == ("Example Bank compares its credit cards. The Platinum Rewards card charges "
                                          "£95 a year.\n\nThe Gold Card has a 21.9% APR.")
    assert report["sections"] == [{"title": "Platinum Rewards",
                                   "summary": "Representative 24.9% APR variable. Annual fee £95."}]
    assert report["key_figures"] == [{"label": "Platinum Rewards APR", "value": "24.9%",
                                      "context": "representative, variable"}]

    # The report prompt carries both the cleaned page content and the validated products
    report_prompt = next(p for p in llm.prompts if p.startswith("Write a report"))
    assert "Compare our everyday cards" in report_prompt  # page content
    assert '"Representative APR"' in report_prompt  # validated product JSON
    assert "help@example-bank.test" not in report_prompt  # PII redacted before the LLM
    assert "We use cookies" not in report_prompt  # cleaned

    pdf = build_pdf(job, job.products)
    assert pdf.startswith(b"%PDF")


def test_pipeline_blocked_by_robots():
    job = _run(robots=ROBOTS_BLOCK_BOT)
    assert job.status == "blocked"
    assert "robots_txt" in job.error
    assert job.products == []


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
        assert jobs and jobs[0]["url"] == URL
        detail = client.get(f"/api/jobs/{jobs[0]['id']}").json()
        assert "compliance" in detail and "events" in detail
        assert detail["page_report"]["overview"]
        stats = client.get("/api/stats").json()
        assert stats["products_by_category"].get("Credit Card", 0) >= 4
        products = client.get("/api/products", params={"category": "Credit Card"}).json()
        assert products
        assert client.get(f"/api/jobs/{jobs[0]['id']}/report/pdf").headers["content-type"] == "application/pdf"
        assert client.post("/api/jobs", json={"url": "http://10.0.0.1/"}).status_code == 422
