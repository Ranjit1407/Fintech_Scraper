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
    institution: Mapped[str | None] = mapped_column(String(255))
    api_available: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_scraped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    jobs: Mapped[list[ScrapeJob]] = relationship(back_populates="source")
    products: Mapped[list[Product]] = relationship(back_populates="source")


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
    products: Mapped[list[Product]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="Product.id"
    )
    extraction: Mapped[ExtractionResult | None] = relationship(
        back_populates="job", cascade="all, delete-orphan", uselist=False
    )
    chunks: Mapped[list[DocumentChunk]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="DocumentChunk.idx"
    )

    @property
    def product_count(self) -> int:
        return len(self.products)


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("scrape_jobs.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), index=True)
    name: Mapped[str] = mapped_column(String(300), index=True)
    provider: Mapped[str | None] = mapped_column(String(255))
    category: Mapped[str] = mapped_column(String(40), index=True)
    category_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    classification_method: Mapped[str | None] = mapped_column(String(60))
    description: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    extraction_method: Mapped[str | None] = mapped_column(String(40))
    data: Mapped[dict] = mapped_column(JSONType)  # ExtractedProduct
    summary: Mapped[dict | None] = mapped_column(JSONType)  # ProductSummary
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    job: Mapped[ScrapeJob] = relationship(back_populates="products")
    source: Mapped[Source] = relationship(back_populates="products")
    attributes: Mapped[list[ProductAttribute]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )
    references: Mapped[list[SourceReference]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )


class ProductAttribute(Base):
    # Rates, fees and other facts as rows, so they can be queried across products.
    __tablename__ = "product_attributes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(30), index=True)
    name: Mapped[str] = mapped_column(String(255))
    value: Mapped[str] = mapped_column(Text)
    numeric_value: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(20))

    product: Mapped[Product] = relationship(back_populates="attributes")


class ExtractionResult(Base):
    __tablename__ = "extraction_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("scrape_jobs.id", ondelete="CASCADE"), unique=True)
    understanding: Mapped[dict | None] = mapped_column(JSONType)
    page_report: Mapped[dict | None] = mapped_column(JSONType)  # PageReport
    raw_products: Mapped[list | None] = mapped_column(JSONType)
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


class SourceReference(Base):
    __tablename__ = "source_references"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(String(2048))
    snippet: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    product: Mapped[Product] = relationship(back_populates="references")
