"""Run the pipeline on a website URL and/or a folder of documents."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from app.ai.llm_client import list_llm_profiles, llm_status, load_llm_profiles
from app.config import get_settings, setup_logging
from app.database.database import SessionLocal, init_db
from app.database.models import ScrapeJob
from app.documents.loader import DocumentError, list_documents, resolve_folder
from app.pipeline import run_job
from app.validation.schemas import DataType, DocumentJobRequest, JobStatus, ScrapeRequest


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def _profiles_command(_args: argparse.Namespace) -> int:
    listing = list_llm_profiles(get_settings())
    if listing.get("error"):
        return _fail(listing["error"])
    for profile in listing["profiles"]:
        marker = "*" if profile["name"] == listing["default"] else " "
        state = "configured" if profile["enabled"] else f"unavailable: {profile['reason']}"
        print(f"{marker} {profile['name']:<18} {profile['model'] or '-':<42} {state}")
        if profile["description"]:
            print(f"    {profile['description']}")
    print("\n* = default. Choose another with --profile NAME or LLM_PROFILE in .env.")
    return 0


def _create_jobs(args: argparse.Namespace, data_types: list[DataType], keywords: list[str]) -> list[str] | int:
    settings = get_settings()
    requests: list[tuple[str, dict]] = []
    if args.url:
        url = args.url if "://" in args.url else f"https://{args.url}"
        try:
            request = ScrapeRequest(url=url, keywords=keywords, data_types=data_types, render_mode=args.render,
                                    llm_profile=args.profile)
        except ValidationError as exc:
            return _fail(f"invalid URL {args.url!r}: {exc.errors()[0]['msg']}")
        requests.append((str(request.url), request.model_dump(mode="json")))
    if args.docs:
        try:
            folder = resolve_folder(args.docs, settings)
        except DocumentError as exc:
            return _fail(str(exc))
        files = list_documents(folder, recursive=args.recursive, limit=settings.max_documents_per_folder)
        if not files:
            return _fail(f"no supported documents in {folder}")
        print(f"Found {len(files)} document(s) in {folder}")
        for path in files:
            request = DocumentJobRequest(document_path=str(path), keywords=keywords, data_types=data_types,
                                         llm_profile=args.profile)
            requests.append((path.as_uri(), request.model_dump(mode="json")))

    with SessionLocal() as db:
        jobs = [ScrapeJob(url=url, request=body, status=JobStatus.QUEUED.value, events=[]) for url, body in requests]
        db.add_all(jobs)
        db.commit()
        return [job.id for job in jobs]


def _print_job(job: ScrapeJob) -> None:
    print(f"\n=== {job.url}")
    print(f"status: {job.status}" + (f"  ({job.error})" if job.error else ""))
    report = job.extraction.page_report if job.extraction else None
    for finding in (report or {}).get("keyword_findings") or []:
        passages = finding.get("passages") or []
        exact = sum(1 for p in passages if p.get("match") == "exact")
        print(f"  keyword '{finding['keyword']}': "
              + (f"{exact} exact, {len(passages) - exact} related passage(s)" if passages else "not found"))
        if finding.get("summary"):
            print(f"    {finding['summary']}")
        for point in (finding.get("points") or [])[:5]:
            print(f"    - {point}")
    items = sorted(job.items, key=lambda i: not i.data.get("matched_keywords"))
    for item in items:
        unverified = item.data.get("unverified") or []
        matched = item.data.get("matched_keywords") or []
        note = (f", matches {', '.join(matched)}" if matched else "") + (
            f", {len(unverified)} unsupported fact(s) removed" if unverified else "")
        print(f"  - {item.name} [{item.type}] confidence {item.confidence:.2f}{note}")
    if report:
        print(f"  report ({report.get('model') or 'extractive, no LLM'}): {report.get('headline') or ''}")
        if report.get("overview"):
            print(f"  {report['overview']}")


def _run_command(args: argparse.Namespace) -> int:
    if not args.url and not args.docs:
        return _fail("give --url, --docs or both")
    settings = get_settings()
    setup_logging(settings)
    init_db()
    if args.profile:
        profiles, _ = load_llm_profiles(settings.llm_config_file)
        if args.profile not in profiles:
            return _fail(f"unknown profile {args.profile!r}; run `python -m app.cli profiles`")
    status = llm_status(settings, args.profile)
    print(f"LLM: {status['model']} (profile {status['profile']})" if status["enabled"]
          else f"LLM: disabled ({status['reason']}); rule-based extraction only")

    try:
        data_types = [DataType(t.strip()) for t in args.data_types.split(",")] if args.data_types else list(DataType)
    except ValueError:
        return _fail(f"--data-types must be a comma-separated subset of {', '.join(t.value for t in DataType)}")
    try:
        keywords = ScrapeRequest.model_validate({"url": "https://example.com", "keywords": args.keywords or []}).keywords
    except ValidationError as exc:
        return _fail(f"--keywords: {exc.errors()[0]['msg'].removeprefix('Value error, ')}")
    job_ids = _create_jobs(args, data_types, keywords)
    if isinstance(job_ids, int):
        return job_ids

    for job_id in job_ids:  # one at a time: polite to websites and to free-model rate limits
        asyncio.run(run_job(job_id))

    from app.main import _job_detail

    out_dir = Path(args.out) if args.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
    ok = True
    with SessionLocal() as db:
        for job_id in job_ids:
            job = db.get(ScrapeJob, job_id)
            _print_job(job)
            ok &= job.status == JobStatus.COMPLETED.value
            if out_dir:
                path = out_dir / f"report-{job.id[:8]}.json"
                path.write_text(json.dumps(_job_detail(job).model_dump(mode="json"), indent=2, ensure_ascii=False),
                                encoding="utf-8")
                print(f"  saved {path}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="collect and extract data from a website and/or documents")
    run.add_argument("--url", help="web page to scrape, e.g. https://example.com/pricing")
    run.add_argument("--docs", help="folder of PDF, DOCX, TXT, MD or HTML documents")
    run.add_argument("--recursive", action="store_true", help="include documents in sub-folders")
    run.add_argument("--keywords", help='comma-separated keywords to look for first, e.g. "pricing, refund policy"')
    run.add_argument("--profile", help="LLM profile from config/llm.toml (default: active profile)")
    run.add_argument("--data-types", help="comma-separated: " + ",".join(t.value for t in DataType))
    run.add_argument("--render", choices=["auto", "http", "browser"], default="auto", help="website rendering")
    run.add_argument("--out", help="folder to save one JSON report per job")
    run.set_defaults(handler=_run_command)

    profiles = commands.add_parser("profiles", help="list LLM profiles and whether their API keys are set")
    profiles.set_defaults(handler=_profiles_command)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
