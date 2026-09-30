# Re-uploads only add information: updated values replace old ones with the same label, and
# nothing stored earlier is deleted.

from __future__ import annotations

from typing import Any

from app.validation.schemas import ExtractedItem, PageReport

_REPORT_LISTS = ("key_points", "items_overview", "things_to_watch", "data_gaps")


def _key(text: Any) -> str:
    return " ".join(str(text or "").lower().split())


def _union(old: list, new: list, key=_key) -> list:
    seen = {key(item) for item in old}
    return old + [item for item in new if key(item) not in seen]


def merge_item(existing: ExtractedItem, new: ExtractedItem) -> ExtractedItem:
    old, fresh = existing.model_dump(mode="json"), new.model_dump(mode="json")
    merged = {**old, **{k: v for k, v in fresh.items() if v not in (None, "", [], {})}}
    labels = {_key(label) for label in fresh["attributes"]}
    merged["attributes"] = {**{k: v for k, v in old["attributes"].items() if _key(k) not in labels},
                            **fresh["attributes"]}
    merged["matched_keywords"] = _union(old["matched_keywords"], fresh["matched_keywords"])
    merged["unverified"] = fresh["unverified"]
    merged["confidence"] = new.confidence
    return ExtractedItem.model_validate(merged)


def merge_reports(previous: PageReport, new: PageReport) -> PageReport:
    updates: dict[str, Any] = {key: _union(getattr(new, key), getattr(previous, key)) for key in _REPORT_LISTS}
    updates["key_figures"] = _union(new.key_figures, previous.key_figures, key=lambda f: _key(f.label))
    updates["sections"] = _union(new.sections, previous.sections, key=lambda s: _key(s.title))
    updates["keyword_findings"] = _union(new.keyword_findings, previous.keyword_findings, key=lambda f: _key(f.keyword))
    updates["part_notes"] = new.part_notes or previous.part_notes
    return new.model_copy(update=updates)
