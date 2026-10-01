from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Literal

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.ai.llm_client import LLMClient, list_llm_profiles, llm_status, load_llm_profiles
from app.compliance.checker import run_compliance_checks
from app.config import get_settings, setup_logging
from app.database.database import SessionLocal, get_db, init_db
from app.database.models import Item, ScrapeJob, Source
from app.pipeline import CANCEL_MESSAGE, run_job
from app.reports.csv_report import build_csv
from app.reports.pdf_report import build_pdf
from app.scraper.http_scraper import HttpScraper
from app.validation.schemas import (
    ComplianceReport,
    ItemOut,
    JobDetail,
    JobOut,
    JobStatus,
    ScrapeRequest,
)

log = logging.getLogger(__name__)
settings = get_settings()
FRONTEND_DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"

ACTIVE_STATUSES = (JobStatus.QUEUED.value, JobStatus.RUNNING.value)
STREAM_POLL_SECONDS = 1.0
STREAM_MAX_SECONDS = 1800


def _fail_interrupted_jobs() -> None:
    # Without Celery a job runs inside this process, so anything still queued or running was lost
    # when the server last stopped. Left alone it stays RUNNING forever and blocks re-uploads.
    message = "Interrupted: the server stopped while this job was running"
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        jobs = db.scalars(select(ScrapeJob).where(ScrapeJob.status.in_(ACTIVE_STATUSES))).all()
        for job in jobs:
            job.status = JobStatus.FAILED.value
            job.error = message
            job.finished_at = job.finished_at or now
            job.events = [*(job.events or []),
                          {"at": now.isoformat(), "stage": "error", "level": "error", "message": message}]
        db.commit()
    if jobs:
        log.warning("Marked %d interrupted job(s) as failed", len(jobs))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    setup_logging(settings)
    init_db()
    if not settings.use_celery:
        _fail_interrupted_jobs()
    load_llm_profiles(settings.llm_config_file)  # fail fast on a malformed config or a key in it
    status = llm_status(settings)
    log.info("Web scraper API started (LLM: %s, Celery: %s)",
             f"{status['profile']} → {status['model']}" if status["enabled"] else f"disabled — {status['reason']}",
             settings.use_celery)
    yield


app = FastAPI(
    title="Web Scraper API",
    version="2.0.0",
    description="Governance-first web scraping with grounded AI extraction and keyword search.",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _dispatch(job_id: str, background: BackgroundTasks) -> None:
    if settings.use_celery:
        from app.worker import run_scrape_job

        run_scrape_job.delay(job_id)
    else:
        background.add_task(run_job, job_id)


def _get_job(db: Session, job_id: str) -> ScrapeJob:
    job = db.get(ScrapeJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def _job_detail(job: ScrapeJob) -> JobDetail:
    extraction = job.extraction
    return JobDetail(
        **JobOut.model_validate(job).model_dump(),
        request=job.request or {},
        compliance=job.compliance,
        events=job.events or [],
        items=[ItemOut.model_validate(i) for i in job.items],
        understanding=extraction.understanding if extraction else None,
        page_report=extraction.page_report if extraction else None,
        rejected=(extraction.rejected or []) if extraction else [],
        tables=extraction.tables if extraction else None,
        page_metadata=extraction.page_metadata if extraction else None,
        rag=extraction.rag if extraction else None,
    )


def _check_profile(name: str | None) -> None:
    if name is None:
        return
    profiles, _ = load_llm_profiles(settings.llm_config_file)
    if name not in profiles:
        raise HTTPException(status_code=422, detail=f"Unknown LLM profile {name!r}; available: {', '.join(profiles)}")


def _download(content: bytes, media_type: str, filename: str) -> Response:
    return Response(content, media_type=media_type,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "llm": llm_status(settings), "celery": settings.use_celery}


@app.get("/api/llm/profiles")
def llm_profiles() -> dict:
    """LLM profiles from config/llm.toml and whether each is usable (API keys are never returned)."""
    return list_llm_profiles(settings)


@app.post("/api/compliance/check", response_model=ComplianceReport)
async def compliance_check(request: ScrapeRequest) -> ComplianceReport:
    """Dry run: governance checks only, nothing is extracted or stored."""
    _check_profile(request.llm_profile)
    llm = LLMClient(settings, profile=request.llm_profile)
    try:
        async with HttpScraper(settings) as scraper:
            outcome = await run_compliance_checks(
                str(request.url), scraper,
                api_endpoint=str(request.api_endpoint) if request.api_endpoint else None, llm=llm,
            )
    finally:
        await llm.aclose()
    return outcome.report


@app.post("/api/jobs", response_model=JobOut, status_code=202)
def create_job(request: ScrapeRequest, background: BackgroundTasks, db: Session = Depends(get_db)) -> ScrapeJob:
    _check_profile(request.llm_profile)
    job = ScrapeJob(url=str(request.url), request=request.model_dump(mode="json"),
                    status=JobStatus.QUEUED.value, events=[])
    db.add(job)
    db.commit()
    log.info("Job %s queued for %s", job.id, job.url)
    _dispatch(job.id, background)
    return job


@app.get("/api/jobs", response_model=list[JobOut])
def list_jobs(
    status: list[JobStatus] | None = Query(None, description="Keep only these statuses; repeat for several"),
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    stmt = select(ScrapeJob).options(selectinload(ScrapeJob.items))
    if status:
        stmt = stmt.where(ScrapeJob.status.in_([s.value for s in status]))
    stmt = stmt.order_by(func.coalesce(ScrapeJob.updated_at, ScrapeJob.created_at).desc()).limit(limit)
    return db.scalars(stmt).all()


@app.get("/api/jobs/{job_id}", response_model=JobDetail)
def get_job(job_id: str, db: Session = Depends(get_db)) -> JobDetail:
    return _job_detail(_get_job(db, job_id))


@app.delete("/api/jobs/{job_id}", response_model=JobOut)
def cancel_job(job_id: str, db: Session = Depends(get_db)) -> ScrapeJob:
    """Ask a queued or running job to stop. A running pipeline stops at its next stage boundary."""
    job = _get_job(db, job_id)
    if job.status not in ACTIVE_STATUSES:
        raise HTTPException(status_code=409, detail=f"Job is already {job.status}; there is nothing to cancel")
    now = datetime.now(timezone.utc)
    job.status = JobStatus.CANCELLED.value
    job.error = CANCEL_MESSAGE
    job.finished_at = now
    job.events = [*(job.events or []),
                  {"at": now.isoformat(), "stage": "cancelled", "level": "warn",
                   "message": "Cancel requested; the pipeline stops at the next stage"}]
    db.commit()
    log.info("Job %s cancelled", job_id)
    return job


@app.post("/api/jobs/{job_id}/retry", response_model=JobOut, status_code=202)
def retry_job(job_id: str, background: BackgroundTasks, db: Session = Depends(get_db)) -> ScrapeJob:
    """Queue a new job with the same request as an earlier one. The earlier job is left untouched."""
    previous = _get_job(db, job_id)
    if previous.status in ACTIVE_STATUSES:
        raise HTTPException(status_code=409, detail=f"Job is {previous.status}; cancel it before retrying")
    try:
        request = ScrapeRequest.model_validate(previous.request or {})
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'request'}: {e['msg']}"
                             for e in exc.errors()[:3])
        hint = f" Start a new job for {previous.url} instead." if previous.retryable else ""
        raise HTTPException(
            status_code=422,
            detail=f"This job cannot be re-run because its saved settings are no longer valid ({problems}).{hint}",
        )
    _check_profile(request.llm_profile)
    job = ScrapeJob(url=str(request.url), request=request.model_dump(mode="json"),
                    status=JobStatus.QUEUED.value, events=[])
    db.add(job)
    db.commit()
    log.info("Job %s queued as a retry of %s", job.id, job_id)
    _dispatch(job.id, background)
    return job


def _sse(name: str, payload: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(payload)}\n\n"


def _read_progress(job_id: str) -> tuple[str, list] | None:
    with SessionLocal() as db:
        job = db.get(ScrapeJob, job_id)
        return None if job is None else (job.status, list(job.events or []))


@app.get("/api/jobs/{job_id}/events")
async def stream_job_events(job_id: str, after: int = Query(0, ge=0)) -> StreamingResponse:
    """Server-sent events: one `stage` event per pipeline step as it happens, then a final `end` event.

    `after` is how many steps the caller already has, so a reconnecting client sees no duplicates.
    """
    async def stream() -> AsyncIterator[str]:
        sent = after
        deadline = time.monotonic() + STREAM_MAX_SECONDS
        while True:
            progress = await asyncio.to_thread(_read_progress, job_id)
            if progress is None:
                yield _sse("end", {"status": "missing"})
                return
            status, events = progress
            for event in events[sent:]:
                yield _sse("stage", event)
            sent = max(sent, len(events))
            if status not in ACTIVE_STATUSES:
                yield _sse("end", {"status": status})
                return
            if time.monotonic() > deadline:
                yield _sse("end", {"status": status, "timeout": True})
                return
            yield ": keep-alive\n\n"
            await asyncio.sleep(STREAM_POLL_SECONDS)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/jobs/{job_id}/report/{fmt}")
def job_report(job_id: str, fmt: Literal["csv", "pdf"], db: Session = Depends(get_db)) -> Response:
    job = _get_job(db, job_id)
    stem = f"scrape-report-{job.id[:8]}"
    if fmt == "csv":
        return _download(build_csv(job.items), "text/csv; charset=utf-8", f"{stem}.csv")
    return _download(build_pdf(job, job.items), "application/pdf", f"{stem}.pdf")


def _item_query(item_type: str | None, q: str | None):
    stmt = select(Item).options(selectinload(Item.references)).order_by(Item.created_at.desc())
    if item_type:
        stmt = stmt.where(Item.type == item_type.strip().lower())
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(or_(Item.name.ilike(pattern), Item.description.ilike(pattern)))
    return stmt


@app.get("/api/items", response_model=list[ItemOut])
def list_items(
    type: str | None = None,
    q: str | None = None,
    limit: int = Query(200, ge=1, le=2000),
    db: Session = Depends(get_db),
):
    return db.scalars(_item_query(type, q).limit(limit)).all()


@app.get("/api/items/export/csv")
def export_items(type: str | None = None, q: str | None = None, db: Session = Depends(get_db)):
    return _download(build_csv(db.scalars(_item_query(type, q)).all()), "text/csv; charset=utf-8", "items.csv")


@app.get("/api/items/{item_id}", response_model=ItemOut)
def get_item(item_id: int, db: Session = Depends(get_db)):
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return item


@app.get("/api/stats")
def stats(db: Session = Depends(get_db)) -> dict:
    by_type = dict(db.execute(select(Item.type, func.count(Item.id)).group_by(Item.type)
                              .order_by(func.count(Item.id).desc()).limit(8)).all())
    by_status = dict(db.execute(select(ScrapeJob.status, func.count(ScrapeJob.id)).group_by(ScrapeJob.status)).all())
    return {
        "items": db.scalar(select(func.count(Item.id))) or 0,
        "sources": db.scalar(select(func.count(Source.id))) or 0,
        "jobs": sum(by_status.values()),
        "items_by_type": by_type,
        "jobs_by_status": by_status,
    }

if FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
