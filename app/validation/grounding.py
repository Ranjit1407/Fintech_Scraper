# LLMs (small local ones especially) invent values or attach a fact to the wrong item, so every
# attribute must be supported by the part of the source about *that* item: its section, table row or
# JSON-LD entry. Facts that aren't are removed, listed in `unverified`, and lower the confidence.

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.scraper.parser import ParsedPage, find_figures
from app.validation.schemas import PageReport, clean_attributes, clean_text, parse_number

NUMBER_RE = re.compile(r"(?<![\d.,])\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|(?<![\d.,])\d+(?:\.\d+)?")
ZERO_WORDS = re.compile(r"\b(no|free|nil|zero|waived|none)\b", re.IGNORECASE)
MAX_LLM_CONFIDENCE = 0.95
MIN_WORD_OVERLAP = 0.5

STOPWORDS = {
    "the", "and", "for", "with", "from", "your", "you", "this", "that", "are", "our", "all", "any", "not",
    "can", "has", "have", "will", "per", "into", "on", "of", "to", "in", "a", "an", "or", "is", "be", "by", "at",
}


def _words(text: str) -> set[str]:
    """Meaningful word stems (first 5 letters) so 'purchases' matches 'purchase'."""
    return {w[:5] for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in STOPWORDS}


def numbers_in(text: str) -> set[float]:
    numbers = {round(float(m.group().replace(",", "")), 4) for m in NUMBER_RE.finditer(text)}
    numbers |= {round(f.value, 4) for f in find_figures(text, limit=1000)}  # "$25k" -> 25000
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
    words: set[str]
    vocabulary: set[str] = field(default_factory=set)  # every whole word in the source, lower-cased

    @classmethod
    def from_page(cls, page: ParsedPage) -> PageEvidence:
        segments = [f"{page.title}\n{page.metadata.get('description', '')}"]
        segments += [f"{s.heading or ''}\n{s.text}" for s in page.sections]
        segments += [" | ".join(f"{k}: {v}" for k, v in row.items() if v) for t in page.tables for row in t.rows]
        segments += [json.dumps(item, ensure_ascii=False) for item in page.json_ld]
        text = "\n".join(segments)
        return cls(segments=segments, numbers=numbers_in(text), words=_words(text),
                   vocabulary=set(re.findall(r"[a-z0-9]+", text.lower())))

    def context_for(self, name: str) -> str | None:
        exact = name.lower().strip()
        matches = [s for s in self.segments if exact and exact in s.lower()]
        if not matches and (pattern := _name_pattern(name)):
            matches = [s for s in self.segments if pattern.search(s)]
        return "\n".join(matches) if matches else None


def ground_item(item: dict[str, Any], evidence: PageEvidence) -> dict[str, Any]:
    context = evidence.context_for(str(item.get("name") or ""))
    numbers = numbers_in(context) if context else evidence.numbers
    words = _words(context) if context else evidence.words
    zero_ok = bool(ZERO_WORDS.search(context or "")) or _contains(numbers, 0)

    def numbers_supported(text: str) -> bool:
        return all(zero_ok if n == 0 else _contains(numbers, n) for n in numbers_in(text))

    def supported(text: str) -> bool:
        if numbers_in(text):
            return numbers_supported(text)
        text_words = _words(text)
        return not text_words or len(text_words & words) / len(text_words) >= MIN_WORD_OVERLAP

    unverified: list[str] = []
    checked = verified = 0
    attributes = {}
    for label, value in clean_attributes(item.get("attributes")).items():
        checked += 1
        if supported(value):
            verified += 1
            attributes[label] = value
        else:
            unverified.append(f"{label}: {value}")
    item["attributes"] = attributes

    # A description is the model's own sentence, so only the figures it quotes are checked.
    description = clean_text(item.get("description"))
    if description and not numbers_supported(description):
        unverified.append(f"description: {description}")
        item["description"] = None

    confidence = parse_number(item.get("confidence"))
    confidence = min(confidence if confidence is not None and confidence <= 1 else 0.5, MAX_LLM_CONFIDENCE)
    if checked:
        confidence *= 0.5 + 0.5 * verified / checked
    if context is None:
        confidence *= 0.7
        unverified.append("name not found in the source")
    item["confidence"] = round(confidence, 3)
    item["unverified"] = unverified
    return item


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_CAPITALISED = re.compile(r"\b[A-Z][A-Za-z]*(?:['’&.-][A-Za-z]+)*\b")


def _names(text: str) -> set[str]:
    """Words of capitalised terms (people, places, organisations, products), lower-cased.

    A capital after the start of a sentence is just grammar, so those words are skipped.
    """
    found = set()
    for match in _CAPITALISED.finditer(text):
        before = text[: match.start()].rstrip(" \t\"'“‘([*•–—-")
        if not before or before.endswith((".", "!", "?", "\n")):
            continue
        found |= {w for w in re.findall(r"[a-z]+", match.group().lower()) if len(w) > 1}
    return found


def _supported(text: str, evidence: PageEvidence) -> bool:
    """Every number and every name in a report line must appear somewhere in the source.

    This catches what a model adds from memory or makes up: figures it calculated, rounded or
    converted, and people, places or organisations the source never mentions.
    """
    if not all(n == 0 or _contains(evidence.numbers, n) for n in numbers_in(text)):
        return False
    return not evidence.vocabulary or _names(text) <= evidence.vocabulary


def ground_report(report: PageReport, evidence: PageEvidence) -> tuple[PageReport, int]:
    """Report text isn't tied to one item, so its numbers and names only have to appear anywhere in the source.

    Unsupported sentences are removed; a headline or overview that loses everything comes back empty for
    the caller to replace with the extractive one. data_gaps is left alone: it lists what the source lacks.
    """
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
        key: keep(getattr(report, key)) for key in ("key_points", "items_overview", "things_to_watch")
    }
    updates["detailed_summary"] = keep_sentences(report.detailed_summary)
    updates["overview"] = keep_sentences(report.overview)
    updates["headline"] = report.headline if _supported(report.headline, evidence) else ""
    updates["audience"] = report.audience if report.audience and _supported(report.audience, evidence) else None
    removed += (report.headline != updates["headline"]) + (report.audience != updates["audience"])
    sections = []
    for section in report.sections:
        summary = keep_sentences(section.summary)
        if summary:
            sections.append(section.model_copy(update={"summary": summary}))
    updates["sections"] = sections
    figures = [f for f in report.key_figures
               if all(_contains(evidence.numbers, n) for n in numbers_in(f.value))
               and _supported(f"{f.label}. {f.context or ''}", evidence)]
    removed += len(report.key_figures) - len(figures)
    updates["key_figures"] = figures
    updates["part_notes"] = [keep(notes) for notes in report.part_notes]
    updates["keyword_findings"] = [
        finding.model_copy(update={"summary": keep_sentences(finding.summary), "points": keep(finding.points)})
        for finding in report.keyword_findings
    ]
    return report.model_copy(update=updates), removed
