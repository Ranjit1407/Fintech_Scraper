from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.ai.llm_client import LLMClient, list_llm_profiles, llm_status, load_llm_profiles
from app.compliance.checker import run_compliance_checks
from app.config import get_settings, setup_logging
from app.database.database import get_db, init_db
from app.database.models import Product, ScrapeJob, Source
from app.documents.loader import SUPPORTED_EXTENSIONS, DocumentError, list_documents, resolve_folder
from app.documents.uploads import (
    batch_folder,
    delete_batch,
    describe_previous,
    find_previous_document,
    is_processed,
    mark_processed,
    read_manifest,
    save_upload,
)
from app.pipeline import run_job
from app.reports.csv_report import build_csv
from app.reports.pdf_report import build_pdf
from app.scraper.http_scraper import HttpScraper
from app.validation.schemas import (
    ComplianceReport,
    DataType,
    DocumentFolderRequest,
    DocumentJobRequest,
    JobDetail,
    JobOut,
    JobStatus,
    ProcessUploadRequest,
    ProductCategory,
    ProductOut,
    ScrapeRequest,
    UploadBatch,
    UploadedFile,
)

log = logging.getLogger(__name__)
settings = get_settings()
FRONTEND_DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    setup_logging(settings)
    init_db()
    load_llm_profiles(settings.llm_config_file)  # fail fast on a malformed config or a key in it
    status = llm_status(settings)
    log.info("Fintech scraper API started (LLM: %s, Celery: %s)",
             f"{status['profile']} → {status['model']}" if status["enabled"] else f"disabled — {status['reason']}",
             settings.use_celery)
    yield


app = FastAPI(
    title="Fintech Scraper API",
    version="1.0.0",
    description="Compliance-first collection and AI extraction of financial product data.",
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
        products=[ProductOut.model_validate(p) for p in job.products],
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
    """Dry run: compliance checks only, nothing is extracted or stored."""
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


@app.post("/api/documents", response_model=list[JobOut], status_code=202)
def create_document_jobs(request: DocumentFolderRequest, background: BackgroundTasks,
                         db: Session = Depends(get_db)) -> list[ScrapeJob]:
    """Queue one job per supported document in a folder on the machine running the API."""
    _check_profile(request.llm_profile)
    try:
        folder = resolve_folder(request.folder, settings)
    except DocumentError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    files = list_documents(folder, recursive=request.recursive, limit=settings.max_documents_per_folder)
    if not files:
        raise HTTPException(status_code=422, detail=f"No supported documents in {folder} "
                                                    f"({', '.join(sorted(SUPPORTED_EXTENSIONS))})")
    return _queue_documents([(path, None) for path in files], request.data_types, request.llm_profile, db, background)


def _queue_documents(files: list[tuple[Path, UploadedFile | None]], data_types: list[DataType],
                     llm_profile: str | None, db: Session, background: BackgroundTasks,
                     updates: list[tuple[ScrapeJob, Path, UploadedFile, str]] = ()) -> list[ScrapeJob]:
    jobs = []
    for path, info in files:
        job_request = DocumentJobRequest(document_path=str(path), data_types=data_types, llm_profile=llm_profile,
                                         original_name=info.name if info else None,
                                         file_sha256=info.sha256 if info else None)
        job = ScrapeJob(url=path.as_uri(), request=job_request.model_dump(mode="json"),
                        status=JobStatus.QUEUED.value, events=[])
        db.add(job)
        jobs.append(job)
    now = datetime.now(timezone.utc)
    for job, path, info, match in updates:
        previous = job.request or {}
        history = [*previous.get("previous_files", []), previous.get("document_path")]
        job.request = DocumentJobRequest(
            document_path=str(path), data_types=data_types, llm_profile=llm_profile, original_name=info.name,
            file_sha256=info.sha256, update=True, previous_files=[h for h in history if h],
        ).model_dump(mode="json")
        job.url = path.as_uri()
        job.status, job.error, job.updated_at = JobStatus.QUEUED.value, None, now
        kind = "same content" if match == "identical" else "new version with the same name"
        job.events = [*(job.events or []), {
            "at": now.isoformat(), "stage": "update", "level": "info",
            "message": f"Re-uploaded {info.name} ({kind}); new results are merged into the existing data",
        }]
        jobs.append(job)
    db.commit()
    log.info("Queued %d new and %d updated document job(s)", len(files), len(updates))
    for job in jobs:
        _dispatch(job.id, background)
    return jobs


@app.post("/api/uploads", response_model=UploadBatch, status_code=201)
async def upload_documents(files: list[UploadFile] = File(...), db: Session = Depends(get_db)) -> UploadBatch:
    """Store uploaded documents and flag any that were processed before. Nothing is processed yet."""
    try:
        batch = await save_upload(files, settings)
    except DocumentError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    for file in batch.files:
        file.previous = describe_previous(find_previous_document(db, file.sha256, file.name))
    log.info("Upload %s: %d document(s) stored (%d processed before), %d skipped", batch.batch_id[:8],
             len(batch.files), sum(f.previous is not None for f in batch.files), len(batch.skipped))
    return batch


@app.post("/api/uploads/{batch_id}/process", response_model=list[JobOut], status_code=202)
def process_upload(batch_id: str, request: ProcessUploadRequest, background: BackgroundTasks,
                   db: Session = Depends(get_db)) -> list[ScrapeJob]:
    """Queue the upload: documents processed before update their existing job, the rest get new jobs."""
    _check_profile(request.llm_profile)
    try:
        folder = batch_folder(batch_id, settings)
    except DocumentError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    if is_processed(folder):
        raise HTTPException(status_code=409, detail="These documents have already been processed")
    manifest = {f.stored_as: f for f in read_manifest(folder)}
    new_files: list[tuple[Path, UploadedFile | None]] = []
    updates: list[tuple[ScrapeJob, Path, UploadedFile, str]] = []
    for path in list_documents(folder, limit=settings.max_documents_per_folder):
        info = manifest.get(path.name)
        match = find_previous_document(db, info.sha256, info.name) if info and request.update_existing else None
        if match and all(job.id != match[0].id for job, *_ in updates):
            job, kind = match
            if job.status in (JobStatus.QUEUED.value, JobStatus.RUNNING.value):
                raise HTTPException(status_code=409, detail=f"{info.name} is still being processed from an earlier "
                                                            "upload; try again when it has finished")
            updates.append((job, path, info, kind))
        else:
            new_files.append((path, info))
    mark_processed(folder)
    return _queue_documents(new_files, request.data_types, request.llm_profile, db, background, updates)


@app.delete("/api/uploads/{batch_id}", status_code=204)
def cancel_upload(batch_id: str) -> Response:
    try:
        delete_batch(batch_id, settings)
    except DocumentError as exc:
        raise HTTPException(status_code=409 if "already" in str(exc) else 404, detail=str(exc)) from None
    return Response(status_code=204)


@app.get("/api/jobs", response_model=list[JobOut])
def list_jobs(limit: int = Query(50, ge=1, le=500), db: Session = Depends(get_db)):
    stmt = (select(ScrapeJob).options(selectinload(ScrapeJob.products))
            .order_by(func.coalesce(ScrapeJob.updated_at, ScrapeJob.created_at).desc()).limit(limit))
    return db.scalars(stmt).all()


@app.get("/api/jobs/{job_id}", response_model=JobDetail)
def get_job(job_id: str, db: Session = Depends(get_db)) -> JobDetail:
    return _job_detail(_get_job(db, job_id))


@app.get("/api/jobs/{job_id}/report/{fmt}")
def job_report(job_id: str, fmt: Literal["json", "csv", "pdf"], db: Session = Depends(get_db)) -> Response:
    job = _get_job(db, job_id)
    stem = f"fintech-report-{job.id[:8]}"
    if fmt == "json":
        return JSONResponse(_job_detail(job).model_dump(mode="json"),
                            headers={"Content-Disposition": f'attachment; filename="{stem}.json"'})
    if fmt == "csv":
        return _download(build_csv(job.products), "text/csv; charset=utf-8", f"{stem}.csv")
    return _download(build_pdf(job, job.products), "application/pdf", f"{stem}.pdf")


def _product_query(category: ProductCategory | None, q: str | None):
    stmt = select(Product).options(selectinload(Product.references)).order_by(Product.created_at.desc())
    if category:
        stmt = stmt.where(Product.category == category.value)
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(or_(Product.name.ilike(pattern), Product.provider.ilike(pattern)))
    return stmt


@app.get("/api/products", response_model=list[ProductOut])
def list_products(
    category: ProductCategory | None = None,
    q: str | None = None,
    limit: int = Query(200, ge=1, le=2000),
    db: Session = Depends(get_db),
):
    return db.scalars(_product_query(category, q).limit(limit)).all()


@app.get("/api/products/export/csv")
def export_products(category: ProductCategory | None = None, q: str | None = None, db: Session = Depends(get_db)):
    products = db.scalars(_product_query(category, q)).all()
    return _download(build_csv(products), "text/csv; charset=utf-8", "fintech-products.csv")


@app.get("/api/products/{product_id}", response_model=ProductOut)
def get_product(product_id: int, db: Session = Depends(get_db)):
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


@app.get("/api/stats")
def stats(db: Session = Depends(get_db)) -> dict:
    by_category = dict(db.execute(select(Product.category, func.count(Product.id)).group_by(Product.category)).all())
    by_status = dict(db.execute(select(ScrapeJob.status, func.count(ScrapeJob.id)).group_by(ScrapeJob.status)).all())
    return {
        "products": sum(by_category.values()),
        "sources": db.scalar(select(func.count(Source.id))) or 0,
        "jobs": sum(by_status.values()),
        "products_by_category": by_category,
        "jobs_by_status": by_status,
    }


if FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
