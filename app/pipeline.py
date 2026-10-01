from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.extraction import extract_items, merge_part_items, same_item
from app.ai.keywords import keyword_findings, tag_items
from app.ai.llm_client import LLMClient
from app.ai.rag import RagIndex, build_index, load_rag_config, source_length, split_parts
from app.ai.summarization import REPORT_QUESTIONS, part_notes, report_context, summarize_page
from app.ai.webpage_understanding import understand_page
from app.compliance.checker import ComplianceOutcome, run_compliance_checks
from app.compliance.pii import get_pii_detector
from app.config import Settings, get_settings
from app.database.database import SessionLocal
from app.database.models import DocumentChunk, ExtractionResult, Item, ItemReference, ScrapeJob, Source
from app.scraper.browser_scraper import BrowserUnavailable, fetch_rendered, needs_browser
from app.scraper.http_scraper import FetchResult, HttpScraper
from app.scraper.parser import ParsedPage, parse_html, parse_json_payload
from app.validation.grounding import PageEvidence, ground_item, ground_report
from app.validation.merge import merge_item, merge_reports
from app.validation.schemas import (
    ComplianceReport,
    DataType,
    ExtractedItem,
    JobStatus,
    PageReport,
    PageUnderstanding,
    RejectedItem,
    ScrapeRequest,
    validate_items,
)

log = logging.getLogger(__name__)

ScraperFactory = Callable[[Settings], HttpScraper]
LLMFactory = Callable[[Settings], LLMClient]

CANCEL_MESSAGE = "Cancelled at your request"


class JobCancelled(Exception):
    """The job's row was set to 'cancelled' by the API while the pipeline was running."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _event(db: Session, job: ScrapeJob, stage: str, message: str, level: str = "info") -> None:
    job.events = [*(job.events or []), {"at": _now().isoformat(), "stage": stage, "message": message, "level": level}]
    db.commit()
    log_level = {"error": logging.ERROR, "warn": logging.WARNING}.get(level, logging.INFO)
    log.log(log_level, "[job %s] %s: %s", job.id[:8], stage, message)


def _stop_requested(db: Session, job: ScrapeJob) -> bool:
    # The cancel endpoint writes from its own session, so re-read the column we care about.
    db.refresh(job, ["status"])
    return job.status == JobStatus.CANCELLED.value


async def run_job(
    job_id: str,
    *,
    scraper_factory: ScraperFactory | None = None,
    llm_factory: LLMFactory | None = None,
) -> None:
    settings = get_settings()
    db = SessionLocal()
    llm: LLMClient | None = None
    try:
        job = db.get(ScrapeJob, job_id)
        if job is None:
            log.error("Job %s not found", job_id)
            return
        if job.status == JobStatus.CANCELLED.value:
            log.info("Job %s was cancelled before it started", job_id)
            return
        request = job.request or {}
        llm = llm_factory(settings) if llm_factory else LLMClient(settings, profile=request.get("llm_profile"))
        job.status = JobStatus.RUNNING.value
        job.started_at = _now()
        db.commit()
        try:
            factory = scraper_factory or HttpScraper
            async with factory(settings) as scraper:
                await _execute(db, job, scraper, llm, settings)
        except JobCancelled:
            db.rollback()  # discard the stage that was in flight; keep what earlier stages committed
            job.status = JobStatus.CANCELLED.value
            job.error = job.error or CANCEL_MESSAGE
            _event(db, job, "cancelled", "Stopped before finishing", level="warn")
        except Exception as exc:
            log.exception("Job %s failed", job_id)
            db.rollback()
            job.status = JobStatus.FAILED.value
            job.error = f"{type(exc).__name__}: {exc}"[:2000]
            _event(db, job, "error", job.error, level="error")
        finally:
            job.finished_at = _now()
            db.commit()
    finally:
        if llm is not None:
            await llm.aclose()
        db.close()


def _report_checks(report: ComplianceReport, emit: Callable[..., None]) -> None:
    for check in report.checks:
        level = {"fail": "error", "warn": "warn"}.get(check.status.value, "info")
        emit("compliance", f"{check.name}: {check.status.value.upper()} — {check.summary}", level)


async def _execute(db: Session, job: ScrapeJob, scraper: HttpScraper, llm: LLMClient, settings: Settings) -> None:
    request = ScrapeRequest.model_validate(job.request)
    url = str(request.url)

    def emit(stage: str, message: str, level: str = "info") -> None:
        _event(db, job, stage, message, level)
        if _stop_requested(db, job):
            raise JobCancelled

    emit("compliance", "Running governance checks")
    outcome = await run_compliance_checks(
        url, scraper, api_endpoint=str(request.api_endpoint) if request.api_endpoint else None, llm=llm
    )
    report = outcome.report
    job.compliance = report.model_dump(mode="json")
    source = _get_or_create_source(db, url)
    job.source = source
    source.api_available = bool(report.api and report.api.available)
    _report_checks(report, emit)

    if not report.allowed:
        job.status = JobStatus.BLOCKED.value
        job.error = "; ".join(report.blocked_reasons)[:2000]
        emit("compliance", "BLOCKED — scraping stopped", "error")
        return
    emit("compliance", "ALLOWED — proceeding")

    fetched, method = await _route_and_fetch(request, outcome, settings, emit)
    job.fetch_method = method
    emit("router", f"Content fetched via {method} ({len(fetched.text):,} characters)")

    if fetched.is_json:
        page = await asyncio.to_thread(parse_json_payload, fetched.json(), fetched.final_url)
    else:
        page = await asyncio.to_thread(parse_html, fetched.text, fetched.final_url)
    emit("extraction", f"{len(page.blocks)} text blocks, {len(page.tables)} tables, "
                       f"{len(page.figures)} figures, {len(page.json_ld)} JSON-LD items")
    emit("cleaning", f"Removed {page.stats.get('noise_elements_removed', 0)} noise elements (scripts, navigation, "
                     f"ads, banners) and {page.stats.get('duplicate_blocks_removed', 0)} duplicate/boilerplate "
                     f"text blocks")
    await _process(db, job, source, page, fetched.final_url, set(request.data_types), request.keywords,
                   llm, settings, emit)


async def _process(
    db: Session,
    job: ScrapeJob,
    source: Source,
    page: ParsedPage,
    source_url: str,
    data_types: set[DataType],
    keywords: list[str],
    llm: LLMClient,
    settings: Settings,
    emit: Callable[..., None],
) -> None:
    """Shared stages for web pages and documents."""
    redactions = await asyncio.to_thread(page.redact, get_pii_detector().redact)
    if redactions:
        emit("cleaning", f"Redacted {redactions} personal-data item(s) before AI processing")

    max_chars = llm.max_input_chars
    understanding = await understand_page(page, llm, max_chars)
    source.title = page.title[:500]
    source.publisher = (understanding.publisher or "")[:255] or None
    emit("understanding", f"Page type: {understanding.page_type}; publisher: "
                          f"{understanding.publisher or 'unknown'} (via {understanding.method})")

    # A source longer than one prompt is read in parts so that nothing is cut off, and it is
    # indexed for retrieval so the report can draw on the most relevant passages.
    rag_config = load_rag_config(settings)
    parts = split_parts(page, max_chars) if llm.enabled and source_length(page) > max_chars else [page]
    index: RagIndex | None = None
    if len(parts) > 1:
        emit("rag", f"Long source ({len(page.text):,} characters): reading all of it in {len(parts)} parts")
        index = await build_index(page, REPORT_QUESTIONS, rag_config)
        emit("rag", f"Indexed {len(index.chunks)} chunks with {index.method} for retrieval")

    evidence = PageEvidence.from_page(page)
    raw_items: list = []
    valid: list[ExtractedItem] = []
    rejected: list[RejectedItem] = []
    if DataType.ITEMS in data_types:
        raw_items, method = await _extract(page, parts, understanding, llm, max_chars, keywords, emit)
        emit("extraction", f"{len(raw_items)} candidate item(s) extracted via {method}")
        unverified_total = 0
        for raw in raw_items:
            if isinstance(raw, dict):
                ground_item(raw, evidence)
                unverified_total += len(raw["unverified"])
        if unverified_total:
            emit("validation", f"Removed {unverified_total} fact(s) not supported by the source "
                               f"(see each item's 'unverified' list)", "warn")
        valid, rejected = validate_items(raw_items, source_url=source_url,
                                         min_confidence=settings.min_item_confidence)
        emit("validation", f"{len(valid)} item(s) valid, {len(rejected)} rejected",
             "warn" if rejected and not valid else "info")
    else:
        emit("extraction", "Item extraction skipped: items were not selected")

    if keywords:
        valid = tag_items(valid, keywords)
        emit("keywords", f"{sum(1 for i in valid if i.matched_keywords)} item(s) mention your keywords")

    updating = bool((job.request or {}).get("update")) and job.extraction is not None
    previous_report = job.extraction.page_report if updating else None
    rows, merged = _store(db, job, source, page, valid, rejected, understanding, raw_items, data_types, llm)
    if updating:
        emit("storage", f"Updated existing results: {merged} item(s) merged with new facts, "
                        f"{len(rows) - merged} new item(s) added, {len(job.items) - len(rows)} kept unchanged")
    else:
        emit("storage", f"Stored {len(rows)} item(s) with source references")

    findings = []
    if keywords:
        emit("keywords", f"Searching the whole source for: {', '.join(keywords)}")
        findings = await keyword_findings(page, keywords, valid, llm, rag_config, max_chars)
        for finding in findings:
            exact = sum(1 for p in finding.passages if p.match == "exact")
            related = len(finding.passages) - exact
            emit("keywords", f"'{finding.keyword}': {exact} exact and {related} related passage(s), "
                             f"{len(finding.items)} item(s)", "info" if finding.found else "warn")

    emit("report", f"Writing the report via {llm.model_name}" if llm.enabled else "Writing the report")
    content = None
    notes: list[list[str]] = []
    if index is not None:
        for number, part in enumerate(parts, 1):
            emit("report", f"Reading part {number} of {len(parts)} for the summary")
            notes.append(await part_notes(part, number, len(parts), llm, max_chars))
        notes_only = report_context(page, notes, [], max_chars)
        passages = index.retrieve(rag_config.top_k, budget_chars=max_chars - len(notes_only) - 200)
        content = report_context(page, notes, [c.labelled() for c in passages], max_chars)
        _store_chunks(db, job, index, {c.index for c in passages})
        job.extraction.rag = {
            "characters": len(page.text), "parts": len(parts), "chunks": len(index.chunks),
            "embedding": index.method, "retrieved": len(passages),
            "notes": sum(len(n) for n in notes), "questions": REPORT_QUESTIONS,
        }
        emit("rag", f"Summary input: {sum(len(n) for n in notes)} notes from all {len(parts)} parts and "
                    f"{len(passages)} retrieved passages")
    report = await summarize_page(page, understanding, valid, llm, max_chars, content=content)
    report = report.model_copy(update={"part_notes": notes, "keyword_findings": findings})
    report, dropped = ground_report(report, evidence)
    words = len(report.detailed_summary.split())
    emit("report", f"Detailed information: {len(report.sections)} section(s), {len(report.key_figures)} key "
                   f"figure(s), {words}-word detailed summary")
    if dropped:
        emit("report", f"Dropped {dropped} report line(s) or figure(s) that are not supported by the source", "warn")
    if previous_report:
        report = merge_reports(PageReport.model_validate(previous_report), report)
        emit("report", "Merged the new report with the earlier findings for this document")
    job.extraction.page_report = report.model_dump(mode="json")
    if report.method == "llm":
        emit("report", f"Report written by {report.model}: {report.headline}")
    elif llm.enabled:
        emit("report", "LLM report failed; stored a basic extractive summary instead", "warn")
    else:
        emit("report", "LLM not configured — set OPENROUTER_API_KEY in .env and choose a profile "
                       "(e.g. gemma-4-26b-moe) in config/llm.toml; stored a basic extractive summary instead", "warn")

    source.last_scraped_at = _now()
    job.status = JobStatus.COMPLETED.value
    emit("done", "Pipeline completed")


async def _extract(
    page: ParsedPage,
    parts: list[ParsedPage],
    understanding: PageUnderstanding,
    llm: LLMClient,
    max_chars: int,
    keywords: list[str],
    emit: Callable[..., None],
) -> tuple[list, str]:
    if len(parts) == 1:
        return await extract_items(page, understanding, llm, max_chars, keywords)
    found, methods = [], set()
    for number, part in enumerate(parts, 1):
        emit("extraction", f"Extracting items from part {number} of {len(parts)}")
        items, method = await extract_items(part, understanding, llm, max_chars, keywords)
        found.append(items)
        methods.add(method)
    return merge_part_items(found), f"{'+'.join(sorted(methods))} over {len(parts)} parts"


async def _route_and_fetch(
    request: ScrapeRequest,
    outcome: ComplianceOutcome,
    settings: Settings,
    emit: Callable[..., None],
) -> tuple[FetchResult, str]:
    """Official API if one was verified; otherwise static HTML, or Playwright for JS-rendered pages."""
    if outcome.api_result is not None:
        return outcome.api_result, "api"

    landing = outcome.landing
    if landing is None:  # cannot happen when compliance allowed the job
        raise RuntimeError("compliance passed without a fetched landing page")
    if landing.is_json:
        return landing, "http"

    wants_browser = request.render_mode == "browser" or (
        request.render_mode == "auto" and needs_browser(landing.text)
    )
    if not wants_browser:
        return landing, "http"
    if not settings.playwright_enabled:
        if request.render_mode == "browser":
            raise BrowserUnavailable("browser rendering is disabled (PLAYWRIGHT_ENABLED=false)")
        emit("router", "Page looks JavaScript-rendered but Playwright is disabled; using static HTML", "warn")
        return landing, "http"
    try:
        return await fetch_rendered(landing.final_url, settings), "browser"
    except BrowserUnavailable as exc:
        if request.render_mode == "browser":
            raise
        emit("router", f"Browser rendering unavailable ({exc}); using static HTML", "warn")
        return landing, "http"


def _store_chunks(db: Session, job: ScrapeJob, index: RagIndex, retrieved: set[int]) -> None:
    keep_vectors = index.method != "tfidf"  # TF-IDF vectors only mean something within this run
    job.chunks = [
        DocumentChunk(
            idx=chunk.index, heading=(chunk.heading or "")[:500] or None, text=chunk.text,
            embedding=[round(float(v), 6) for v in index.vectors[chunk.index]] if keep_vectors else None,
            embedding_model=index.method, retrieved=chunk.index in retrieved,
        )
        for chunk in index.chunks
    ]
    db.flush()


def _get_or_create_source(db: Session, url: str) -> Source:
    source = db.scalar(select(Source).where(Source.url == url))
    if source is None:
        parts = urlsplit(url)
        source = Source(url=url, domain="local-documents" if parts.scheme == "file" else parts.netloc.lower())
        db.add(source)
        db.flush()
    return source


def _store(
    db: Session,
    job: ScrapeJob,
    source: Source,
    page: ParsedPage,
    valid: list[ExtractedItem],
    rejected: list[RejectedItem],
    understanding: PageUnderstanding,
    raw_items: list,
    data_types: set[DataType],
    llm: LLMClient,
) -> tuple[list[Item], int]:
    # A re-upload updates the job in place: matching items are merged and nothing stored earlier is removed.
    # Returns (rows written, how many were merged into existing items).
    now = _now()
    values = dict(
        understanding=understanding.model_dump(mode="json"),
        raw_items=[i for i in raw_items if isinstance(i, dict)],
        rejected=[r.model_dump() for r in rejected],
        tables=[asdict(t) for t in page.tables] if DataType.TABLES in data_types else None,
        text=page.text[:100_000] if DataType.TEXT in data_types else None,
        page_metadata=page.metadata if DataType.METADATA in data_types else None,
        llm_model=llm.model_name,
        content_hash=page.content_hash,
        rag=None,
    )
    if job.extraction is None:
        db.add(ExtractionResult(job=job, **values))
    else:
        for key, value in values.items():
            setattr(job.extraction, key, value)
    job.chunks = []  # re-indexed later when the source is long

    rows: list[Item] = []
    merged_count = 0
    for item in valid:
        row = next((i for i in job.items if same_item(i.name, item.name)), None)
        if row is None:
            row = Item(job=job, name=item.name)
            db.add(row)
        else:
            item = merge_item(ExtractedItem.model_validate(row.data), item)
            merged_count += 1
        row.source = source
        row.type = item.type
        row.description = item.description
        row.confidence = item.confidence
        row.extraction_method = item.extraction_method
        row.data = item.model_dump(mode="json")
        row.references = [*row.references, ItemReference(url=str(item.source_url), snippet=item.source_snippet,
                                                         content_hash=page.content_hash, retrieved_at=now)]
        rows.append(row)
    db.commit()
    return rows, merged_count
