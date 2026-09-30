# Headings matter: grounding attributes each fact to the section that names the product, so the
# loaders recover headings from Word styles, Markdown markers and PDF font sizes.

from __future__ import annotations

import hashlib
import re
from collections import Counter
from io import BytesIO
from pathlib import Path

import pandas as pd

from app.config import Settings
from app.scraper.parser import (
    ExtractedTable,
    ParsedPage,
    TextBlock,
    _table_lines,
    deduplicate,
    find_figures,
    frame_to_table,
    parse_html,
)

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md", ".markdown", ".html", ".htm"}

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")
_SETEXT = re.compile(r"^(=+|-+)\s*$")
_BULLET = re.compile(r"^\s*(?:[•▪◦‣■□●○◆◇►▶✓✔]\s*|(?:[-*+]|\d{1,2}[.)])\s+)(.+)$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")


class DocumentError(ValueError):
    pass


def resolve_folder(folder: str, settings: Settings) -> Path:
    try:
        path = Path(folder.strip().strip('"')).expanduser().resolve(strict=True)
    except OSError:
        raise DocumentError(f"Folder not found: {folder}") from None
    if not path.is_dir():
        raise DocumentError(f"Not a folder: {folder}")
    roots = [Path(root).expanduser().resolve() for root in settings.document_roots]
    if roots and not any(path.is_relative_to(root) for root in roots):
        raise DocumentError("Folder is outside the allowed DOCUMENT_ROOTS")
    return path


def list_documents(folder: Path, *, recursive: bool = False, limit: int = 100) -> list[Path]:
    candidates = folder.rglob("*") if recursive else folder.glob("*")
    files = sorted(
        p for p in candidates
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
        and not p.name.startswith(("~$", "."))  # Office lock files
        and p.resolve().is_relative_to(folder)  # no symlinks out of the folder
    )
    return files[:limit]


def load_document(path: Path, max_bytes: int = 20_000_000) -> ParsedPage:
    size = path.stat().st_size
    if size > max_bytes:
        raise DocumentError(f"{path.name} is {size / 1e6:.1f} MB (limit {max_bytes / 1e6:.0f} MB)")
    data = path.read_bytes()
    uri = path.resolve().as_uri()
    suffix = path.suffix.lower()

    if suffix in (".html", ".htm"):
        page = parse_html(data.decode("utf-8", errors="replace"), uri)
    else:
        if suffix == ".pdf":
            blocks, tables, title, extra = _load_pdf(data)
        elif suffix == ".docx":
            blocks, tables, title, extra = _load_docx(data)
        else:
            blocks, tables = text_to_blocks(data.decode("utf-8-sig", errors="replace"))
            title, extra = None, {}
        page = _make_page(uri, title or _first_heading(blocks) or path.stem, blocks, tables, data)
        page.metadata.update(extra)
    if not page.blocks and not page.tables:
        raise DocumentError(f"{path.name} has no extractable text (scanned PDFs need OCR first)")
    page.metadata.update(source="document", filename=path.name, file_type=suffix.lstrip("."), bytes=size)
    return page


def _first_heading(blocks: list[TextBlock]) -> str | None:
    return next((b.text for b in blocks if b.kind == "heading"), None)


def _make_page(uri: str, title: str, blocks: list[TextBlock], tables: list[ExtractedTable], raw: bytes) -> ParsedPage:
    deduped = deduplicate(blocks)
    body = "\n".join(b.text for b in deduped)
    return ParsedPage(
        url=uri,
        title=title[:500],
        metadata={"url": uri, "title": title[:500]},
        json_ld=[],
        blocks=deduped,
        tables=tables,
        figures=find_figures(body + "\n" + _table_lines(tables)),
        content_hash=hashlib.sha256(raw).hexdigest(),
        stats={"duplicate_blocks_removed": len(blocks) - len(deduped), "tables": len(tables)},
    )


def _rows_to_table(rows: list[list[str]], caption: str | None = None) -> ExtractedTable | None:
    rows = [r for r in rows if any(cell.strip() for cell in r)]
    if len(rows) < 2:
        return None
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    frame = pd.DataFrame([[c or None for c in r] for r in rows[1:]], columns=rows[0])
    return frame_to_table(frame, caption)


def text_to_blocks(text: str) -> tuple[list[TextBlock], list[ExtractedTable]]:
    blocks: list[TextBlock] = []
    tables: list[ExtractedTable] = []
    paragraph: list[str] = []
    table_rows: list[list[str]] = []
    lines = text.replace("\r\n", "\n").split("\n")

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(TextBlock("paragraph", " ".join(paragraph)))
            paragraph.clear()

    def flush_table() -> None:
        if table_rows:
            caption = blocks[-1].text if blocks and blocks[-1].kind == "heading" else None
            if table := _rows_to_table(table_rows, caption):
                tables.append(table)
            table_rows.clear()

    for raw in lines:
        line = re.sub(r"^(?:>\s?)+", "", raw.strip())  # Markdown block quotes
        if _TABLE_ROW.match(line):
            flush_paragraph()
            if not _TABLE_RULE.match(line):
                table_rows.append([cell.strip() for cell in line.strip("|").split("|")])
            continue
        flush_table()
        if not line:
            flush_paragraph()
            continue
        if _SETEXT.match(line):
            if len(paragraph) == 1:  # "Title\n=====" heading
                blocks.append(TextBlock("heading", paragraph.pop(), level=1 if line.startswith("=") else 2))
            else:  # horizontal rule
                flush_paragraph()
            continue
        if match := _MD_HEADING.match(line):
            flush_paragraph()
            blocks.append(TextBlock("heading", match.group(2), level=len(match.group(1))))
        elif match := _BULLET.match(line):
            flush_paragraph()
            blocks.append(TextBlock("list_item", match.group(1)))
        elif _looks_like_caps_heading(line) and not paragraph:
            blocks.append(TextBlock("heading", line.title() if line.isupper() else line, level=2))
        else:
            paragraph.append(line)
    flush_paragraph()
    flush_table()
    return blocks, tables


def _looks_like_caps_heading(line: str) -> bool:
    letters = [c for c in line if c.isalpha()]
    return 3 <= len(letters) and len(line) <= 80 and line.isupper() and not line.endswith((".", ",", ";"))


def _load_docx(data: bytes) -> tuple[list[TextBlock], list[ExtractedTable], str | None, dict]:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = Document(BytesIO(data))
    blocks: list[TextBlock] = []
    tables: list[ExtractedTable] = []
    for element in document.element.body.iterchildren():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "p":
            paragraph = Paragraph(element, document)
            text = re.sub(r"\s+", " ", paragraph.text).strip()
            if not text:
                continue
            style = (paragraph.style.name if paragraph.style is not None else "") or ""
            if style == "Title":
                blocks.append(TextBlock("heading", text, level=1))
            elif match := re.match(r"Heading (\d)", style):
                blocks.append(TextBlock("heading", text, level=int(match.group(1))))
            elif style.startswith("List") or paragraph._p.pPr is not None and paragraph._p.pPr.numPr is not None:
                blocks.append(TextBlock("list_item", text))
            else:
                blocks.append(TextBlock("paragraph", text))
        elif tag == "tbl":
            table = Table(element, document)
            rows = [[re.sub(r"\s+", " ", cell.text).strip() for cell in row.cells] for row in table.rows]
            caption = blocks[-1].text if blocks and blocks[-1].kind == "heading" else None
            if extracted := _rows_to_table(rows, caption):
                tables.append(extracted)
    props = document.core_properties
    extra = {k: v for k, v in {"author": props.author, "subject": props.subject}.items() if v}
    return blocks, tables, props.title or None, extra


def _load_pdf(data: bytes) -> tuple[list[TextBlock], list[ExtractedTable], str | None, dict]:
    # extract_text keeps word spacing but not font sizes, and the visitor's text has the reverse
    # problem, so each extracted line is matched to its sizes by position in the non-space characters.
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(data))
    lines: list[tuple[str, float]] = []  # (text, effective font size; 0 = unknown)
    for page in reader.pages:
        chars: list[str] = []
        sizes: list[float] = []

        def visit(text, cm, tm, _font, font_size, chars=chars, sizes=sizes):
            size = font_size * abs(tm[3] or 1) * abs(cm[3] or 1)
            for char in text:
                if not char.isspace():
                    chars.append(char)
                    sizes.append(size)

        text = page.extract_text(visitor_text=visit) or ""
        stream = "".join(chars)
        cursor = 0
        for raw in text.split("\n"):
            line = re.sub(r"\s+", " ", raw).strip()
            if not line:
                lines.append(("", 0.0))
                continue
            key = re.sub(r"\s+", "", line)
            pos = stream.find(key, cursor)
            if pos >= 0:
                cursor = pos + len(key)
                lines.append((line, max(sizes[pos:cursor])))
            else:
                lines.append((line, 0.0))
        lines.append(("", 0.0))  # page break ends a paragraph

    blocks = _pdf_blocks(lines)
    meta_title = (reader.metadata or {}).get("/Title") if reader.metadata else None
    # PDF metadata titles are often file names or internal codes; a heading in the text reads better
    title = _first_heading(blocks) or (str(meta_title) if meta_title else None)
    return blocks, [], title, {"pages": len(reader.pages)}


def _pdf_blocks(lines: list[tuple[str, float]]) -> list[TextBlock]:
    weights = Counter()
    for text, size in lines:
        if text and size > 0:
            weights[round(size, 1)] += len(text)
    body_size = weights.most_common(1)[0][0] if weights else 0.0
    heading_sizes = sorted({round(s, 1) for t, s in lines
                            if t and body_size and s >= body_size * 1.15 and len(t) <= 120}, reverse=True)
    level_of = {size: min(i + 1, 6) for i, size in enumerate(heading_sizes)}

    blocks: list[TextBlock] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            blocks.append(TextBlock("paragraph", " ".join(paragraph)))
            paragraph.clear()

    for text, size in lines:
        if not text:
            flush()
        elif round(size, 1) in level_of:
            flush()
            blocks.append(TextBlock("heading", text, level=level_of[round(size, 1)]))
        elif match := _BULLET.match(text):
            flush()
            blocks.append(TextBlock("list_item", match.group(1)))
        elif blocks and blocks[-1].kind == "list_item" and not paragraph and text[:1].islower():
            blocks[-1].text += " " + text  # wrapped bullet
        else:
            paragraph.append(text)
            if text.endswith((".", ":", "!", "?")):
                flush()
    flush()
    return blocks
