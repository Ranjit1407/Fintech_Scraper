# Re-uploads only add information: updated values replace old ones with the same label, and
# nothing stored earlier is deleted.

from __future__ import annotations

from typing import Any

from app.validation.schemas import ExtractedProduct, PageReport

_REPORT_LISTS = ("key_points", "products_overview", "rates_and_fees", "eligibility_and_conditions",
                 "things_to_watch", "data_gaps")


def _key(text: Any) -> str:
    return " ".join(str(text or "").lower().split())


def _by_label(old: list[dict], new: list[dict]) -> list[dict]:
    labels = {_key(item.get("label")) for item in new}
    return [item for item in old if _key(item.get("label")) not in labels or not item.get("label")] + new


def _union(old: list, new: list, key=_key) -> list:
    seen = {key(item) for item in old}
    return old + [item for item in new if key(item) not in seen]


def merge_product(existing: ExtractedProduct, new: ExtractedProduct) -> ExtractedProduct:
    old, fresh = existing.model_dump(mode="json"), new.model_dump(mode="json")
    merged = {**old, **{k: v for k, v in fresh.items() if v not in (None, "", [], {})}}
    merged["rates"] = _by_label(old["rates"], fresh["rates"])
    merged["fees"] = _by_label(old["fees"], fresh["fees"])
    for key in ("eligibility", "features", "conditions"):
        merged[key] = _union(old[key], fresh[key])
    merged["attributes"] = {**old["attributes"], **fresh["attributes"]}
    merged["unverified"] = fresh["unverified"]
    merged["confidence"] = new.confidence
    return ExtractedProduct.model_validate(merged)


def merge_reports(previous: PageReport, new: PageReport) -> PageReport:
    updates: dict[str, Any] = {key: _union(getattr(new, key), getattr(previous, key)) for key in _REPORT_LISTS}
    updates["key_figures"] = _union(new.key_figures, previous.key_figures, key=lambda f: _key(f.label))
    updates["sections"] = _union(new.sections, previous.sections, key=lambda s: _key(s.title))
    updates["part_notes"] = new.part_notes or previous.part_notes
    return new.model_copy(update=updates)
