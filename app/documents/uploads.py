from __future__ import annotations

import hashlib
import json
import re
import shutil
import uuid
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database.models import ScrapeJob
from app.documents.loader import SUPPORTED_EXTENSIONS, DocumentError
from app.validation.schemas import PreviousDocument, SkippedFile, UploadBatch, UploadedFile

_BATCH_ID_RE = re.compile(r"[0-9a-f]{32}")
_UNSAFE_CHARS = re.compile(r"[^\w.\-() ]+")
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
PROCESSED_MARKER = ".processed"  # hidden, so list_documents ignores it
MANIFEST = ".manifest.json"
_CHUNK = 1024 * 1024


def _stored_name(relative: PurePosixPath) -> str:
    # Browser-supplied names are never used as paths (traversal): files are stored flat.
    # 'brochures/2026/cards.pdf' (chosen folder 'brochures') -> '2026__cards.pdf'
    parts = list(relative.parts[1:] if len(relative.parts) > 1 else relative.parts)
    cleaned = [_UNSAFE_CHARS.sub("_", p).strip(" .") for p in parts]
    name = "__".join(p for p in cleaned if p) or "document"
    stem, suffix = name[: -len(relative.suffix)] if relative.suffix else name, relative.suffix.lower()
    if stem.upper() in _WINDOWS_RESERVED:
        stem = f"_{stem}"
    return f"{stem[:120]}{suffix}"


def _unique(path: Path) -> Path:
    candidate, n = path, 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}-{n}{path.suffix}")
        n += 1
    return candidate


async def save_upload(files: list[UploadFile], settings: Settings) -> UploadBatch:
    batch_id = uuid.uuid4().hex
    folder = Path(settings.upload_dir) / batch_id
    folder.mkdir(parents=True)
    saved: list[UploadedFile] = []
    skipped: list[SkippedFile] = []

    for upload in files:
        name = (upload.filename or "").replace("\\", "/").lstrip("/")
        relative = PurePosixPath(name)
        if relative.name.startswith((".", "~$")):
            skipped.append(SkippedFile(name=name, reason="hidden or temporary file"))
            continue
        if relative.suffix.lower() not in SUPPORTED_EXTENSIONS:
            skipped.append(SkippedFile(name=name, reason="unsupported file type"))
            continue
        if len(saved) >= settings.max_documents_per_folder:
            skipped.append(SkippedFile(name=name, reason=f"over the limit of {settings.max_documents_per_folder} documents"))
            continue

        target = _unique(folder / _stored_name(relative))
        size = 0
        digest = hashlib.sha256()
        with target.open("wb") as out:
            while chunk := await upload.read(_CHUNK):
                size += len(chunk)
                if size > settings.max_document_bytes:
                    break
                digest.update(chunk)
                out.write(chunk)
        duplicate = next((f for f in saved if f.sha256 == digest.hexdigest()), None)
        if size > settings.max_document_bytes or size == 0 or duplicate:
            target.unlink()
            reason = ("empty file" if size == 0 else f"same content as {duplicate.name}" if duplicate
                      else f"larger than {settings.max_document_bytes / 1e6:.0f} MB")
            skipped.append(SkippedFile(name=name, reason=reason))
            continue
        saved.append(UploadedFile(name=name, bytes=size, sha256=digest.hexdigest(), stored_as=target.name))

    if not saved:
        shutil.rmtree(folder, ignore_errors=True)
        reasons = sorted({s.reason for s in skipped}) or ["no files received"]
        raise DocumentError(f"No documents could be uploaded ({'; '.join(reasons)}). "
                            f"Supported types: {', '.join(sorted(SUPPORTED_EXTENSIONS))}")
    (folder / MANIFEST).write_text(json.dumps([f.model_dump() for f in saved]), encoding="utf-8")
    return UploadBatch(batch_id=batch_id, files=saved, skipped=skipped)


def read_manifest(folder: Path) -> list[UploadedFile]:
    path = folder / MANIFEST
    if not path.is_file():
        return []
    return [UploadedFile.model_validate(item) for item in json.loads(path.read_text(encoding="utf-8"))]


def _display_name(name: str) -> str:
    return PurePosixPath(name.replace("\\", "/")).name.lower()


def find_previous_document(db: Session, sha256: str, name: str) -> tuple[ScrapeJob, str] | None:
    """The most recent job for the same document: identical content first, otherwise the same file name."""
    jobs = db.scalars(
        select(ScrapeJob).where(ScrapeJob.url.startswith("file:")).order_by(ScrapeJob.created_at.desc()).limit(1000)
    ).all()
    for job in jobs:
        if (job.request or {}).get("file_sha256") == sha256:
            return job, "identical"
    wanted = _display_name(name)
    for job in jobs:
        request = job.request or {}
        previous = request.get("original_name") or unquote(job.url.rsplit("/", 1)[-1])
        if _display_name(previous) == wanted:
            return job, "same_name"
    return None


def describe_previous(match: tuple[ScrapeJob, str] | None) -> PreviousDocument | None:
    if match is None:
        return None
    job, kind = match
    return PreviousDocument(job_id=job.id, match=kind, status=job.status, product_count=len(job.products),
                            processed_at=job.finished_at or job.created_at)


def batch_folder(batch_id: str, settings: Settings) -> Path:
    folder = Path(settings.upload_dir) / batch_id
    if not _BATCH_ID_RE.fullmatch(batch_id) or not folder.is_dir():
        raise DocumentError("Upload not found; it may have been cancelled")
    return folder


def mark_processed(folder: Path) -> None:
    (folder / PROCESSED_MARKER).touch()


def is_processed(folder: Path) -> bool:
    return (folder / PROCESSED_MARKER).exists()


def delete_batch(batch_id: str, settings: Settings) -> None:
    folder = batch_folder(batch_id, settings)
    if is_processed(folder):
        raise DocumentError("These documents have already been processed and are referenced by jobs")
    shutil.rmtree(folder)
