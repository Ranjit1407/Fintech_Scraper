from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass, field
from io import StringIO
from typing import Any, Callable, Iterator
from urllib.parse import urljoin

import pandas as pd
from bs4 import BeautifulSoup, Comment, Tag

log = logging.getLogger(__name__)

HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
BLOCK_TAGS = HEADING_TAGS + ("p", "li", "dt", "dd", "blockquote", "figcaption", "summary")
CONTAINER_TAGS = ("div", "section", "article", "main", "span")
LEAF_BREAKERS = BLOCK_TAGS + ("div", "section", "article", "main", "table", "ul", "ol", "dl")

NOISE_TAGS = [
    "script", "style", "noscript", "template", "svg", "canvas", "iframe", "object", "embed",
    "nav", "aside", "form", "button", "dialog", "link", "meta",
]
NOISE_ROLES = {"navigation", "banner", "contentinfo", "complementary", "search", "dialog", "alertdialog"}
NOISE_TOKENS = {
    "ad", "ads", "advert", "advertisement", "sponsored", "cookie", "cookies", "consent", "gdpr",
    "popup", "modal", "newsletter", "breadcrumb", "breadcrumbs", "navbar", "nav", "menu", "sidebar",
    "social", "share", "sharing", "skip-link", "outbrain", "taboola",
}
NOISE_PREFIX = re.compile(
    r"^(?:ad|ads|advert|advertisement|sponsored|cookie|cookies|consent|gdpr|popup|modal|newsletter|"
    r"breadcrumbs?|navbar|sidebar|social|sharing|taboola|outbrain)[-_]",
    re.IGNORECASE,
)
BOILERPLATE = {
    "read more", "learn more", "find out more", "apply now", "click here", "see details", "show more",
    "back to top", "skip to content", "skip to main content", "menu", "close",
}
IGNORED_LD_TYPES = {"BreadcrumbList", "WebSite", "WebPage", "SiteNavigationElement", "ImageObject", "SearchAction"}
MAX_TABLES = 25
MAX_TABLE_ROWS = 500

_CURRENCY_CODES = {
    "$": "USD", "US$": "USD", "A$": "AUD", "C$": "CAD", "S$": "SGD", "€": "EUR", "£": "GBP",
    "₹": "INR", "RS": "INR", "RS.": "INR", "¥": "JPY",
}
_AMOUNT_MULTIPLIERS = {
    "k": 1e3, "m": 1e6, "bn": 1e9, "million": 1e6, "billion": 1e9,
    "lakh": 1e5, "lakhs": 1e5, "crore": 1e7, "crores": 1e7,
}
_NUM = r"\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?"
AMOUNT_RE = re.compile(
    r"(?<![A-Za-z])(?P<cur>US\$|A\$|C\$|S\$|\$|€|£|₹|¥|Rs\.?|INR|USD|EUR|GBP|AUD|CAD|SGD)\s?"
    rf"(?P<num>{_NUM})(?:\s?(?P<mult>k|m|bn|million|billion|lakhs?|crores?)\b)?",
    re.IGNORECASE,
)
AMOUNT_SUFFIX_RE = re.compile(rf"(?<![\w.])(?P<num>{_NUM})\s?(?P<cur>USD|EUR|GBP|INR|AUD|CAD|SGD)\b")
PERCENT_RE = re.compile(r"(?<![\d.])(?P<num>\d{1,3}(?:\.\d{1,3})?)\s?%")


@dataclass
class TextBlock:
    kind: str  # heading | paragraph | list_item
    text: str
    level: int = 0


@dataclass
class Section:
    heading: str | None
    level: int
    blocks: list[TextBlock] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(b.text for b in self.blocks)


@dataclass
class ExtractedTable:
    columns: list[str]
    rows: list[dict[str, str | None]]
    caption: str | None = None

    def to_csv(self, max_rows: int = 30) -> str:
        return pd.DataFrame(self.rows[:max_rows], columns=self.columns).to_csv(index=False)


@dataclass
class Figure:
    kind: str  # percentage | amount
    value: float
    raw: str
    context: str
    currency: str | None = None


@dataclass
class Link:
    url: str
    text: str
    rel: str = ""


@dataclass
class ParsedPage:
    url: str
    title: str
    metadata: dict[str, Any]
    json_ld: list[dict[str, Any]]
    blocks: list[TextBlock]
    tables: list[ExtractedTable]
    figures: list[Figure]
    content_hash: str
    stats: dict[str, int] = field(default_factory=dict)

    @property
    def sections(self) -> list[Section]:
        sections = [Section(heading=None, level=0)]
        for block in self.blocks:
            if block.kind == "heading":
                sections.append(Section(heading=block.text, level=block.level))
            else:
                sections[-1].blocks.append(block)
        return [s for s in sections if s.heading or s.blocks]

    @property
    def text(self) -> str:
        lines = []
        for block in self.blocks:
            if block.kind == "heading":
                lines.append(f"{'#' * max(block.level, 1)} {block.text}")
            elif block.kind == "list_item":
                lines.append(f"- {block.text}")
            else:
                lines.append(block.text)
        return "\n".join(lines)

    def to_prompt(self, max_chars: int = 15_000) -> str:
        header_lines = [f"URL: {self.url}", f"TITLE: {self.title}"]
        description = self.metadata.get("description") or self.metadata.get("og:description")
        if description:
            header_lines.append(f"DESCRIPTION: {description}")
        parts = ["\n".join(header_lines)]

        structured = [item for item in self.json_ld if not _ld_types(item) & IGNORED_LD_TYPES]
        if structured:
            parts.append("STRUCTURED DATA (JSON-LD):\n" + _truncate(json.dumps(structured, ensure_ascii=False), 2500))

        tables_text = "\n\n".join(
            f"Table {i + 1}{f' ({t.caption})' if t.caption else ''}:\n{t.to_csv(25)}"
            for i, t in enumerate(self.tables[:8])
        )
        budget = max(1000, max_chars - sum(len(p) for p in parts))
        body = self.text
        body_budget = min(len(body), budget - min(len(tables_text), budget // 3))
        parts.append("CONTENT:\n" + _truncate(body, body_budget))
        if tables_text:
            parts.append("TABLES (CSV):\n" + _truncate(tables_text, budget - body_budget))
        return "\n\n".join(parts)

    def redact(self, redactor: Callable[[str], tuple[str, int]]) -> int:
        total = 0
        for block in self.blocks:
            block.text, count = redactor(block.text)
            total += count
        for table in self.tables:
            for row in table.rows:
                for key, value in row.items():
                    if value:
                        row[key], count = redactor(value)
                        total += count
        return total


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 15)] + "\n[truncated]"


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def make_soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:  # lxml missing or choked on the document
        return BeautifulSoup(html, "html.parser")


_META_KEYS = {"description", "keywords", "author", "copyright", "rights", "dc.rights", "dcterms.rights", "robots"}


def extract_metadata(soup: BeautifulSoup, url: str) -> dict[str, Any]:
    meta: dict[str, Any] = {"url": url}
    if soup.title and soup.title.string:
        meta["title"] = _collapse(soup.title.string)
    for tag in soup.find_all("meta"):
        key = tag.get("name") or tag.get("property") or tag.get("itemprop")
        content = tag.get("content")
        if not key or not content:
            continue
        key = key.strip().lower()
        if key in _META_KEYS or key.startswith(("og:", "article:", "twitter:")):
            meta[key] = _collapse(content)[:1000]
    canonical = soup.find("link", rel="canonical")
    if canonical and canonical.get("href"):
        meta["canonical"] = urljoin(url, canonical["href"])
    if soup.html and soup.html.get("lang"):
        meta["lang"] = soup.html["lang"]
    return meta


def _ld_types(item: dict[str, Any]) -> set[str]:
    types = item.get("@type", [])
    return {types} if isinstance(types, str) else {t for t in types if isinstance(t, str)}


def _flatten_ld(data: Any) -> Iterator[dict[str, Any]]:
    if isinstance(data, list):
        for item in data:
            yield from _flatten_ld(item)
    elif isinstance(data, dict):
        if "@graph" in data:
            yield from _flatten_ld(data["@graph"])
        if "@type" in data:
            yield data


def extract_json_ld(soup: BeautifulSoup) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text()
        try:
            items.extend(_flatten_ld(json.loads(raw)))
        except (json.JSONDecodeError, TypeError):
            continue
    return items[:50]


def extract_links(soup: BeautifulSoup, base_url: str) -> list[Link]:
    links: list[Link] = []
    seen: set[str] = set()
    for tag in soup.find_all(["a", "link"], href=True):
        href = tag["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        url = urljoin(base_url, href).split("#")[0]
        rel = " ".join(tag.get("rel") or [])
        if tag.name == "link" and "license" not in rel:
            continue
        key = f"{url}|{rel}"
        if key in seen:
            continue
        seen.add(key)
        links.append(Link(url=url, text=_collapse(tag.get_text(" ")), rel=rel))
    return links


def _is_noise(tag: Tag) -> bool:
    # Hidden elements are deliberately kept: collapsed accordions and tabs often hold details such as
    # prices, dates and conditions.
    if tag.get("role") in NOISE_ROLES:
        return True
    tokens = list(tag.get("class") or [])
    if tag.get("id"):
        tokens.append(tag["id"])
    return any(t.lower() in NOISE_TOKENS or NOISE_PREFIX.match(t) for t in tokens)


def clean_soup(soup: BeautifulSoup) -> int:
    removed = 0
    for tag in soup.find_all(NOISE_TAGS):
        if not tag.decomposed:
            tag.decompose()
            removed += 1
    for tag in soup.find_all(["header", "footer"]):
        # Keep headers/footers that belong to an article; drop the site-wide ones.
        if not tag.decomposed and tag.find_parent(["article", "main"]) is None:
            tag.decompose()
            removed += 1
    for tag in soup.find_all(True):
        if tag.decomposed or tag.name in ("html", "body", "main", "article"):
            continue
        if _is_noise(tag):
            tag.decompose()
            removed += 1
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()
    return removed


def _cell(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)[:500]
    text = _collapse(str(value))
    return text or None


def _column_names(columns: Any) -> list[str]:
    names: list[str] = []
    for i, col in enumerate(columns):
        if isinstance(col, tuple):
            parts = [str(p) for p in col if not str(p).startswith("Unnamed")]
            name = " / ".join(dict.fromkeys(parts))
        elif isinstance(col, (int, float)):
            name = ""
        else:
            name = "" if str(col).startswith("Unnamed") else str(col)
        name = _collapse(name) or f"column_{i + 1}"
        base, n = name, 2
        while name in names:
            name = f"{base} ({n})"
            n += 1
        names.append(name)
    return names


def frame_to_table(df: pd.DataFrame, caption: str | None = None) -> ExtractedTable | None:
    df = df.dropna(how="all").dropna(axis=1, how="all")
    if df.shape[0] < 1 or df.shape[1] < 2:
        return None
    columns = _column_names(df.columns)
    rows = [
        {col: _cell(value) for col, value in zip(columns, record)}
        for record in df.itertuples(index=False, name=None)
    ]
    return ExtractedTable(columns=columns, rows=rows[:MAX_TABLE_ROWS], caption=caption)


def extract_tables(soup: BeautifulSoup) -> list[ExtractedTable]:
    tables: list[ExtractedTable] = []
    for element in soup.find_all("table"):
        if element.find_parent("table") is not None:
            continue
        try:
            frames = pd.read_html(StringIO(str(element)))
        except (ValueError, ImportError, IndexError) as exc:
            log.debug("Skipping unparseable table: %s", exc)
            continue
        caption = _collapse(element.caption.get_text(" ")) if element.caption else None
        for frame in frames:
            table = frame_to_table(frame, caption)
            if table:
                tables.append(table)
        if len(tables) >= MAX_TABLES:
            break
    return tables


def extract_blocks(soup: BeautifulSoup) -> list[TextBlock]:
    root = soup.body or soup
    selected: set[int] = set()
    blocks: list[TextBlock] = []
    for element in root.find_all(BLOCK_TAGS + CONTAINER_TAGS):
        if element.find_parent("table") is not None:
            continue  # tables are extracted separately
        if any(id(parent) in selected for parent in element.parents):
            continue
        if element.name in CONTAINER_TAGS and element.find(LEAF_BREAKERS) is not None:
            continue  # only leaf containers carry their own text
        text = _collapse(element.get_text(" "))
        if not text:
            continue
        selected.add(id(element))
        if element.name in HEADING_TAGS:
            blocks.append(TextBlock("heading", text, level=int(element.name[1])))
        elif element.name in ("li", "dt", "dd"):
            blocks.append(TextBlock("list_item", text))
        else:
            blocks.append(TextBlock("paragraph", text))
    return blocks


def deduplicate(blocks: list[TextBlock]) -> list[TextBlock]:
    # Headings define sections, so they are always kept. Short facts like "Free delivery" legitimately
    # repeat under different items, so they are dropped only when repeated back-to-back.
    seen: set[str] = set()
    previous = ""
    out: list[TextBlock] = []
    for block in blocks:
        key = re.sub(r"\W+", " ", block.text.lower()).strip()
        if not key:
            continue
        if block.kind != "heading":
            if key in BOILERPLATE or key == previous or (len(key) >= 40 and key in seen):
                continue
        seen.add(key)
        previous = key
        out.append(block)
    return out


def _figure_context(text: str, start: int, end: int) -> str:
    return _collapse(text[max(0, start - 60) : end + 30])


def find_figures(text: str, limit: int = 200) -> list[Figure]:
    figures: list[Figure] = []
    for match in PERCENT_RE.finditer(text):
        figures.append(
            Figure("percentage", float(match.group("num")), match.group(0),
                            _figure_context(text, match.start(), match.end()))
        )
    for regex in (AMOUNT_RE, AMOUNT_SUFFIX_RE):
        for match in regex.finditer(text):
            value = float(match.group("num").replace(",", ""))
            multiplier = match.groupdict().get("mult")
            if multiplier:
                value *= _AMOUNT_MULTIPLIERS[multiplier.lower()]
            code = match.group("cur").upper()
            figures.append(
                Figure("amount", value, match.group(0), _figure_context(text, match.start(), match.end()),
                                currency=_CURRENCY_CODES.get(code, code))
            )
    return figures[:limit]


def _table_lines(tables: list[ExtractedTable]) -> str:
    return "\n".join(
        " | ".join(f"{k}: {v}" for k, v in row.items() if v) for table in tables for row in table.rows
    )


def parse_html(html: str, url: str) -> ParsedPage:
    soup = make_soup(html)
    metadata = extract_metadata(soup, url)
    json_ld = extract_json_ld(soup)
    h1 = soup.find("h1")
    h1_text = _collapse(h1.get_text(" ")) if h1 else ""

    removed = clean_soup(soup)
    tables = extract_tables(soup)
    raw_blocks = extract_blocks(soup)
    blocks = deduplicate(raw_blocks)

    body_text = "\n".join(b.text for b in blocks)
    figures = find_figures(body_text + "\n" + _table_lines(tables))
    title = metadata.get("og:title") or metadata.get("title") or h1_text or url
    return ParsedPage(
        url=url,
        title=title[:500],
        metadata=metadata,
        json_ld=json_ld,
        blocks=blocks,
        tables=tables,
        figures=figures,
        content_hash=hashlib.sha256((body_text + _table_lines(tables)).encode("utf-8")).hexdigest(),
        stats={
            "noise_elements_removed": removed,
            "duplicate_blocks_removed": len(raw_blocks) - len(blocks),
            "tables": len(tables),
        },
    )


def _largest_record_list(data: Any, depth: int = 0) -> list[dict[str, Any]] | None:
    """Find the biggest list of objects in a JSON document (the API's 'records')."""
    best: list[dict[str, Any]] | None = None
    if isinstance(data, list):
        records = [item for item in data if isinstance(item, dict)]
        if records:
            best = records
    if depth < 4:
        children = data.values() if isinstance(data, dict) else data if isinstance(data, list) else []
        for child in children:
            if isinstance(child, (dict, list)):
                candidate = _largest_record_list(child, depth + 1)
                if candidate and (best is None or len(candidate) > len(best)):
                    best = candidate
    return best


def _scalar_items(data: Any, prefix: str = "", skip: int | None = None) -> Iterator[tuple[str, str]]:
    if isinstance(data, dict):
        for key, value in data.items():
            yield from _scalar_items(value, f"{prefix}.{key}" if prefix else str(key), skip)
    elif isinstance(data, list):
        if id(data) == skip:
            return
        for i, value in enumerate(data[:50]):
            yield from _scalar_items(value, f"{prefix}[{i}]", skip)
    elif data is not None and str(data).strip():
        yield prefix, str(data).strip()


def parse_json_payload(data: Any, url: str) -> ParsedPage:
    """Turn an official API's JSON response into the same structure as a parsed web page."""
    tables: list[ExtractedTable] = []
    records = _largest_record_list(data)
    records_id = None
    if records:
        table = frame_to_table(pd.json_normalize(records[:1000], max_level=2), caption="API records")
        if table:
            tables.append(table)
            # the records list is represented by the table; don't repeat it as text
            records_id = next(
                (id(v) for v in _walk_lists(data) if isinstance(v, list) and v and v[0] is records[0]), None
            )

    blocks = [TextBlock("paragraph", f"{path}: {value[:500]}")
              for path, value in _scalar_items(data, skip=records_id)][:300]
    title = str(data.get("title") or data.get("name") or "") if isinstance(data, dict) else ""
    text = "\n".join(b.text for b in blocks)
    return ParsedPage(
        url=url,
        title=title or f"API response from {url}",
        metadata={"url": url, "source": "api"},
        json_ld=[],
        blocks=blocks,
        tables=tables,
        figures=find_figures(text + "\n" + _table_lines(tables)),
        content_hash=hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode("utf-8")).hexdigest(),
        stats={"tables": len(tables)},
    )


def _walk_lists(data: Any, depth: int = 0) -> Iterator[Any]:
    if isinstance(data, list):
        yield data
    if depth < 4:
        children = data.values() if isinstance(data, dict) else data if isinstance(data, list) else []
        for child in children:
            if isinstance(child, (dict, list)):
                yield from _walk_lists(child, depth + 1)
