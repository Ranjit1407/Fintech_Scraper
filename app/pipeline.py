from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.classification import get_classifier
from app.ai.extraction import extract_products, merge_part_products, same_product
from app.ai.llm_client import LLMClient
from app.ai.rag import RagIndex, build_index, load_rag_config, source_length, split_parts
from app.ai.summarization import REPORT_QUESTIONS, part_notes, report_context, summarize_page, summarize_products
from app.ai.webpage_understanding import understand_page
from app.compliance.checker import ComplianceOutcome, run_compliance_checks
from app.compliance.pii import check_pii, get_pii_detector
from app.config import Settings, get_settings
from app.database.database import SessionLocal
from app.database.models import (
    DocumentChunk,
    ExtractionResult,
    Product,
    ProductAttribute,
    ScrapeJob,
    Source,
    SourceReference,
)
from app.documents.loader import load_document
from app.scraper.browser_scraper import BrowserUnavailable, fetch_rendered, needs_browser
from app.scraper.http_scraper import FetchResult, HttpScraper
from app.scraper.parser import ParsedPage, _table_lines, parse_html, parse_json_payload
from app.validation.grounding import PageEvidence, ground_product, ground_report
from app.validation.merge import merge_product, merge_reports
from app.validation.schemas import (
    CheckStatus,
    ComplianceCheck,
    ComplianceReport,
    DataType,
    DocumentJobRequest,
    ExtractedProduct,
    JobStatus,
    PageReport,
    PageUnderstanding,
    RejectedProduct,
    ScrapeRequest,
    validate_products,
)

log = logging.getLogger(__name__)

ScraperFactory = Callable[[Settings], HttpScraper]
LLMFactory = Callable[[Settings], LLMClient]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _event(db: Session, job: ScrapeJob, stage: str, message: str, level: str = "info") -> None:
    job.events = [*(job.events or []), {"at": _now().isoformat(), "stage": stage, "message": message, "level": level}]
    db.commit()
    log_level = {"error": logging.ERROR, "warn": logging.WARNING}.get(level, logging.INFO)
    log.log(log_level, "[job %s] %s: %s", job.id[:8], stage, message)


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
        request = job.request or {}
        llm = llm_factory(settings) if llm_factory else LLMClient(settings, profile=request.get("llm_profile"))
        job.status = JobStatus.RUNNING.value
        job.started_at = _now()
        db.commit()
        try:
            if request.get("source") == "document":
                await _execute_document(db, job, llm, settings)
            else:
                factory = scraper_factory or HttpScraper
                async with factory(settings) as scraper:
                    await _execute(db, job, scraper, llm, settings)
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


async def _execute(db: Session, job: ScrapeJob, scraper: HttpScraper, llm: LLMClient, settings: Settings) -> None:
    request = ScrapeRequest.model_validate(job.request)
    url = str(request.url)
    data_types = set(request.data_types)

    def emit(stage: str, message: str, level: str = "info") -> None:
        _event(db, job, stage, message, level)

    emit("compliance", "Running compliance checks")
    outcome = await run_compliance_checks(
        url, scraper, api_endpoint=str(request.api_endpoint) if request.api_endpoint else None, llm=llm
    )
    report = outcome.report
    job.compliance = report.model_dump(mode="json")
    source = _get_or_create_source(db, url)
    job.source = source
    source.api_available = bool(report.api and report.api.available)
    for check in report.checks:
        level = {"fail": "error", "warn": "warn"}.get(check.status.value, "info")
        emit("compliance", f"{check.name}: {check.status.value.upper()} — {check.summary}", level)

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
                       f"{len(page.figures)} financial figures, {len(page.json_ld)} JSON-LD items")
    emit("cleaning", f"Removed {page.stats.get('noise_elements_removed', 0)} noise elements (scripts, navigation, "
                     f"ads, banners) and {page.stats.get('duplicate_blocks_removed', 0)} duplicate/boilerplate "
                     f"text blocks")
    await _process(db, job, source, page, fetched.final_url, data_types, llm, settings, emit)


async def _execute_document(db: Session, job: ScrapeJob, llm: LLMClient, settings: Settings) -> None:
    # robots.txt and terms don't apply to a local document, but personal data is still checked and redacted.
    request = DocumentJobRequest.model_validate(job.request)
    path = Path(request.document_path)

    def emit(stage: str, message: str, level: str = "info") -> None:
        _event(db, job, stage, message, level)

    emit("document", f"Loading {path.name}")
    page = await asyncio.to_thread(load_document, path, settings.max_document_bytes)
    job.fetch_method = "document"
    emit("extraction", f"{len(page.blocks)} text blocks, {len(page.tables)} tables, "
                       f"{len(page.figures)} financial figures from {page.metadata.get('file_type', '').upper()} file")

    pii = await asyncio.to_thread(check_pii, page.text + "\n" + _table_lines(page.tables), get_pii_detector(),
                                  settings.pii_block_threshold)
    local = ComplianceCheck(name="local_document", status=CheckStatus.PASS,
                            summary="User-supplied document; website robots.txt and terms checks do not apply",
                            details={"path": str(path)})
    report = ComplianceReport(url=page.url, allowed=pii.status != CheckStatus.FAIL, checks=[local, pii])
    job.compliance = report.model_dump(mode="json")
    source = _get_or_create_source(db, page.url)
    job.source = source
    for check in report.checks:
        level = {"fail": "error", "warn": "warn"}.get(check.status.value, "info")
        emit("compliance", f"{check.name}: {check.status.value.upper()} — {check.summary}", level)
    if not report.allowed:
        job.status = JobStatus.BLOCKED.value
        job.error = "; ".join(report.blocked_reasons)[:2000]
        emit("compliance", "BLOCKED — document not processed", "error")
        return
    await _process(db, job, source, page, page.url, set(request.data_types), llm, settings, emit)


async def _process(
    db: Session,
    job: ScrapeJob,
    source: Source,
    page: ParsedPage,
    source_url: str,
    data_types: set[DataType],
    llm: LLMClient,
    settings: Settings,
    emit: Callable[..., None],
) -> None:
    redactions = await asyncio.to_thread(page.redact, get_pii_detector().redact)
    if redactions:
        emit("cleaning", f"Redacted {redactions} personal-data item(s) before AI processing")

    max_chars = llm.max_input_chars
    understanding = await understand_page(page, llm, max_chars)
    source.title = page.title[:500]
    source.institution = (understanding.institution or "")[:255] or None
    emit("understanding", f"Page type: {understanding.page_type}; institution: "
                          f"{understanding.institution or 'unknown'} (via {understanding.method})")

    # A source longer than one prompt is read in parts so that nothing is cut off, and it is
    # indexed for retrieval so the report can draw on the most relevant passages.
    parts = split_parts(page, max_chars) if llm.enabled and source_length(page) > max_chars else [page]
    index: RagIndex | None = None
    if len(parts) > 1:
        rag_config = load_rag_config(settings)
        emit("rag", f"Long source ({len(page.text):,} characters): reading all of it in {len(parts)} parts")
        index = await build_index(page, REPORT_QUESTIONS, rag_config)
        emit("rag", f"Indexed {len(index.chunks)} chunks with {index.method} for retrieval")

    if len(parts) == 1:
        raw_products, extraction_method = await extract_products(page, understanding, llm, max_chars)
    else:
        found, methods = [], set()
        for number, part in enumerate(parts, 1):
            emit("extraction", f"Extracting products from part {number} of {len(parts)}")
            products, method = await extract_products(part, understanding, llm, max_chars)
            found.append(products)
            methods.add(method)
        raw_products = merge_part_products(found)
        extraction_method = f"{'+'.join(sorted(methods))} over {len(parts)} parts"
    emit("extraction", f"{len(raw_products)} candidate product(s) extracted via {extraction_method}")

    classifier = get_classifier(settings.classifier_model_path)
    for item in raw_products:
        if isinstance(item, dict):
            result = classifier.classify(item)
            item.update(category=result.category.value, category_confidence=result.confidence,
                        classification_method=result.method)
    categories = sorted({str(p.get("category")) for p in raw_products if isinstance(p, dict)})
    emit("classification", f"Categories: {', '.join(categories) or 'none'}")

    evidence = PageEvidence.from_page(page)
    unverified_total = 0
    for item in raw_products:
        if isinstance(item, dict):
            ground_product(item, evidence)
            unverified_total += len(item["unverified"])
    if unverified_total:
        emit("validation", f"Removed {unverified_total} fact(s) not supported by the page "
                           f"(see each product's 'unverified' list)", "warn")
    valid, rejected = validate_products(
        raw_products, source_url=source_url, min_confidence=settings.min_product_confidence,
        data_types=data_types,
    )
    emit("validation", f"{len(valid)} product(s) valid, {len(rejected)} rejected",
         "warn" if rejected and not valid else "info")

    updating = bool((job.request or {}).get("update")) and job.extraction is not None
    previous_report = job.extraction.page_report if updating else None
    rows, stored, merged = _store(db, job, source, page, valid, rejected, understanding, raw_products, data_types, llm)
    if updating:
        emit("storage", f"Updated existing results: {merged} product(s) merged with new facts, "
                        f"{len(rows) - merged} new product(s) added, {len(job.products) - len(rows)} kept unchanged")
    else:
        emit("storage", f"Stored {len(rows)} product(s) with source references")

    summaries = await summarize_products(stored, llm)
    for row, summary in zip(rows, summaries):
        row.summary = summary.model_dump(mode="json")
    if summaries:
        emit("summarization", f"Summarised {len(summaries)} product(s) via {summaries[0].method}")

    emit("report", f"Writing page report via {llm.model_name}" if llm.enabled else "Writing page report")
    content = None
    if index is not None:
        notes = []
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
    if index is not None:
        report = report.model_copy(update={"part_notes": notes})
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
        emit("report", f"Page report written by {report.model}: {report.headline}")
    elif llm.enabled:
        emit("report", "LLM page report failed; stored a basic extractive summary instead", "warn")
    else:
        emit("report", "LLM not configured (choose a profile in config/llm.toml and set its API key in .env); "
                       "stored a basic extractive summary instead", "warn")

    source.last_scraped_at = _now()
    job.status = JobStatus.COMPLETED.value
    emit("done", "Pipeline completed")


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


def _attributes_for(product: ExtractedProduct) -> list[ProductAttribute]:
    attrs: list[ProductAttribute] = []
    for rate in product.rates:
        attrs.append(ProductAttribute(kind="rate", name=rate.label, value=rate.display(),
                                      numeric_value=rate.value if rate.value is not None else rate.min, unit="%"))
    for fee in product.fees:
        attrs.append(ProductAttribute(kind="fee", name=fee.label, value=fee.display(),
                                      numeric_value=fee.amount if fee.amount is not None else fee.percentage,
                                      unit=fee.currency or ("%" if fee.percentage is not None else None)))
    for name in ("min_amount", "max_amount"):
        value = getattr(product, name)
        if value is not None:
            attrs.append(ProductAttribute(kind="amount", name=name, value=f"{value:g}", numeric_value=value,
                                          unit=product.currency))
    if product.tenure:
        attrs.append(ProductAttribute(kind="tenure", name="tenure", value=product.tenure))
    for kind, values in (("eligibility", product.eligibility), ("feature", product.features),
                         ("condition", product.conditions)):
        attrs.extend(ProductAttribute(kind=kind, name=kind, value=v) for v in values)
    attrs.extend(ProductAttribute(kind="attribute", name=k, value=v) for k, v in product.attributes.items())
    return attrs


def _store(
    db: Session,
    job: ScrapeJob,
    source: Source,
    page: ParsedPage,
    valid: list[ExtractedProduct],
    rejected: list[RejectedProduct],
    understanding: PageUnderstanding,
    raw_products: list,
    data_types: set[DataType],
    llm: LLMClient,
) -> tuple[list[Product], list[ExtractedProduct], int]:
    # A re-upload updates the job in place: matching products are merged and nothing stored earlier is removed.
    # Returns (rows written, their product data, how many were merged into existing products).
    now = _now()
    values = dict(
        understanding=understanding.model_dump(mode="json"),
        raw_products=[p for p in raw_products if isinstance(p, dict)],
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
    job.chunks = []  # re-indexed below when the source is long

    rows: list[Product] = []
    stored: list[ExtractedProduct] = []
    merged_count = 0
    for product in valid:
        row = next((p for p in job.products if same_product(p.name, product.name)), None)
        if row is None:
            row = Product(job=job, name=product.name)
            db.add(row)
        else:
            product = merge_product(ExtractedProduct.model_validate(row.data), product)
            merged_count += 1
        row.source = source
        row.provider = (product.provider or "")[:255] or None
        row.category = product.category.value
        row.category_confidence = product.category_confidence
        row.classification_method = product.classification_method
        row.description = product.description
        row.confidence = product.confidence
        row.extraction_method = product.extraction_method
        row.data = product.model_dump(mode="json")
        row.attributes = _attributes_for(product)
        row.references = [*row.references, SourceReference(url=str(product.source_url), snippet=product.source_snippet,
                                                             content_hash=page.content_hash, retrieved_at=now)]
        rows.append(row)
        stored.append(product)
    db.commit()
    return rows, stored, merged_count
