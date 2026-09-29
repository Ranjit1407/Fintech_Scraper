# LLM output is messy ("19.99%", "high", "credit_card", nested objects), so the product schemas
# coerce what they safely can and reject what they cannot.

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
    model_validator,
)


class ProductCategory(str, Enum):
    LOAN = "Loan"
    CREDIT_CARD = "Credit Card"
    INSURANCE = "Insurance"
    INVESTMENT = "Investment"
    BANKING = "Banking"
    PAYMENT = "Payment"
    OTHER = "Other"


class DataType(str, Enum):
    RATES = "rates"
    FEES = "fees"
    AMOUNTS = "amounts"
    ELIGIBILITY = "eligibility"
    FEATURES = "features"
    CONDITIONS = "conditions"
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


_CATEGORY_ALIASES: dict[str, ProductCategory] = {
    "loans": ProductCategory.LOAN,
    "lending": ProductCategory.LOAN,
    "mortgage": ProductCategory.LOAN,
    "mortgages": ProductCategory.LOAN,
    "credit cards": ProductCategory.CREDIT_CARD,
    "creditcard": ProductCategory.CREDIT_CARD,
    "card": ProductCategory.CREDIT_CARD,
    "cards": ProductCategory.CREDIT_CARD,
    "investments": ProductCategory.INVESTMENT,
    "wealth": ProductCategory.INVESTMENT,
    "bank": ProductCategory.BANKING,
    "bank account": ProductCategory.BANKING,
    "savings": ProductCategory.BANKING,
    "deposit": ProductCategory.BANKING,
    "deposits": ProductCategory.BANKING,
    "account": ProductCategory.BANKING,
    "payments": ProductCategory.PAYMENT,
    "wallet": ProductCategory.PAYMENT,
}


def coerce_category(value: Any) -> ProductCategory | None:
    if value is None:
        return None
    if isinstance(value, ProductCategory):
        return value
    key = re.sub(r"[\s_-]+", " ", str(value)).strip().lower()
    for category in ProductCategory:
        if category.value.lower() == key:
            return category
    return _CATEGORY_ALIASES.get(key)


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


_CURRENCY_SYMBOLS = {
    "$": "USD", "US$": "USD", "€": "EUR", "£": "GBP", "₹": "INR", "RS": "INR", "RS.": "INR",
    "¥": "JPY", "A$": "AUD", "C$": "CAD", "S$": "SGD",
}


def normalize_currency(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip().upper()
    if text in _CURRENCY_SYMBOLS:
        return _CURRENCY_SYMBOLS[text]
    return text if re.fullmatch(r"[A-Z]{3}", text) else None


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


def _clean_optional_str(value: Any) -> str | None:
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
        text = _clean_optional_str(item)
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text[:500])
    return out[:25]


def _clean_attributes(value: Any) -> dict[str, str]:
    if isinstance(value, list):  # [{"name": ..., "value": ...}]
        value = {
            item.get("name") or item.get("key"): item.get("value")
            for item in value
            if isinstance(item, dict) and (item.get("name") or item.get("key"))
        }
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for key, raw in value.items():
        text = _clean_optional_str(raw)
        if key and text:
            out[str(key).strip()[:100]] = text[:500]
        if len(out) >= 50:
            break
    return out


Number = Annotated[float | None, BeforeValidator(parse_number)]
Currency = Annotated[str | None, BeforeValidator(normalize_currency)]
OptStr = Annotated[str | None, BeforeValidator(_clean_optional_str)]
StrList = Annotated[list[str], BeforeValidator(_clean_str_list)]
Confidence = Annotated[float, BeforeValidator(_coerce_confidence), Field(ge=0.0, le=1.0)]


def _fmt_amount(amount: float, currency: str | None) -> str:
    text = f"{amount:,.2f}".removesuffix(".00")
    return f"{currency} {text}" if currency else text


class Rate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    label: str = "Rate"
    value: Number = None
    min: Number = None
    max: Number = None
    period: OptStr = None

    @field_validator("label", mode="before")
    @classmethod
    def _label(cls, value: Any) -> str:
        return _clean_optional_str(value) or "Rate"

    @field_validator("value", "min", "max")
    @classmethod
    def _range(cls, value: float | None) -> float | None:
        if value is not None and not 0 <= value <= 1000:
            raise ValueError("rate must be between 0 and 1000 percent")
        return value

    @model_validator(mode="after")
    def _has_value(self) -> Rate:
        if self.value is None and self.min is None and self.max is None:
            raise ValueError("rate has no numeric value")
        if self.min is not None and self.max is not None and self.min > self.max:
            self.min, self.max = self.max, self.min
        return self

    def display(self) -> str:
        if self.value is not None:
            figure = f"{self.value:g}%"
        elif self.min is not None and self.max is not None:
            figure = f"{self.min:g}% – {self.max:g}%"
        elif self.min is not None:
            figure = f"from {self.min:g}%"
        else:
            figure = f"up to {self.max:g}%"
        suffix = f" ({self.period})" if self.period else ""
        return f"{self.label}: {figure}{suffix}"


class Fee(BaseModel):
    model_config = ConfigDict(extra="ignore")

    label: str = "Fee"
    amount: Number = None
    currency: Currency = None
    percentage: Number = None
    frequency: OptStr = None
    description: OptStr = None

    @field_validator("label", mode="before")
    @classmethod
    def _label(cls, value: Any) -> str:
        return _clean_optional_str(value) or "Fee"

    @field_validator("amount")
    @classmethod
    def _amount(cls, value: float | None) -> float | None:
        if value is not None and value < 0:
            raise ValueError("fee amount cannot be negative")
        return value

    @field_validator("percentage")
    @classmethod
    def _percentage(cls, value: float | None) -> float | None:
        if value is not None and not 0 <= value <= 100:
            raise ValueError("fee percentage must be between 0 and 100")
        return value

    @model_validator(mode="after")
    def _has_content(self) -> Fee:
        if self.amount is None and self.percentage is None and self.description is None:
            raise ValueError("fee has no amount, percentage or description")
        return self

    def display(self) -> str:
        parts = []
        if self.amount is not None:
            parts.append(_fmt_amount(self.amount, self.currency))
        if self.percentage is not None:
            parts.append(f"{self.percentage:g}%")
        if not parts and self.description:
            parts.append(self.description)
        text = f"{self.label}: {' + '.join(parts)}"
        return f"{text} ({self.frequency})" if self.frequency else text


def _keep_valid(model: type[BaseModel]):
    """Validate list items individually and drop invalid ones instead of rejecting the product."""

    def validator(value: Any) -> list[Any]:
        if value is None:
            return []
        if not isinstance(value, list):
            value = [value]
        items = []
        for item in value:
            if isinstance(item, (int, float, str)):
                item = {"value": item} if model is Rate else {"description": item}
            try:
                items.append(model.model_validate(item))
            except ValidationError:
                continue
        return items[:20]

    return validator


def describe_rates(rates: list[Any] | None) -> list[str]:
    return [r.display() for r in _keep_valid(Rate)(rates or [])]


def describe_fees(fees: list[Any] | None) -> list[str]:
    return [f.display() for f in _keep_valid(Fee)(fees or [])]


FIELD_GROUPS: dict[DataType, dict[str, Any]] = {
    DataType.RATES: {"rates": []},
    DataType.FEES: {"fees": []},
    DataType.AMOUNTS: {"min_amount": None, "max_amount": None, "currency": None, "tenure": None},
    DataType.ELIGIBILITY: {"eligibility": []},
    DataType.FEATURES: {"features": [], "attributes": {}},
    DataType.CONDITIONS: {"conditions": []},
}


class ExtractedProduct(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=2, max_length=300)
    provider: OptStr = None
    category: ProductCategory = ProductCategory.OTHER
    category_confidence: Confidence = 0.0
    classification_method: OptStr = None
    description: OptStr = None
    rates: Annotated[list[Rate], BeforeValidator(_keep_valid(Rate))] = Field(default_factory=list)
    fees: Annotated[list[Fee], BeforeValidator(_keep_valid(Fee))] = Field(default_factory=list)
    min_amount: Number = None
    max_amount: Number = None
    currency: Currency = None
    tenure: OptStr = None
    eligibility: StrList = Field(default_factory=list)
    features: StrList = Field(default_factory=list)
    conditions: StrList = Field(default_factory=list)
    attributes: Annotated[dict[str, str], BeforeValidator(_clean_attributes)] = Field(default_factory=dict)
    confidence: Confidence = 0.5
    source_url: HttpUrl | FileUrl
    source_snippet: OptStr = None
    extraction_method: str = "unknown"
    unverified: StrList = Field(default_factory=list)  # facts removed by grounding

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, value: Any) -> str:
        text = _clean_optional_str(value)
        if text is None:
            raise ValueError("product name is required")
        return text

    @field_validator("category", mode="before")
    @classmethod
    def _category(cls, value: Any) -> ProductCategory:
        return coerce_category(value) or ProductCategory.OTHER

    @field_validator("description")
    @classmethod
    def _description(cls, value: str | None) -> str | None:
        return value[:2000] if value else value

    @field_validator("source_snippet")
    @classmethod
    def _snippet(cls, value: str | None) -> str | None:
        return value[:500] if value else value

    @field_validator("min_amount", "max_amount")
    @classmethod
    def _non_negative(cls, value: float | None) -> float | None:
        return None if value is not None and value < 0 else value

    @model_validator(mode="after")
    def _amount_order(self) -> ExtractedProduct:
        if self.min_amount is not None and self.max_amount is not None and self.min_amount > self.max_amount:
            self.min_amount, self.max_amount = self.max_amount, self.min_amount
        return self

    def restrict_to(self, data_types: set[DataType]) -> ExtractedProduct:
        update: dict[str, Any] = {}
        for data_type, defaults in FIELD_GROUPS.items():
            if data_type not in data_types:
                update.update(defaults)
        return self.model_copy(update=update) if update else self


class RejectedProduct(BaseModel):
    name: str | None = None
    reason: str
    errors: list[str] = Field(default_factory=list)


def _name_key(name: str) -> str:
    return re.sub(r"\W+", " ", name.lower()).strip()


def validate_products(
    raw_products: list[Any],
    *,
    source_url: str,
    min_confidence: float,
    data_types: set[DataType],
) -> tuple[list[ExtractedProduct], list[RejectedProduct]]:
    valid: dict[str, ExtractedProduct] = {}
    rejected: list[RejectedProduct] = []

    for item in raw_products:
        if not isinstance(item, dict):
            rejected.append(RejectedProduct(reason="extraction output is not an object"))
            continue
        name = _clean_optional_str(item.get("name"))
        try:
            product = ExtractedProduct.model_validate({**item, "source_url": source_url})
        except ValidationError as exc:
            errors = [f"{'.'.join(str(p) for p in e['loc']) or 'product'}: {e['msg']}" for e in exc.errors()]
            rejected.append(RejectedProduct(name=name and name[:300], reason="schema validation failed", errors=errors[:10]))
            continue

        if product.confidence < min_confidence:
            rejected.append(
                RejectedProduct(
                    name=product.name,
                    reason=f"confidence {product.confidence:.2f} below threshold {min_confidence:.2f}",
                )
            )
            continue

        product = product.restrict_to(data_types)
        key = _name_key(product.name)
        existing = valid.get(key)
        if existing is None:
            valid[key] = product
        elif product.confidence > existing.confidence:
            rejected.append(RejectedProduct(name=existing.name, reason="duplicate of a higher-confidence product"))
            valid[key] = product
        else:
            rejected.append(RejectedProduct(name=product.name, reason="duplicate of a higher-confidence product"))

    return list(valid.values()), rejected


PAGE_TYPES = {"product_detail", "product_listing", "comparison", "rates_table", "article", "legal", "other"}


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
        if isinstance(item, dict) and (name := _clean_optional_str(item.get("name"))):
            out.append({"name": name[:200], "type": str(item.get("type") or "other")[:40]})
    return out[:50]


class PageUnderstanding(BaseModel):
    model_config = ConfigDict(extra="ignore")

    page_type: str = "other"
    institution: OptStr = None
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


class ProductSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    summary: str = ""
    key_features: StrList = Field(default_factory=list)
    fees_and_rates: StrList = Field(default_factory=list)
    eligibility: StrList = Field(default_factory=list)
    important_conditions: StrList = Field(default_factory=list)
    method: str = "template"

    @field_validator("summary", mode="before")
    @classmethod
    def _summary(cls, value: Any) -> str:
        return (_clean_optional_str(value) or "")[:3000]


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
            cleaned = {k: _clean_optional_str(item.get(k)) for k in model.model_fields}
            if all(cleaned.get(k) for k in text_fields):
                out.append({k: (v[:1500] if isinstance(v, str) else v) for k, v in cleaned.items()})
        return out[:limit]

    return validator


class PageReport(BaseModel):
    model_config = ConfigDict(extra="ignore")

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
    products_overview: StrList = Field(default_factory=list)
    rates_and_fees: StrList = Field(default_factory=list)
    eligibility_and_conditions: StrList = Field(default_factory=list)
    things_to_watch: StrList = Field(default_factory=list)
    data_gaps: StrList = Field(default_factory=list)
    method: str = "basic"  # llm | basic
    model: str | None = None

    @field_validator("headline", mode="before")
    @classmethod
    def _headline(cls, value: Any) -> str:
        return (_clean_optional_str(value) or "")[:200]

    @field_validator("overview", mode="before")
    @classmethod
    def _overview(cls, value: Any) -> str:
        return (_clean_optional_str(value) or "")[:4000]


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


class ScrapeRequest(BaseModel):
    url: HttpUrl
    data_types: list[DataType] = Field(default_factory=lambda: list(DataType))
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
        return list(dict.fromkeys(value)) or list(DataType)


class DocumentFolderRequest(BaseModel):
    """Process every supported document (PDF, DOCX, TXT, MD, HTML) in a folder: one job per file."""

    folder: str = Field(min_length=1, description="Folder path on the machine running the API")
    recursive: bool = False
    data_types: list[DataType] = Field(default_factory=lambda: list(DataType))
    llm_profile: str | None = None

    @field_validator("data_types")
    @classmethod
    def _data_types(cls, value: list[DataType]) -> list[DataType]:
        return list(dict.fromkeys(value)) or list(DataType)


class PreviousDocument(BaseModel):
    """An earlier job for the same document: processing the upload updates it instead of adding a new one."""

    job_id: str
    match: Literal["identical", "same_name"]  # same content, or same file name with new content
    status: JobStatus
    product_count: int
    processed_at: datetime


class UploadedFile(BaseModel):
    name: str  # path as chosen in the browser, e.g. "brochures/cards.pdf"
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
    data_types: list[DataType] = Field(default_factory=lambda: list(DataType))
    llm_profile: str | None = None
    update_existing: bool = True

    @field_validator("data_types")
    @classmethod
    def _data_types(cls, value: list[DataType]) -> list[DataType]:
        return list(dict.fromkeys(value)) or list(DataType)


class DocumentJobRequest(BaseModel):
    source: Literal["document"] = "document"
    document_path: str
    data_types: list[DataType] = Field(default_factory=lambda: list(DataType))
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
    product_count: int = 0


class ReferenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    url: str
    snippet: str | None = None
    content_hash: str | None = None
    retrieved_at: datetime


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: str
    name: str
    provider: str | None = None
    category: str
    category_confidence: float
    classification_method: str | None = None
    description: str | None = None
    confidence: float
    extraction_method: str | None = None
    data: dict[str, Any]
    summary: dict[str, Any] | None = None
    references: list[ReferenceOut] = Field(default_factory=list)
    created_at: datetime


class JobDetail(JobOut):
    request: dict[str, Any]
    compliance: dict[str, Any] | None = None
    events: list[dict[str, Any]] = Field(default_factory=list)
    products: list[ProductOut] = Field(default_factory=list)
    understanding: dict[str, Any] | None = None
    page_report: dict[str, Any] | None = None
    rejected: list[dict[str, Any]] = Field(default_factory=list)
    tables: list[dict[str, Any]] | None = None
    page_metadata: dict[str, Any] | None = None
    rag: dict[str, Any] | None = None
