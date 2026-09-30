from __future__ import annotations

import logging
import re
import tomllib
from dataclasses import dataclass, fields, replace
from pathlib import Path

import httpx
import numpy as np

from app.config import Settings
from app.scraper.parser import ExtractedTable, ParsedPage, TextBlock

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RagConfig:
    chunk_chars: int = 1500
    top_k: int = 3
    embedding_provider: str = "ollama"
    embedding_model: str = "bge-m3"
    embedding_base_url: str = "http://localhost:11434"
    embedding_timeout: float = 180.0


def load_rag_config(settings: Settings) -> RagConfig:
    path = Path(settings.llm_config_file)
    if not path.is_file():
        return RagConfig()
    with path.open("rb") as handle:
        values = tomllib.load(handle).get("rag") or {}
    known = {f.name for f in fields(RagConfig)}
    unknown = set(values) - known
    if unknown:
        raise ValueError(f"{path}: unknown setting(s) {sorted(unknown)} in [rag]")
    return RagConfig(**values)


@dataclass
class Chunk:
    index: int
    heading: str | None
    text: str

    def labelled(self) -> str:
        return f"[passage {self.index + 1}{f' · {self.heading}' if self.heading else ''}]\n{self.text}"


def _block_line(block: TextBlock) -> str:
    if block.kind == "heading":
        return f"{'#' * max(block.level, 1)} {block.text}"
    return f"- {block.text}" if block.kind == "list_item" else block.text


def _split_long(text: str, limit: int) -> list[str]:
    sentences = re.split(r"(?<=[.!?;])\s+", text)
    pieces, current = [], ""
    for sentence in sentences:
        while len(sentence) > limit:
            pieces.append(sentence[:limit])
            sentence = sentence[limit:]
        if current and len(current) + len(sentence) + 1 > limit:
            pieces.append(current)
            current = ""
        current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current)
    return pieces


def chunk_page(page: ParsedPage, chunk_chars: int = 1500) -> list[Chunk]:
    chunks: list[Chunk] = []
    heading: str | None = None
    lines: list[str] = []

    def flush() -> None:
        if lines:
            chunks.append(Chunk(len(chunks), heading, "\n".join(lines)))
            lines.clear()

    for block in page.blocks:
        if block.kind == "heading":
            flush()
            heading = block.text
            continue
        for piece in _split_long(_block_line(block), chunk_chars):
            if lines and sum(len(line) + 1 for line in lines) + len(piece) > chunk_chars:
                flush()
            lines.append(piece)
    flush()

    for number, table in enumerate(page.tables, 1):
        heading = table.caption or f"Table {number}"
        for row in table.rows:
            line = " | ".join(f"{k}: {v}" for k, v in row.items() if v)
            if line:
                if lines and sum(len(x) + 1 for x in lines) + len(line) > chunk_chars:
                    flush()
                lines.append(line[:chunk_chars])
        flush()
    return chunks


def _table_size(table: ExtractedTable) -> int:
    return len(table.to_csv(len(table.rows))) + 40


def split_parts(page: ParsedPage, max_chars: int) -> list[ParsedPage]:
    """Consecutive parts that each fit one prompt and together cover the whole source."""
    budget = max(2000, max_chars - 1500)  # room for the prompt header (URL, title, JSON-LD)
    parts: list[ParsedPage] = []
    current: list[TextBlock] = []
    size = 0

    def emit(blocks: list[TextBlock], tables: list[ExtractedTable]) -> None:
        parts.append(replace(page, blocks=list(blocks), tables=list(tables),
                             json_ld=page.json_ld if not parts else []))

    for block in page.blocks:
        pieces = _split_long(block.text, budget // 2) if len(block.text) > budget // 2 else [block.text]
        for text in pieces:
            piece = TextBlock(block.kind, text, block.level)
            if current and size + len(text) + 1 > budget:
                emit(current, [])
                # repeat the current heading so the next part keeps its context
                last_heading = next((b for b in reversed(current) if b.kind == "heading"), None)
                current = [last_heading] if last_heading and piece.kind != "heading" else []
                size = sum(len(b.text) + 1 for b in current)
            current.append(piece)
            size += len(text) + 1
    if current or not parts:
        emit(current, [])

    tables: list[ExtractedTable] = []
    table_size = 0
    for table in page.tables:
        for start in range(0, max(len(table.rows), 1), 200):
            piece = replace(table, rows=table.rows[start : start + 200])
            piece_size = _table_size(piece)
            if tables and table_size + piece_size > budget:
                emit([], tables)
                tables, table_size = [], 0
            tables.append(piece)
            table_size += piece_size
    if tables:
        last = parts[-1]
        text_size = sum(len(b.text) + 1 for b in last.blocks)
        if not last.tables and text_size + table_size <= budget:
            parts[-1] = replace(last, tables=tables)
        else:
            emit([], tables)
    return parts


def source_length(page: ParsedPage) -> int:
    return len(page.to_prompt(10**9))


def _normalise(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.where(norms == 0, 1, norms)


async def _ollama_embed(texts: list[str], config: RagConfig) -> np.ndarray:
    async with httpx.AsyncClient(base_url=config.embedding_base_url, timeout=config.embedding_timeout) as client:
        response = await client.post("/api/embed", json={"model": config.embedding_model, "input": texts})
        if response.status_code == 404:
            raise RuntimeError(f"embedding model {config.embedding_model!r} not found "
                               f"(run: ollama pull {config.embedding_model})")
        response.raise_for_status()
        vectors = response.json().get("embeddings") or []
    if len(vectors) != len(texts):
        raise RuntimeError("embedding response did not match the input")
    return np.asarray(vectors, dtype=np.float32)


def _tfidf(texts: list[str]) -> np.ndarray:
    from sklearn.feature_extraction.text import TfidfVectorizer

    try:
        vectorizer = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, stop_words="english")
        return vectorizer.fit_transform(texts).toarray().astype(np.float32)
    except ValueError:  # every word was a stop word
        return TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True).fit_transform(texts).toarray().astype(np.float32)


@dataclass
class RagIndex:
    chunks: list[Chunk]
    vectors: np.ndarray  # rows are normalised, so a dot product is cosine similarity
    query_vectors: np.ndarray
    queries: list[str]
    method: str  # "ollama:bge-m3" or "tfidf"

    def retrieve(self, top_k: int, budget_chars: int) -> list[Chunk]:
        """Top chunks for every query within a character budget, returned in document order."""
        scores = self.query_vectors @ self.vectors.T
        ranked: list[int] = []
        for row in scores:
            for index in np.argsort(-row)[:top_k]:
                if int(index) not in ranked:
                    ranked.append(int(index))
        chosen, used = [], 0
        for index in ranked:
            size = len(self.chunks[index].labelled()) + 2
            if used + size > budget_chars:
                continue
            chosen.append(index)
            used += size
        return [self.chunks[i] for i in sorted(chosen)]


async def build_index(page: ParsedPage, queries: list[str], config: RagConfig) -> RagIndex:
    """Embed every chunk plus the queries in one call (one model load on small GPUs)."""
    chunks = chunk_page(page, config.chunk_chars)
    texts = [f"{c.heading}\n{c.text}" if c.heading else c.text for c in chunks] + queries
    method = "tfidf"
    matrix = None
    if config.embedding_provider == "ollama" and chunks:
        try:
            matrix = await _ollama_embed(texts, config)
            method = f"ollama:{config.embedding_model}"
        except Exception as exc:  # Ollama down, model not pulled, timeout...
            log.warning("Embedding with %s failed (%s); using TF-IDF retrieval", config.embedding_model, exc)
    if matrix is None:
        matrix = _tfidf(texts) if chunks else np.zeros((len(texts), 1), dtype=np.float32)
    matrix = _normalise(matrix)
    return RagIndex(chunks, matrix[: len(chunks)], matrix[len(chunks) :], queries, method)
