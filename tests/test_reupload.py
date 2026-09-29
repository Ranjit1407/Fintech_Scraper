from fastapi.testclient import TestClient

from app.database.database import SessionLocal
from app.database.models import ScrapeJob
from app.main import app
from app.validation.merge import merge_product, merge_reports
from app.validation.schemas import ExtractedProduct, PageReport

V1 = b"""# Zeta Bank CD Rates

| CD term | APY | Minimum deposit |
| --- | --- | --- |
| Zeta 6-Month CD | 4.00% | $500 |
| Zeta 12-Month CD | 4.35% | $1,000 |
"""
V2 = b"""# Zeta Bank CD Rates

| CD term | APY | Minimum deposit |
| --- | --- | --- |
| Zeta 12-Month CD | 4.50% | $1,000 |
| Zeta 36-Month CD | 3.80% | $1,000 |
"""


def _upload_and_process(client, content, name="reupload/zeta-rates.md"):
    batch = client.post("/api/uploads", files=[("files", (name, content, "text/markdown"))]).json()
    jobs = client.post(f"/api/uploads/{batch['batch_id']}/process", json={}).json()
    return batch, jobs


def _products(client, job_id):
    detail = client.get(f"/api/jobs/{job_id}").json()
    return detail, {p["name"]: p for p in detail["products"]}


def test_reupload_asks_and_merges_into_the_existing_job():
    with TestClient(app) as client:
        batch, jobs = _upload_and_process(client, V1)
        assert batch["files"][0]["previous"] is None
        job_id = jobs[0]["id"]
        detail, products = _products(client, job_id)
        assert detail["status"] == "completed", detail["error"]
        assert {"Zeta 6-Month CD", "Zeta 12-Month CD"} <= set(products)

        # identical re-upload: flagged, then processed into the same job without duplicates
        batch, jobs = _upload_and_process(client, V1)
        previous = batch["files"][0]["previous"]
        assert previous["job_id"] == job_id and previous["match"] == "identical"
        assert [j["id"] for j in jobs] == [job_id]
        detail, again = _products(client, job_id)
        assert detail["status"] == "completed" and detail["updated_at"]
        assert sorted(again) == sorted(products)
        assert len(again["Zeta 12-Month CD"]["references"]) == 2
        assert any(e["stage"] == "update" for e in detail["events"])

        # new version with the same name: changed rate replaced, new product added, old one kept
        batch, jobs = _upload_and_process(client, V2)
        assert batch["files"][0]["previous"]["match"] == "same_name"
        assert [j["id"] for j in jobs] == [job_id]
        detail, merged = _products(client, job_id)
        assert {"Zeta 6-Month CD", "Zeta 12-Month CD", "Zeta 36-Month CD"} <= set(merged)
        twelve = [r["value"] for r in merged["Zeta 12-Month CD"]["data"]["rates"]]
        assert 4.5 in twelve and 4.35 not in twelve
        assert len(merged["Zeta 12-Month CD"]["references"]) == 3
        assert len(detail["request"]["previous_files"]) == 2
        assert any("merged with new facts" in e["message"] for e in detail["events"])

        with SessionLocal() as db:
            same_doc = [j for j in db.query(ScrapeJob).all()
                        if (j.request or {}).get("original_name") == "reupload/zeta-rates.md"]
            assert len(same_doc) == 1


def test_reupload_refused_while_the_document_is_processing():
    content = V1.replace(b"Zeta", b"Omega")
    with TestClient(app) as client:
        _, jobs = _upload_and_process(client, content, name="omega.md")
        with SessionLocal() as db:
            db.get(ScrapeJob, jobs[0]["id"]).status = "running"
            db.commit()
        batch = client.post("/api/uploads", files=[("files", ("omega.md", content, "text/markdown"))]).json()
        response = client.post(f"/api/uploads/{batch['batch_id']}/process", json={})
        assert response.status_code == 409 and "still being processed" in response.json()["detail"]


def test_merge_rules():
    old = ExtractedProduct(name="Card", source_url="https://a.example/",
                           rates=[{"label": "APR", "value": 20.0}, {"label": "Cash APR", "value": 25.0}],
                           features=["Cashback"], tenure="12 months")
    new = ExtractedProduct(name="Card", source_url="https://a.example/v2",
                           rates=[{"label": "apr", "value": 18.0}], features=["cashback", "Lounge access"])
    merged = merge_product(old, new)
    assert [(r.label, r.value) for r in merged.rates] == [("Cash APR", 25.0), ("apr", 18.0)]
    assert merged.features == ["Cashback", "Lounge access"] and merged.tenure == "12 months"
    assert str(merged.source_url) == "https://a.example/v2"

    report = merge_reports(
        PageReport(headline="Old", key_points=["A"], key_figures=[{"label": "PAT", "value": "Rs10bn"}],
                   sections=[{"title": "Intro", "summary": "Old intro."}]),
        PageReport(headline="New", key_points=["B", "a"], key_figures=[{"label": "pat", "value": "Rs12bn"}],
                   sections=[{"title": "Outlook", "summary": "New outlook."}]),
    )
    assert report.headline == "New" and report.key_points == ["B", "a"]
    assert [f.value for f in report.key_figures] == ["Rs12bn"]
    assert [s.title for s in report.sections] == ["Outlook", "Intro"]
