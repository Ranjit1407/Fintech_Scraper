import asyncio

import pytest
from fastapi.testclient import TestClient

from app.config import PROJECT_ROOT, get_settings
from app.database.database import SessionLocal, init_db
from app.database.models import ScrapeJob
from app.documents.loader import DocumentError, list_documents, load_document, resolve_folder, text_to_blocks
from app.documents.uploads import batch_folder
from app.main import app
from app.pipeline import run_job
from app.validation.schemas import DocumentJobRequest

SAMPLE_DOCS = PROJECT_ROOT / "sample_docs"


def _sections(page) -> dict[str, str]:
    return {s.heading: s.text for s in page.sections if s.heading}


def test_sample_folder_lists_every_format():
    files = {p.suffix for p in list_documents(SAMPLE_DOCS)}
    assert {".pdf", ".docx", ".md", ".txt"} <= files


def test_pdf_headings_come_from_font_sizes():
    page = load_document(SAMPLE_DOCS / "northwind-home-loans-key-facts.pdf")
    sections = _sections(page)
    assert "Interest rate: 6.125%, APR: 6.284%" in sections["30-Year Fixed Mortgage"]
    assert "5.375%" in sections["15-Year Fixed Mortgage"] and "6.125%" not in sections["15-Year Fixed Mortgage"]
    assert page.metadata["file_type"] == "pdf" and page.url.startswith("file:///")


def test_pdf_words_placed_separately_keep_their_spaces(tmp_path):
    """Many PDFs position each word instead of writing space characters."""
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas

    path = tmp_path / "note.pdf"
    pdf = canvas.Canvas(str(path), pagesize=LETTER)

    def words(y, font, size, items):
        text = pdf.beginText(72, y)
        text.setFont(font, size)
        for word in items:  # no space characters: each word is moved into place
            text.textOut(word)
            text.moveCursor(pdf.stringWidth(word, font, size) + size * 0.3, 0)
        pdf.drawText(text)

    words(720, "Helvetica-Bold", 18, ["Quarterly", "Bank", "Note"])
    words(690, "Helvetica", 10, ["Deposits", "grew", "1.9%", "quarter", "on", "quarter."])
    words(670, "Helvetica", 10, ["■", "Margins", "held", "steady."])
    pdf.save()

    page = load_document(path)
    assert page.title == "Quarterly Bank Note"
    assert "Deposits grew 1.9% quarter on quarter." in page.text
    assert any(b.kind == "list_item" and b.text == "Margins held steady." for b in page.blocks)


def test_docx_headings_lists_and_tables():
    page = load_document(SAMPLE_DOCS / "northwind-insurance-guide.docx")
    assert "Coverage from $100,000 to $2,000,000" in _sections(page)["Northwind Term Life 20"]
    assert any(b.kind == "list_item" for b in page.blocks)
    table = page.tables[0]
    assert table.columns == ["Product", "Coverage", "Premium", "Term"]
    assert table.rows[0]["Premium"] == "from $18 per month"


def test_markdown_table_and_text_caps_headings():
    md = load_document(SAMPLE_DOCS / "northwind-cd-rates.md")
    assert md.tables[0].rows[1] == {"CD term": "12-Month CD", "APY": "4.35%", "Minimum deposit": "$1,000",
                                    "Early withdrawal penalty": "90 days of interest"}
    txt = load_document(SAMPLE_DOCS / "northwind-pay-fees.txt")
    assert {"Fees", "Limits", "Eligibility"} <= set(_sections(txt))


def test_text_parsing_details():
    blocks, tables = text_to_blocks("Title\n=====\n\nSome intro\ncontinues here.\n\n* one\n2. two\n\n---\n")
    assert [(b.kind, b.text) for b in blocks] == [
        ("heading", "Title"), ("paragraph", "Some intro continues here."), ("list_item", "one"), ("list_item", "two"),
    ]
    assert tables == []


def test_folder_checks(tmp_path, monkeypatch):
    settings = get_settings()
    with pytest.raises(DocumentError, match="not found"):
        resolve_folder(str(tmp_path / "missing"), settings)
    monkeypatch.setattr(settings, "document_roots", [str(tmp_path)])
    with pytest.raises(DocumentError, match="DOCUMENT_ROOTS"):
        resolve_folder(str(SAMPLE_DOCS), settings)
    assert resolve_folder(str(tmp_path), settings) == tmp_path.resolve()


def test_empty_and_oversized_documents(tmp_path):
    (tmp_path / "empty.txt").write_text("   \n", encoding="utf-8")
    with pytest.raises(DocumentError, match="no extractable text"):
        load_document(tmp_path / "empty.txt")
    with pytest.raises(DocumentError, match="limit"):
        load_document(SAMPLE_DOCS / "northwind-insurance-guide.docx", max_bytes=100)


def _run_document(path) -> ScrapeJob:
    init_db()
    with SessionLocal() as db:
        request = DocumentJobRequest(document_path=str(path))
        job = ScrapeJob(url=path.as_uri(), request=request.model_dump(mode="json"), status="queued", events=[])
        db.add(job)
        db.commit()
        job_id = job.id
    asyncio.run(run_job(job_id))
    return SessionLocal().get(ScrapeJob, job_id)


def test_document_job_runs_the_pipeline():
    path = SAMPLE_DOCS / "northwind-cd-rates.md"
    job = _run_document(path)
    assert job.status == "completed", job.error
    assert job.fetch_method == "document"
    checks = {c["name"]: c["status"] for c in job.compliance["checks"]}
    assert checks == {"local_document": "pass", "personal_data": "pass"}
    names = {p.name for p in job.products}
    assert {"12-Month CD", "60-Month CD"} <= names
    twelve = next(p for p in job.products if p.name == "12-Month CD")
    assert twelve.references[0].url == path.resolve().as_uri()
    assert job.source.domain == "local-documents"


def test_document_job_redacts_personal_data():
    job = _run_document(SAMPLE_DOCS / "northwind-pay-fees.txt")
    assert job.status == "completed", job.error
    assert "pay-support@northwind-sample.test" not in (job.extraction.text or "")
    assert "<EMAIL_ADDRESS>" in job.extraction.text


def test_document_and_profile_endpoints():
    with TestClient(app) as client:
        response = client.post("/api/documents", json={"folder": str(SAMPLE_DOCS)})
        assert response.status_code == 202, response.text
        jobs = response.json()
        assert len(jobs) == 4 and all(j["url"].startswith("file:///") for j in jobs)
        detail = client.get(f"/api/jobs/{jobs[0]['id']}").json()
        assert detail["status"] == "completed"

        assert client.post("/api/documents", json={"folder": str(SAMPLE_DOCS / "nope")}).status_code == 422
        bad_profile = client.post("/api/documents", json={"folder": str(SAMPLE_DOCS), "llm_profile": "nope"})
        assert bad_profile.status_code == 422 and "Unknown LLM profile" in bad_profile.json()["detail"]

        listing = client.get("/api/llm/profiles").json()
        assert listing["default"] in {p["name"] for p in listing["profiles"]}
        assert all("api_key" not in p for p in listing["profiles"])


def _upload(client, *files):
    parts = [("files", (name, content, "application/octet-stream")) for name, content in files]
    return client.post("/api/uploads", files=parts)


def test_upload_review_then_process():
    md = (SAMPLE_DOCS / "northwind-cd-rates.md").read_bytes()
    txt = (SAMPLE_DOCS / "northwind-pay-fees.txt").read_bytes()
    with TestClient(app) as client:
        response = _upload(
            client,
            ("my docs/northwind-cd-rates.md", md),
            ("my docs/sub/northwind-pay-fees.txt", txt),
            ("my docs/../../escape.md", md + b"\nEscaped copy.\n"),  # traversal attempt is stored flat in the batch
            ("my docs/copy of rates.md", md),  # same content as another file in this upload
            ("my docs/sheet.xlsx", b"x"),
            ("my docs/.DS_Store", b"x"),
            ("my docs/empty.txt", b""),
        )
        assert response.status_code == 201, response.text
        batch = response.json()
        assert [f["name"] for f in batch["files"]] == [
            "my docs/northwind-cd-rates.md", "my docs/sub/northwind-pay-fees.txt", "my docs/../../escape.md",
        ]
        assert {s["reason"] for s in batch["skipped"]} == {"unsupported file type", "hidden or temporary file",
                                                           "empty file", "same content as my docs/northwind-cd-rates.md"}
        folder = get_settings().upload_dir
        stored = sorted(p.name for p in (__import__("pathlib").Path(folder) / batch["batch_id"]).iterdir()
                        if not p.name.startswith("."))
        assert stored == ["escape.md", "northwind-cd-rates.md", "sub__northwind-pay-fees.txt"]

        jobs = client.post(f"/api/uploads/{batch['batch_id']}/process", json={}).json()
        assert len(jobs) == 3
        details = [client.get(f"/api/jobs/{j['id']}").json() for j in jobs]
        assert all(d["status"] == "completed" for d in details)
        assert any(p["name"] == "12-Month CD" for d in details for p in d["products"])

        # processed uploads can be neither re-processed nor deleted
        assert client.post(f"/api/uploads/{batch['batch_id']}/process", json={}).status_code == 409
        assert client.delete(f"/api/uploads/{batch['batch_id']}").status_code == 409


def test_upload_cancel_and_errors():
    with TestClient(app) as client:
        batch = _upload(client, ("docs/a.md", b"# A card\n\n- APR 19.9%\n")).json()
        assert client.delete(f"/api/uploads/{batch['batch_id']}").status_code == 204
        assert client.post(f"/api/uploads/{batch['batch_id']}/process", json={}).status_code == 404
        assert client.post("/api/uploads/not-a-batch/process", json={}).status_code == 404
        for bad_id in ("../../app", "..", str(PROJECT_ROOT)):
            with pytest.raises(DocumentError):
                batch_folder(bad_id, get_settings())

        nothing = _upload(client, ("docs/sheet.xlsx", b"x"))
        assert nothing.status_code == 422 and "unsupported file type" in nothing.json()["detail"]


def test_private_hosts_opt_in(monkeypatch):
    with TestClient(app) as client:
        assert client.post("/api/compliance/check", json={"url": "http://127.0.0.1:8080/"}).status_code == 422
    from app.validation.schemas import ScrapeRequest

    monkeypatch.setattr(get_settings(), "allow_private_hosts", True)
    assert str(ScrapeRequest(url="http://127.0.0.1:8080/cards").url) == "http://127.0.0.1:8080/cards"
