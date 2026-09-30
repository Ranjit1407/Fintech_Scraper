import asyncio
import re

from app.ai.extraction import merge_part_items
from app.ai.rag import RagConfig, build_index, chunk_page, source_length, split_parts
from app.database.database import SessionLocal, init_db
from app.database.models import ScrapeJob
from app.documents.loader import load_document
from app.pipeline import run_job
from app.validation.schemas import DocumentJobRequest

SECTIONS = 60
FILLER = ("Saver {n} is designed for everyday savers who want easy access to their money while earning "
          "interest on every balance, with online and mobile banking included at no extra cost. "
          "Interest on Saver {n} is calculated daily and paid monthly into the account or another account. ")


def _long_document(tmp_path):
    lines = ["# Northwind Savings Guide", ""]
    for n in range(1, SECTIONS + 1):
        lines += [f"## Saver {n} Account", "", f"- {3 + n / 100:.2f}% APY", f"- Minimum deposit: ${n * 100}", "",
                  FILLER.format(n=n), ""]
    path = tmp_path / "long-guide.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def test_parts_and_chunks_cover_the_whole_source(tmp_path):
    page = load_document(_long_document(tmp_path))
    assert source_length(page) > 15_000
    parts = split_parts(page, 15_000)
    assert len(parts) > 1
    assert all(source_length(p) <= 15_000 for p in parts)
    seen = [b.text for p in parts for b in p.blocks]
    assert all(b.text in seen for b in page.blocks)  # nothing dropped (headings may repeat for context)

    chunks = chunk_page(page, 1500)
    assert all(len(c.text) <= 1500 for c in chunks)
    joined = "\n".join(c.text for c in chunks)
    assert f"{3 + SECTIONS / 100:.2f}% APY" in joined and "Minimum deposit: $4000" in joined


def test_tfidf_retrieval_finds_the_relevant_chunk(tmp_path):
    page = load_document(_long_document(tmp_path))
    index = asyncio.run(build_index(page, ["Saver 37 Account minimum deposit"], RagConfig(embedding_provider="tfidf")))
    assert index.method == "tfidf"
    best = index.retrieve(top_k=1, budget_chars=10_000)
    assert best and best[0].heading == "Saver 37 Account"


def test_items_split_across_parts_are_merged():
    merged = merge_part_items([
        [{"name": "Saver Account", "type": "other", "attributes": {"APY": "3.1%", "Access": "Online"},
          "confidence": 0.6, "extraction_method": "llm"}],
        [{"name": "Saver Account", "type": "savings account", "attributes": {"APY": "3.1%", "Monthly fee": "$5"},
          "description": "Easy access savings.", "confidence": 0.8, "extraction_method": "table"}],
    ])
    assert len(merged) == 1
    item = merged[0]
    assert item["attributes"] == {"APY": "3.1%", "Monthly fee": "$5", "Access": "Online"}
    assert item["type"] == "savings account" and item["description"] == "Easy access savings."
    assert item["confidence"] == 0.8 and item["extraction_method"] == "llm"


class LongDocLLM:
    enabled = True
    model_name = "fake:model"
    max_input_chars = 15_000

    def __init__(self):
        self.prompts: list[str] = []

    async def complete_json(self, system, user):
        self.prompts.append(user)
        if user.startswith("Analyse this web page"):
            return {"page_type": "listing", "publisher": "Northwind", "topic": "Savings accounts"}
        if user.startswith("Extract the distinct items"):
            items = []
            for n, rate in re.findall(r"# Saver (\d+) Account\n- (\d\.\d\d)% APY", user):
                items.append({"name": f"Saver {n} Account", "type": "savings account", "confidence": 0.9,
                              "attributes": {"APY": f"{rate}%"}})
            return {"items": items}
        if user.startswith("This is part"):
            part = re.search(r"part (\d+) of (\d+)", user).group(1)
            numbers = re.findall(r"# Saver (\d+) Account", user)
            return {"facts": [f"Part {part} describes Saver {numbers[0]} to Saver {numbers[-1]}"]}
        if user.startswith("Write a report"):
            return {"headline": "Northwind savings range", "overview": "A guide to Northwind savings accounts.",
                    "key_points": [f"Rates from 3.01% to {3 + SECTIONS / 100:.2f}% APY"]}
        return None

    async def aclose(self):
        pass


def test_long_document_is_read_in_full_with_retrieval(tmp_path, monkeypatch):
    import app.pipeline as pipeline

    monkeypatch.setattr(pipeline, "load_rag_config", lambda _settings: RagConfig(embedding_provider="tfidf"))
    path = _long_document(tmp_path)
    init_db()
    with SessionLocal() as db:
        job = ScrapeJob(url=path.as_uri(), request=DocumentJobRequest(document_path=str(path)).model_dump(mode="json"),
                        status="queued", events=[])
        db.add(job)
        db.commit()
        job_id = job.id
    llm = LongDocLLM()
    asyncio.run(run_job(job_id, llm_factory=lambda _settings: llm))

    job = SessionLocal().get(ScrapeJob, job_id)
    assert job.status == "completed", job.error
    rag = job.extraction.rag
    assert rag["parts"] > 1 and rag["chunks"] >= rag["parts"] and rag["embedding"] == "tfidf"

    # every part was sent for extraction and for notes, so items from the end of the document are found
    extraction_prompts = [p for p in llm.prompts if p.startswith("Extract the distinct items")]
    assert len(extraction_prompts) == rag["parts"]
    names = {i.name for i in job.items}
    assert {"Saver 1 Account", f"Saver {SECTIONS} Account"} <= names
    assert len([p for p in llm.prompts if p.startswith("This is part")]) == rag["parts"]

    report_prompt = next(p for p in llm.prompts if p.startswith("Write a report"))
    assert "NOTES FROM EVERY PART OF THE SOURCE" in report_prompt
    assert f"Part {rag['parts']} describes" in report_prompt and "MOST RELEVANT PASSAGES" in report_prompt
    assert job.extraction.page_report["headline"] == "Northwind savings range"
    part_notes = job.extraction.page_report["part_notes"]
    assert len(part_notes) == rag["parts"] and part_notes[-1][0].startswith(f"Part {rag['parts']} describes")
    assert job.extraction.page_report["key_points"] == [f"Rates from 3.01% to {3 + SECTIONS / 100:.2f}% APY"]

    assert len(job.chunks) == rag["chunks"] and sum(c.retrieved for c in job.chunks) == rag["retrieved"] > 0
    assert any(e["stage"] == "rag" for e in job.events)
