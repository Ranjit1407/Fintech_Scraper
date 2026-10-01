"""Cancel, retry, status filtering and the live event stream."""

import asyncio
import json

from fastapi.testclient import TestClient

from app.database.database import SessionLocal, init_db
from app.database.models import ScrapeJob
from app.main import app
from app.pipeline import CANCEL_MESSAGE, run_job
from app.scraper.http_scraper import HttpScraper
from app.validation.schemas import JobStatus, ScrapeRequest
from tests.fixtures import make_transport

URL = "https://bank.example/cards"


def _make_job(status: str, events: list | None = None) -> str:
    init_db()
    with SessionLocal() as db:
        job = ScrapeJob(url=URL, request=ScrapeRequest(url=URL).model_dump(mode="json"),
                        status=status, events=events or [])
        db.add(job)
        db.commit()
        return job.id


def _status(job_id: str) -> str:
    with SessionLocal() as db:
        return db.get(ScrapeJob, job_id).status


class CancellingLLM:
    """Answers nothing, and cancels the job from a separate session the way the API does."""

    enabled = True
    model_name = "fake:model"
    max_input_chars = 15_000

    def __init__(self, job_id: str):
        self.job_id = job_id
        self.calls = 0

    async def complete_json(self, system: str, user: str):
        self.calls += 1
        if self.calls == 1:
            with SessionLocal() as db:
                job = db.get(ScrapeJob, self.job_id)
                job.status = JobStatus.CANCELLED.value
                job.error = CANCEL_MESSAGE
                db.commit()
        return None

    async def aclose(self):
        pass


def test_cancel_a_queued_job():
    with TestClient(app) as client:  # queued jobs are created inside: startup fails any it finds
        job_id = _make_job(JobStatus.QUEUED.value)
        body = client.delete(f"/api/jobs/{job_id}").json()
        assert body["status"] == "cancelled" and body["error"] == CANCEL_MESSAGE

        again = client.delete(f"/api/jobs/{job_id}")
        assert again.status_code == 409 and "already cancelled" in again.text
        assert client.delete("/api/jobs/does-not-exist").status_code == 404


def test_a_cancelled_job_does_not_start():
    job_id = _make_job(JobStatus.CANCELLED.value)
    asyncio.run(run_job(job_id, scraper_factory=lambda s: HttpScraper(s, transport=make_transport())))
    with SessionLocal() as db:
        job = db.get(ScrapeJob, job_id)
        assert job.status == "cancelled"
        assert job.started_at is None and job.events == []


def test_cancelling_stops_a_running_pipeline():
    job_id = _make_job(JobStatus.QUEUED.value)
    llm = CancellingLLM(job_id)
    transport = make_transport()
    asyncio.run(run_job(job_id,
                        scraper_factory=lambda settings: HttpScraper(settings, transport=transport),
                        llm_factory=lambda settings: llm))
    with SessionLocal() as db:
        job = db.get(ScrapeJob, job_id)
        assert job.status == "cancelled"
        assert job.items == []  # stopped before anything was stored
        assert job.finished_at is not None
        assert any(e["stage"] == "cancelled" for e in job.events)


def test_retry_queues_a_copy_of_the_request(monkeypatch):
    queued: list[str] = []
    monkeypatch.setattr("app.main._dispatch", lambda job_id, background: queued.append(job_id))
    job_id = _make_job(JobStatus.FAILED.value)

    with TestClient(app) as client:
        retried = client.post(f"/api/jobs/{job_id}/retry")
        assert retried.status_code == 202
        body = retried.json()
        assert body["id"] != job_id and body["status"] == "queued" and body["url"] == URL
        assert queued == [body["id"]]
        assert _status(job_id) == "failed"  # the original is left alone

        running = _make_job(JobStatus.RUNNING.value)
        busy = client.post(f"/api/jobs/{running}/retry")
        assert busy.status_code == 409 and "cancel it before retrying" in busy.text


def test_retry_rejects_a_request_that_is_no_longer_valid(monkeypatch):
    monkeypatch.setattr("app.main._dispatch", lambda job_id, background: None)
    job_id = _make_job(JobStatus.FAILED.value)
    with SessionLocal() as db:
        db.get(ScrapeJob, job_id).request = {"url": "http://10.0.0.1/"}  # a private host, now blocked
        db.commit()

    with TestClient(app) as client:
        response = client.post(f"/api/jobs/{job_id}/retry")
        assert response.status_code == 422 and "no longer valid" in response.text


def test_a_job_from_the_document_era_is_not_offered_for_retry(monkeypatch):
    """Upload jobs saved before documents were removed hold a request with no url."""
    monkeypatch.setattr("app.main._dispatch", lambda job_id, background: None)
    job_id = _make_job(JobStatus.COMPLETED.value)
    with SessionLocal() as db:
        job = db.get(ScrapeJob, job_id)
        job.url = "file:///C:/uploads/abc/report.pdf"
        job.request = {"document_path": "C:/uploads/abc/report.pdf", "original_name": "report.pdf",
                       "keywords": [], "data_types": ["items"], "source": "upload", "update": False}
        db.commit()

    with TestClient(app) as client:
        listed = next(j for j in client.get("/api/jobs", params={"limit": 500}).json() if j["id"] == job_id)
        assert listed["retryable"] is False
        assert client.get(f"/api/jobs/{job_id}").json()["retryable"] is False

        response = client.post(f"/api/jobs/{job_id}/retry")
        assert response.status_code == 422
        assert "cannot be re-run" in response.text and "url: Field required" in response.text
        assert "Start a new job" not in response.text  # a file path is no use as a starting point

        # a normal web job is still offered
        healthy = next(j for j in client.get("/api/jobs", params={"limit": 500}).json() if j["id"] != job_id)
        assert healthy["retryable"] is True


def test_jobs_can_be_filtered_by_status():
    completed = _make_job(JobStatus.COMPLETED.value)
    failed = _make_job(JobStatus.FAILED.value)

    with TestClient(app) as client:
        queued = _make_job(JobStatus.QUEUED.value)

        def ids(**params):
            return {job["id"] for job in client.get("/api/jobs", params={"limit": 500, **params}).json()}

        assert {completed, failed, queued} <= ids()
        assert failed in ids(status="failed") and completed not in ids(status="failed")
        both = ids(status=["queued", "failed"])
        assert {queued, failed} <= both and completed not in both
        assert client.get("/api/jobs", params={"status": "nonsense"}).status_code == 422


def test_event_stream_replays_the_log_and_ends():
    events = [{"at": "2026-01-01T00:00:00+00:00", "stage": "compliance", "message": "first", "level": "info"},
              {"at": "2026-01-01T00:00:01+00:00", "stage": "done", "message": "second", "level": "info"}]
    job_id = _make_job(JobStatus.COMPLETED.value, events=events)

    with TestClient(app) as client:
        response = client.get(f"/api/jobs/{job_id}/events")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = response.text
        assert body.count("event: stage") == 2
        assert json.loads(body.split("event: stage\ndata: ")[1].split("\n")[0])["message"] == "first"
        assert 'event: end' in body and '"status": "completed"' in body

        # a client that already has the first step only receives the second
        resumed = client.get(f"/api/jobs/{job_id}/events", params={"after": 1}).text
        assert resumed.count("event: stage") == 1 and "second" in resumed and "first" not in resumed

        missing = client.get("/api/jobs/does-not-exist/events").text
        assert '"status": "missing"' in missing
