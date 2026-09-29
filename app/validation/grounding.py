# LLMs (small local ones especially) invent figures or move a fee from one product to another, so
# every numeric fact must appear in the part of the source about *that* product: its section, table
# row or JSON-LD entry. Facts that don't are removed, listed in `unverified`, and lower the confidence.

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.scraper.parser import ParsedPage, find_financial_figures
from app.validation.schemas import PageReport, parse_number

NUMBER_RE = re.compile(r"(?<![\d.,])\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|(?<![\d.,])\d+(?:\.\d+)?")
ZERO_WORDS = re.compile(r"\b(no|free|nil|zero|waived)\b", re.IGNORECASE)
MAX_LLM_CONFIDENCE = 0.95


STOPWORDS = {
    "the", "and", "for", "with", "from", "your", "you", "this", "that", "are", "our", "all", "any", "not",
    "can", "has", "have", "will", "per", "into", "on", "of", "to", "in", "a", "an", "or", "is", "be", "by", "at",
    "card", "account", "product",
}


def _words(text: str) -> set[str]:
    """Meaningful word stems (first 5 letters) so 'purchases' matches 'purchase'."""
    return {w[:5] for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in STOPWORDS}


def numbers_in(text: str) -> set[float]:
    numbers = {round(float(m.group().replace(",", "")), 4) for m in NUMBER_RE.finditer(text)}
    numbers |= {round(f.value, 4) for f in find_financial_figures(text, limit=1000)}  # "$25k" -> 25000
    return numbers


def _contains(numbers: set[float], value: float) -> bool:
    return any(abs(value - n) < 0.005 for n in numbers)


def _name_pattern(name: str) -> re.Pattern[str] | None:
    """Exact name, or its first two significant words close together (e.g. "Platinum Rewards ... Card")."""
    tokens = [t for t in re.findall(r"[a-z0-9]+", name.lower()) if len(t) > 2]
    if not tokens:
        return None
    if len(tokens) == 1:
        return re.compile(r"\b" + re.escape(tokens[0]) + r"\b", re.IGNORECASE)
    return re.compile(r"\b" + re.escape(tokens[0]) + r"\W+(?:\w+\W+){0,2}?" + re.escape(tokens[1]) + r"\b",
                      re.IGNORECASE)


@dataclass
class PageEvidence:
    segments: list[str]  # sections, table rows, JSON-LD items, title/description
    numbers: set[float]

    @classmethod
    def from_page(cls, page: ParsedPage) -> PageEvidence:
        segments = [f"{page.title}\n{page.metadata.get('description', '')}"]
        segments += [f"{s.heading or ''}\n{s.text}" for s in page.sections]
        segments += [" | ".join(f"{k}: {v}" for k, v in row.items() if v) for t in page.tables for row in t.rows]
        segments += [json.dumps(item, ensure_ascii=False) for item in page.json_ld]
        return cls(segments=segments, numbers=numbers_in("\n".join(segments)))

    def context_for(self, name: str) -> str | None:
        exact = name.lower().strip()
        matches = [s for s in self.segments if exact and exact in s.lower()]
        if not matches and (pattern := _name_pattern(name)):
            matches = [s for s in self.segments if pattern.search(s)]
        return "\n".join(matches) if matches else None


def ground_product(product: dict[str, Any], evidence: PageEvidence) -> dict[str, Any]:
    name = str(product.get("name") or "")
    context = evidence.context_for(name)
    local = numbers_in(context) if context else evidence.numbers
    zero_ok = bool(ZERO_WORDS.search(context or "")) or _contains(local, 0)
    unverified: list[str] = []
    checked = verified = 0

    def supported(value: float) -> bool:
        return zero_ok if value == 0 else _contains(local, value)

    rates = []
    for rate in product.get("rates") or []:
        values = [parse_number(rate.get(k)) for k in ("value", "min", "max")] if isinstance(rate, dict) else []
        values = [v for v in values if v is not None]
        if not values:
            rates.append(rate)  # validation decides
            continue
        checked += 1
        if all(supported(v) for v in values):
            verified += 1
            rates.append(rate)
        else:
            unverified.append(f"{rate.get('label') or 'Rate'}: {', '.join(f'{v:g}%' for v in values)}")
    product["rates"] = rates

    fees = []
    for fee in product.get("fees") or []:
        if not isinstance(fee, dict):
            fees.append(fee)
            continue
        values = [v for v in (parse_number(fee.get("amount")), parse_number(fee.get("percentage"))) if v is not None]
        if not values:
            fees.append(fee)
            continue
        checked += 1
        if all(supported(v) for v in values):
            verified += 1
            fees.append(fee)
        else:
            unverified.append(f"{fee.get('label') or 'Fee'}: {', '.join(f'{v:g}' for v in values)}")
    product["fees"] = fees

    # Text facts must share most of their meaningful words with this product's part of the page
    if context is not None:
        context_words = _words(context)
        for key, label in (("features", "feature"), ("eligibility", "eligibility"), ("conditions", "condition")):
            items = product.get(key)
            if not isinstance(items, list):
                continue
            kept_items = []
            for item in items:
                words = _words(str(item))
                if not words or len(words & context_words) / len(words) >= 0.5:
                    kept_items.append(item)
                else:
                    unverified.append(f"{label}: {item}")
            product[key] = kept_items

    for key in ("min_amount", "max_amount"):
        value = parse_number(product.get(key))
        if value is None:
            continue
        checked += 1
        if supported(value):
            verified += 1
        else:
            unverified.append(f"{key.replace('_', ' ')}: {value:g}")
            product[key] = None

    confidence = parse_number(product.get("confidence"))
    confidence = min(confidence if confidence is not None and confidence <= 1 else 0.5, MAX_LLM_CONFIDENCE)
    if checked:
        confidence *= 0.5 + 0.5 * verified / checked
    if context is None:
        confidence *= 0.7
        unverified.append("product name not found on the page")
    product["confidence"] = round(confidence, 3)
    product["unverified"] = unverified
    return product


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def _supported(text: str, evidence: PageEvidence) -> bool:
    return all(_contains(evidence.numbers, f.value) or f.value == 0 for f in find_financial_figures(text))


def ground_report(report: PageReport, evidence: PageEvidence) -> tuple[PageReport, int]:
    """Report text isn't tied to one product, so a number only has to appear anywhere in the source."""
    removed = 0

    def keep(items: list, text_of=lambda item: item) -> list:
        nonlocal removed
        kept = [item for item in items if _supported(text_of(item), evidence)]
        removed += len(items) - len(kept)
        return kept

    def keep_sentences(text: str) -> str:
        paragraphs = []
        for paragraph in text.split("\n\n"):
            sentences = keep(_SENTENCE_END.split(paragraph.strip()))
            if sentences:
                paragraphs.append(" ".join(sentences))
        return "\n\n".join(paragraphs)

    updates: dict[str, object] = {
        key: keep(getattr(report, key))
        for key in ("key_points", "products_overview", "rates_and_fees", "eligibility_and_conditions",
                    "things_to_watch")
    }
    updates["detailed_summary"] = keep_sentences(report.detailed_summary)
    sections = []
    for section in report.sections:
        summary = keep_sentences(section.summary)
        if summary:
            sections.append(section.model_copy(update={"summary": summary}))
    updates["sections"] = sections
    figures = [f for f in report.key_figures if all(_contains(evidence.numbers, n) for n in numbers_in(f.value))]
    removed += len(report.key_figures) - len(figures)
    updates["key_figures"] = figures
    updates["part_notes"] = [keep(notes) for notes in report.part_notes]
    return report.model_copy(update=updates), removed
