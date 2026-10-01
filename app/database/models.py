from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.database import Base

JSONType = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_job_id() -> str:
    return uuid.uuid4().hex


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url: Mapped[str] = mapped_column(String(2048), unique=True, index=True)
    domain: Mapped[str] = mapped_column(String(255), index=True)
    title: Mapped[str | None] = mapped_column(String(500))
    publisher: Mapped[str | None] = mapped_column("institution", String(255))  # column name kept for existing databases
    api_available: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_scraped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    jobs: Mapped[list[ScrapeJob]] = relationship(back_populates="source")
    items: Mapped[list[Item]] = relationship(back_populates="source")


class ScrapeJob(Base):
    __tablename__ = "scrape_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_job_id)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), index=True)
    url: Mapped[str] = mapped_column(String(2048))
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    request: Mapped[dict] = mapped_column(JSONType)
    compliance: Mapped[dict | None] = mapped_column(JSONType)
    fetch_method: Mapped[str | None] = mapped_column(String(20))
    error: Mapped[str | None] = mapped_column(Text)
    events: Mapped[list] = mapped_column(JSONType, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # last re-upload

    source: Mapped[Source | None] = relationship(back_populates="jobs")
    items: Mapped[list[Item]] = relationship(back_populates="job", cascade="all, delete-orphan", order_by="Item.id")
    extraction: Mapped[ExtractionResult | None] = relationship(
        back_populates="job", cascade="all, delete-orphan", uselist=False
    )
    chunks: Mapped[list[DocumentChunk]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="DocumentChunk.idx"
    )

    @property
    def item_count(self) -> int:
        return len(self.items)

    @property
    def retryable(self) -> bool:
        # Jobs saved by earlier versions (document uploads) hold a request with no web address, so there
        # is nothing to re-run. The retry endpoint revalidates in full and stays the authority.
        url = (self.request or {}).get("url")
        return isinstance(url, str) and url.startswith(("http://", "https://"))


class Item(Base):
    __tablename__ = "items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("scrape_jobs.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), index=True)
    name: Mapped[str] = mapped_column(String(300), index=True)
    type: Mapped[str] = mapped_column(String(40), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    extraction_method: Mapped[str | None] = mapped_column(String(40))
    data: Mapped[dict] = mapped_column(JSONType)  # ExtractedItem
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    job: Mapped[ScrapeJob] = relationship(back_populates="items")
    source: Mapped[Source] = relationship(back_populates="items")
    references: Mapped[list[ItemReference]] = relationship(back_populates="item", cascade="all, delete-orphan")


class ExtractionResult(Base):
    __tablename__ = "extraction_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("scrape_jobs.id", ondelete="CASCADE"), unique=True)
    understanding: Mapped[dict | None] = mapped_column(JSONType)
    page_report: Mapped[dict | None] = mapped_column(JSONType)  # PageReport
    raw_items: Mapped[list | None] = mapped_column(JSONType)
    rejected: Mapped[list | None] = mapped_column(JSONType)
    tables: Mapped[list | None] = mapped_column(JSONType)
    text: Mapped[str | None] = mapped_column(Text)
    page_metadata: Mapped[dict | None] = mapped_column(JSONType)
    llm_model: Mapped[str | None] = mapped_column(String(120))
    content_hash: Mapped[str | None] = mapped_column(String(64))
    rag: Mapped[dict | None] = mapped_column(JSONType)  # only for sources split into parts
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    job: Mapped[ScrapeJob] = relationship(back_populates="extraction")


class DocumentChunk(Base):
    __tablename__ = "document_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("scrape_jobs.id", ondelete="CASCADE"), index=True)
    idx: Mapped[int] = mapped_column(Integer)
    heading: Mapped[str | None] = mapped_column(String(500))
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list | None] = mapped_column(JSONType)
    embedding_model: Mapped[str | None] = mapped_column(String(120))
    retrieved: Mapped[bool] = mapped_column(Boolean, default=False)  # passed to the report prompt

    job: Mapped[ScrapeJob] = relationship(back_populates="chunks")


class ItemReference(Base):
    __tablename__ = "item_references"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(String(2048))
    snippet: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    item: Mapped[Item] = relationship(back_populates="references")
