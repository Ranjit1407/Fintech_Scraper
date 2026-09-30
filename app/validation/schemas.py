# LLM output is messy ("85%", "high", nested objects, lists where a string was asked for), so the
# schemas coerce what they safely can and reject what they cannot.

from __future__ import annotations

import ipaddress
import math
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    FileUrl,
    HttpUrl,
    ValidationError,
    field_validator,
)


class DataType(str, Enum):
    ITEMS = "items"
    TABLES = "tables"
    TEXT = "text"
    METADATA = "metadata"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"


class CheckStatus(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    SKIPPED = "skipped"


_MULTIPLIERS = {
    "k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6,
    "b": 1e9, "bn": 1e9, "billion": 1e9, "lakh": 1e5, "lakhs": 1e5, "crore": 1e7, "crores": 1e7,
}
_NUMBER_RE = re.compile(
    r"(-?\d[\d,]*(?:\.\d+)?|-?\.\d+)\s*(k|thousand|mn|m|million|bn|b|billion|lakhs?|crores?)?\b",
    re.IGNORECASE,
)


def parse_number(value: Any) -> float | None:
    """Parse '19.99%', '$1,500', '2.5k' or 7 into a float; None when not numeric."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if not isinstance(value, str):
        return None
    match = _NUMBER_RE.search(value)
    if not match:
        return None
    number = float(match.group(1).replace(",", ""))
    if match.group(2):
        number *= _MULTIPLIERS[match.group(2).lower()]
    return number


_CONFIDENCE_WORDS = {"very high": 0.95, "high": 0.9, "medium": 0.6, "moderate": 0.6, "low": 0.3, "very low": 0.1}


def _coerce_confidence(value: Any) -> Any:
    if value is None or value == "":
        return 0.5
    if isinstance(value, str):
        key = value.strip().lower()
        if key in _CONFIDENCE_WORDS:
            return _CONFIDENCE_WORDS[key]
        value = parse_number(value)
        if value is None:
            return 0.5
    if isinstance(value, (int, float)) and 1 < value <= 100:
        return value / 100  # "85" meaning 85%
    return value


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        value = "; ".join(str(v) for v in value if v is not None)
    elif isinstance(value, dict):
        value = "; ".join(f"{k}: {v}" for k, v in value.items() if v is not None)
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def _clean_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, dict)):
        value = [value]
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = clean_text(item)
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text[:500])
    return out[:25]


def clean_attributes(value: Any) -> dict[str, str]:
    if isinstance(value, list):  # [{"name": ..., "value": ...}]
        value = {
            item.get("name") or item.get("label") or item.get("key"): item.get("value")
            for item in value
            if isinstance(item, dict) and (item.get("name") or item.get("label") or item.get("key"))
        }
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for key, raw in value.items():
        text = clean_text(raw)
        if key and text:
            out[str(key).strip()[:100]] = text[:500]
        if len(out) >= 40:
            break
    return out


MAX_KEYWORDS = 10
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", re.IGNORECASE)
_ID_NUMBER_RE = re.compile(r"\d[\d\s().+/-]{5,}\d")


def _clean_keywords(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = re.split(r"[,;\n]", value)
    out: list[str] = []
    for raw in value:
        text = re.sub(r"\s+", " ", str(raw)).strip().strip("\"'")
        if not text or text.lower() in (k.lower() for k in out):
            continue
        if len(text) > 60:
            raise ValueError(f"keyword {text[:30]!r}… is too long (max 60 characters)")
        # Governance: keywords must not be used to look up individuals by their identifiers.
        if _EMAIL_RE.search(text) or any(sum(c.isdigit() for c in m.group()) >= 9 for m in _ID_NUMBER_RE.finditer(text)):
            raise ValueError(f"keyword {text!r} looks like a personal identifier (email address, phone or ID "
                             "number); searching for individuals' identifiers is not allowed")
        out.append(text)
    if len(out) > MAX_KEYWORDS:
        raise ValueError(f"at most {MAX_KEYWORDS} keywords are allowed")
    return out


OptStr = Annotated[str | None, BeforeValidator(clean_text)]
StrList = Annotated[list[str], BeforeValidator(_clean_str_list)]
Keywords = Annotated[list[str], BeforeValidator(_clean_keywords)]
Confidence = Annotated[float, BeforeValidator(_coerce_confidence), Field(ge=0.0, le=1.0)]
Attributes = Annotated[dict[str, str], BeforeValidator(clean_attributes)]


def normalize_type(value: Any) -> str:
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", str(value or ""))  # "CreditCard" -> "Credit Card"
    text = re.sub(r"[_\s-]+", " ", text).strip().lower()
    return text[:40] or "other"


class ExtractedItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=2, max_length=300)
    type: str = "other"
    description: OptStr = None
    attributes: Attributes = Field(default_factory=dict)
    confidence: Confidence = 0.5
    source_url: HttpUrl | FileUrl
    source_snippet: OptStr = None
    extraction_method: str = "unknown"
    matched_keywords: StrList = Field(default_factory=list)
    unverified: StrList = Field(default_factory=list)  # facts removed by grounding

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, value: Any) -> str:
        text = clean_text(value)
        if text is None:
            raise ValueError("item name is required")
        return text[:300]

    @field_validator("type", mode="before")
    @classmethod
    def _type(cls, value: Any) -> str:
        return normalize_type(value)

    @field_validator("description")
    @classmethod
    def _description(cls, value: str | None) -> str | None:
        return value[:2000] if value else value

    @field_validator("source_snippet")
    @classmethod
    def _snippet(cls, value: str | None) -> str | None:
        return value[:500] if value else value


class RejectedItem(BaseModel):
    name: str | None = None
    reason: str
    errors: list[str] = Field(default_factory=list)


def _name_key(name: str) -> str:
    return re.sub(r"\W+", " ", name.lower()).strip()


def validate_items(
    raw_items: list[Any], *, source_url: str, min_confidence: float,
) -> tuple[list[ExtractedItem], list[RejectedItem]]:
    valid: dict[str, ExtractedItem] = {}
    rejected: list[RejectedItem] = []

    for raw in raw_items:
        if not isinstance(raw, dict):
            rejected.append(RejectedItem(reason="extraction output is not an object"))
            continue
        name = clean_text(raw.get("name"))
        try:
            item = ExtractedItem.model_validate({**raw, "source_url": source_url})
        except ValidationError as exc:
            errors = [f"{'.'.join(str(p) for p in e['loc']) or 'item'}: {e['msg']}" for e in exc.errors()]
            rejected.append(RejectedItem(name=name and name[:300], reason="schema validation failed", errors=errors[:10]))
            continue

        if item.confidence < min_confidence:
            rejected.append(RejectedItem(
                name=item.name, reason=f"confidence {item.confidence:.2f} below threshold {min_confidence:.2f}"))
            continue

        key = _name_key(item.name)
        existing = valid.get(key)
        if existing is None:
            valid[key] = item
        elif item.confidence > existing.confidence:
            rejected.append(RejectedItem(name=existing.name, reason="duplicate of a higher-confidence item"))
            valid[key] = item
        else:
            rejected.append(RejectedItem(name=item.name, reason="duplicate of a higher-confidence item"))

    return list(valid.values()), rejected


PAGE_TYPES = {"article", "news", "blog_post", "product_page", "listing", "directory", "documentation", "homepage",
              "event", "profile", "forum", "report", "legal", "other"}


class Entity(BaseModel):
    name: str
    type: str = "other"


def _coerce_entities(value: Any) -> list[dict[str, str]]:
    if not value:
        return []
    out = []
    for item in value if isinstance(value, list) else [value]:
        if isinstance(item, str):
            item = {"name": item}
        if isinstance(item, dict) and (name := clean_text(item.get("name"))):
            out.append({"name": name[:200], "type": str(item.get("type") or "other")[:40]})
    return out[:50]


class PageUnderstanding(BaseModel):
    model_config = ConfigDict(extra="ignore")

    page_type: str = "other"
    publisher: OptStr = None
    topic: OptStr = None
    entities: Annotated[list[Entity], BeforeValidator(_coerce_entities)] = Field(default_factory=list)
    important_sections: StrList = Field(default_factory=list)
    language: OptStr = None
    method: str = "rules"

    @field_validator("page_type", mode="before")
    @classmethod
    def _page_type(cls, value: Any) -> str:
        key = re.sub(r"[\s-]+", "_", str(value or "")).strip().lower()
        return key if key in PAGE_TYPES else "other"


class ReportSection(BaseModel):
    title: str
    summary: str


class KeyFigure(BaseModel):
    label: str
    value: str
    context: str | None = None


def _coerce_items(model: type[BaseModel], text_fields: tuple[str, ...], limit: int):
    def validator(value: Any) -> list[dict[str, Any]]:
        items = value if isinstance(value, list) else [] if value is None else [value]
        out = []
        for item in items:
            if not isinstance(item, dict):
                continue
            cleaned = {k: clean_text(item.get(k)) for k in model.model_fields}
            if all(cleaned.get(k) for k in text_fields):
                out.append({k: (v[:1500] if isinstance(v, str) else v) for k, v in cleaned.items()})
        return out[:limit]

    return validator


class KeywordPassage(BaseModel):
    text: str
    heading: str | None = None
    match: Literal["exact", "related"]
    score: float | None = None


class KeywordFinding(BaseModel):
    keyword: str
    found: bool = False
    summary: str = ""
    points: StrList = Field(default_factory=list)
    passages: list[KeywordPassage] = Field(default_factory=list)
    items: list[str] = Field(default_factory=list)  # names of extracted items that mention the keyword
    method: str = "extract"  # llm | extract


class PageReport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    keyword_findings: list[KeywordFinding] = Field(default_factory=list)
    detailed_summary: str = ""
    sections: Annotated[list[ReportSection], BeforeValidator(_coerce_items(ReportSection, ("title", "summary"), 25))] = (
        Field(default_factory=list))
    key_figures: Annotated[list[KeyFigure], BeforeValidator(_coerce_items(KeyFigure, ("label", "value"), 40))] = (
        Field(default_factory=list))
    part_notes: list[StrList] = Field(default_factory=list)  # one list per part of a long source

    @field_validator("detailed_summary", mode="before")
    @classmethod
    def _detailed(cls, value: Any) -> str:
        if isinstance(value, list):
            value = "\n\n".join(str(v) for v in value if v)
        text = re.sub(r"[ \t]+", " ", str(value or "")).strip()
        return re.sub(r"\n{3,}", "\n\n", text)[:8000]

    headline: str = ""
    overview: str = ""
    audience: OptStr = None
    key_points: StrList = Field(default_factory=list)
    items_overview: StrList = Field(default_factory=list)
    things_to_watch: StrList = Field(default_factory=list)
    data_gaps: StrList = Field(default_factory=list)
    method: str = "basic"  # llm | basic
    model: str | None = None

    @field_validator("headline", mode="before")
    @classmethod
    def _headline(cls, value: Any) -> str:
        return (clean_text(value) or "")[:200]

    @field_validator("overview", mode="before")
    @classmethod
    def _overview(cls, value: Any) -> str:
        return (clean_text(value) or "")[:4000]


class ComplianceCheck(BaseModel):
    name: str
    status: CheckStatus
    summary: str
    details: dict[str, Any] = Field(default_factory=dict)


class ApiDiscovery(BaseModel):
    available: bool = False
    json_endpoint: str | None = None
    spec_urls: list[str] = Field(default_factory=list)
    portal_urls: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ComplianceReport(BaseModel):
    url: str
    allowed: bool
    checks: list[ComplianceCheck]
    api: ApiDiscovery | None = None
    request_interval: float | None = None
    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def blocked_reasons(self) -> list[str]:
        return [f"{c.name}: {c.summary}" for c in self.checks if c.status == CheckStatus.FAIL]


def _ensure_public_host(url: HttpUrl | None) -> HttpUrl | None:
    """Reject localhost / private addresses so the API cannot be used to probe internal networks."""
    from app.config import get_settings

    if url is None or get_settings().allow_private_hosts:
        return url
    host = (url.host or "").strip("[]").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("local addresses are not allowed")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return url
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        raise ValueError("private or reserved IP addresses are not allowed")
    return url


def _unique_data_types(value: list[DataType]) -> list[DataType]:
    return list(dict.fromkeys(value)) or list(DataType)


DataTypes = Annotated[list[DataType], Field(default_factory=lambda: list(DataType))]
KEYWORDS_DESCRIPTION = "Optional keywords to look for; their results are reported first"


class ScrapeRequest(BaseModel):
    url: HttpUrl
    keywords: Keywords = Field(default_factory=list, description=KEYWORDS_DESCRIPTION)
    data_types: DataTypes
    render_mode: Literal["auto", "http", "browser"] = "auto"
    api_endpoint: HttpUrl | None = Field(
        default=None, description="Official JSON API endpoint to use instead of scraping, if you have one."
    )
    llm_profile: str | None = Field(default=None, description="LLM profile from config/llm.toml (default: active)")

    @field_validator("url", "api_endpoint")
    @classmethod
    def _public(cls, value: HttpUrl | None) -> HttpUrl | None:
        return _ensure_public_host(value)

    @field_validator("data_types")
    @classmethod
    def _data_types(cls, value: list[DataType]) -> list[DataType]:
        return _unique_data_types(value)


class DocumentFolderRequest(BaseModel):
    """Process every supported document (PDF, DOCX, TXT, MD, HTML) in a folder: one job per file."""

    folder: str = Field(min_length=1, description="Folder path on the machine running the API")
    recursive: bool = False
    keywords: Keywords = Field(default_factory=list, description=KEYWORDS_DESCRIPTION)
    data_types: DataTypes
    llm_profile: str | None = None

    @field_validator("data_types")
    @classmethod
    def _data_types(cls, value: list[DataType]) -> list[DataType]:
        return _unique_data_types(value)


class PreviousDocument(BaseModel):
    """An earlier job for the same document: processing the upload updates it instead of adding a new one."""

    job_id: str
    match: Literal["identical", "same_name"]  # same content, or same file name with new content
    status: JobStatus
    item_count: int
    processed_at: datetime


class UploadedFile(BaseModel):
    name: str  # path as chosen in the browser, e.g. "brochures/guide.pdf"
    bytes: int
    sha256: str | None = None
    stored_as: str | None = None
    previous: PreviousDocument | None = None


class SkippedFile(BaseModel):
    name: str
    reason: str


class UploadBatch(BaseModel):
    """Documents uploaded from the dashboard, waiting for the user to confirm processing."""

    batch_id: str
    files: list[UploadedFile]
    skipped: list[SkippedFile] = Field(default_factory=list)


class ProcessUploadRequest(BaseModel):
    keywords: Keywords = Field(default_factory=list, description=KEYWORDS_DESCRIPTION)
    data_types: DataTypes
    llm_profile: str | None = None
    update_existing: bool = True

    @field_validator("data_types")
    @classmethod
    def _data_types(cls, value: list[DataType]) -> list[DataType]:
        return _unique_data_types(value)


class DocumentJobRequest(BaseModel):
    source: Literal["document"] = "document"
    document_path: str
    keywords: Keywords = Field(default_factory=list)
    data_types: DataTypes
    llm_profile: str | None = None
    original_name: str | None = None
    file_sha256: str | None = None
    update: bool = False  # re-upload: merge into the existing results
    previous_files: list[str] = Field(default_factory=list)


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    url: str
    status: JobStatus
    fetch_method: str | None = None
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime | None = None
    item_count: int = 0


class ReferenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    url: str
    snippet: str | None = None
    content_hash: str | None = None
    retrieved_at: datetime


class ItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: str
    name: str
    type: str
    description: str | None = None
    confidence: float
    extraction_method: str | None = None
    data: dict[str, Any]
    references: list[ReferenceOut] = Field(default_factory=list)
    created_at: datetime


class JobDetail(JobOut):
    request: dict[str, Any]
    compliance: dict[str, Any] | None = None
    events: list[dict[str, Any]] = Field(default_factory=list)
    items: list[ItemOut] = Field(default_factory=list)
    understanding: dict[str, Any] | None = None
    page_report: dict[str, Any] | None = None
    rejected: list[dict[str, Any]] = Field(default_factory=list)
    tables: list[dict[str, Any]] | None = None
    page_metadata: dict[str, Any] | None = None
    rag: dict[str, Any] | None = None
